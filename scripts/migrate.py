"""Apply db/migrations/NNNN_name.sql in name order, one transaction each.

Usage (from api/): uv run python ../scripts/migrate.py [--database-url URL]
Default URL: MIGRATE_DATABASE_URL (the sir_owner role).
"""

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


async def migrate(database_url: str) -> list[str]:
    conn = await asyncpg.connect(database_url.replace("+asyncpg", ""))
    applied_now: list[str] = []
    try:
        await conn.execute(
            "create table if not exists schema_migrations "
            "(version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["version"] for r in await conn.fetch("select version from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("insert into schema_migrations (version) values ($1)", path.stem)
            applied_now.append(path.stem)
    finally:
        await conn.close()
    return applied_now


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "MIGRATE_DATABASE_URL", "postgresql://sir_owner:sir_owner@localhost:5432/support_radar"
        ),
    )
    applied = asyncio.run(migrate(parser.parse_args().database_url))
    print("migrate: applied", applied or "nothing (up to date)")
