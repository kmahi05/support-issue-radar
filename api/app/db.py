"""Two engines: sir_app (RLS applies, user requests) and sir_worker (BYPASSRLS, jobs/webhooks/auth).

Every worker query must filter by org_id explicitly.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .config import Settings

_engines: dict[str, AsyncEngine] = {}


def init_engines(settings: Settings) -> None:
    _engines["app"] = create_async_engine(settings.APP_DATABASE_URL, pool_pre_ping=True)
    _engines["worker"] = create_async_engine(settings.WORKER_DATABASE_URL, pool_pre_ping=True)


async def dispose_engines() -> None:
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()


@asynccontextmanager
async def user_session(user_id: UUID | str) -> AsyncIterator[AsyncConnection]:
    """One transaction on the app engine with app.user_id set (transaction-local)."""
    async with _engines["app"].begin() as conn:
        await conn.execute(
            text("select set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        yield conn


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncConnection]:
    async with _engines["worker"].begin() as conn:
        yield conn
