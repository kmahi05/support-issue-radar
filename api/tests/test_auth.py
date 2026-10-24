from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.auth.tokens import create_access_token
from tests.conftest import PASSWORD, register_and_login


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok", "chroma": "ok"}


async def test_register_created_duplicate_and_short_password(client):
    body = {"email": "dup@example.com", "password": PASSWORD, "display_name": "D"}
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 201
    assert r.json()["email"] == "dup@example.com"
    r = await client.post("/api/v1/auth/register", json={**body, "email": "DUP@example.com"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"
    r = await client.post(
        "/api/v1/auth/register", json={**body, "email": "s@example.com", "password": "short"}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_login_ok_wrong_password_and_unknown_email(client):
    u = await register_and_login(client)
    assert u["tokens"]["token_type"] == "bearer"
    assert u["tokens"]["expires_in"] == 900
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": u["email"], "password": "wrong-password-123"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "invalid_credentials"


async def test_login_rate_limited(client):
    body = {"email": "rl@example.com", "password": "wrong-password-123"}
    codes = [(await client.post("/api/v1/auth/login", json=body)).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


async def test_access_token_required(client):
    u = await register_and_login(client)
    assert (await client.get("/api/v1/me")).status_code == 401
    expired = create_access_token(
        UUID(u["user_id"]), u["email"], now=datetime.now(UTC) - timedelta(hours=1)
    )
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    good = u["tokens"]["access_token"]
    tampered = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    r = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401
    r = await client.get("/api/v1/me", headers=u["headers"])
    assert r.status_code == 200
    assert r.json()["email"] == u["email"]


async def test_refresh_rotation_and_reuse_detection(client):
    u = await register_and_login(client)
    r1 = u["tokens"]["refresh_token"]
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert resp.status_code == 200
    r2 = resp.json()["refresh_token"]
    assert r2 != r1
    # r1 was rotated: using it again is reuse -> 401 and the whole family is revoked
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": r1})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "refresh_token_reused"
    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": r2})
    assert resp.status_code == 401


async def test_logout_revokes_refresh_token(client):
    u = await register_and_login(client)
    rt = u["tokens"]["refresh_token"]
    assert (await client.post("/api/v1/auth/logout", json={"refresh_token": rt})).status_code == 204
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": rt})
    ).status_code == 401
