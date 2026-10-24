"""Idempotent dev setup: create the sir_* roles and the support_radar(+_test) databases.

Usage (from api/): uv run python ../scripts/db_setup.py
Env: PG_SUPERUSER_URL (default postgresql://postgres:postgres@localhost:5432/postgres).
Dev passwords equal the role name; real deployments create the roles with their own passwords.
"""

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import asyncpg

SUPERUSER_URL = os.environ.get(
    "PG_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
DATABASES = ["support_radar", "support_radar_test"]
ROLES = {  # name -> extra attributes
    "sir_owner": "",
    "sir_app": "NOBYPASSRLS",
    "sir_worker": "BYPASSRLS",
}


def with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url.replace("+asyncpg", ""))
    return urlunsplit(parts._replace(path=f"/{dbname}"))


async def ensure_roles(conn: asyncpg.Connection) -> None:
    for role, attrs in ROLES.items():
        exists = await conn.fetchval("select 1 from pg_roles where rolname = $1", role)
        verb = "alter" if exists else "create"
        # Identifiers are constants above, so formatting them into SQL is safe.
        await conn.execute(f"{verb} role {role} login password '{role}' {attrs}")


async def ensure_database(conn: asyncpg.Connection, dbname: str) -> None:
    if not await conn.fetchval("select 1 from pg_database where datname = $1", dbname):
        await conn.execute(f'create database "{dbname}" owner sir_owner')
    else:
        await conn.execute(f'alter database "{dbname}" owner to sir_owner')
    db_conn = await asyncpg.connect(with_database(SUPERUSER_URL, dbname))
    try:
        # PG15+: public schema belongs to pg_database_owner; let the owner role create in it.
        await db_conn.execute("alter schema public owner to sir_owner")
        await db_conn.execute(f'grant connect on database "{dbname}" to sir_app, sir_worker')
    finally:
        await db_conn.close()


async def setup(databases: list[str] = DATABASES) -> None:
    conn = await asyncpg.connect(SUPERUSER_URL.replace("+asyncpg", ""))
    try:
        await ensure_roles(conn)
        for db in databases:
            await ensure_database(conn, db)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(setup())
    print("db_setup: roles and databases ready:", ", ".join(DATABASES))
