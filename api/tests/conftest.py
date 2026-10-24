"""Test setup: a real Postgres test DB (support_radar_test), recreated once per session.

If Postgres is unreachable, DB tests are skipped; pure unit tests still run.
"""

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.fernet import Fernet

TEST_DB = "support_radar_test"
SUPERUSER_URL = os.environ.setdefault(
    "PG_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
_host = urlsplit(SUPERUSER_URL).netloc.split("@")[-1]
_tmp = Path(tempfile.mkdtemp(prefix="sir-tests-"))

os.environ.update(
    {
        "APP_DATABASE_URL": f"postgresql+asyncpg://sir_app:sir_app@{_host}/{TEST_DB}",
        "WORKER_DATABASE_URL": f"postgresql+asyncpg://sir_worker:sir_worker@{_host}/{TEST_DB}",
        "JWT_SECRET": "test-secret-" + "x" * 40,
        "APP_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "CHROMA_PATH": str(_tmp / "chroma"),
        "STORAGE_DIR": str(_tmp / "storage"),
        "GOOGLE_API_KEY": "",
        "RATE_LIMIT_ENABLED": "true",
    }
)
OWNER_URL = f"postgresql://sir_owner:sir_owner@{_host}/{TEST_DB}"
APP_URL = f"postgresql://sir_app:sir_app@{_host}/{TEST_DB}"
WORKER_URL = f"postgresql://sir_worker:sir_worker@{_host}/{TEST_DB}"

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


async def _recreate_test_db() -> None:
    import asyncpg
    import db_setup
    import migrate

    conn = await asyncpg.connect(SUPERUSER_URL, timeout=3)
    try:
        await conn.execute(f'drop database if exists "{TEST_DB}" with (force)')
    finally:
        await conn.close()
    await db_setup.setup([TEST_DB])
    await migrate.migrate(OWNER_URL)


@pytest.fixture(scope="session")
def database() -> None:
    import asyncpg

    async def ping() -> None:
        conn = await asyncpg.connect(SUPERUSER_URL, timeout=3)
        await conn.close()

    try:
        asyncio.run(ping())
    except (OSError, asyncpg.PostgresError, TimeoutError):
        pytest.skip(f"PostgreSQL not reachable at {SUPERUSER_URL}")
    asyncio.run(_recreate_test_db())


@pytest.fixture
async def client(database):
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.ratelimit import limiter

    limiter.reset()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


PASSWORD = "correct-horse-battery"


async def register_and_login(client, email: str | None = None) -> dict:
    email = email or f"u-{uuid.uuid4().hex[:10]}@example.com"
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Test User"},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    tokens = r.json()
    return {
        "email": email,
        "user_id": (await _me(client, tokens["access_token"]))["user_id"],
        "tokens": tokens,
        "headers": {"Authorization": f"Bearer {tokens['access_token']}"},
    }


async def _me(client, access_token: str) -> dict:
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {access_token}"})
    assert r.status_code == 200, r.text
    return r.json()
