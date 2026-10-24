import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import db
from .auth.router import me_router
from .auth.router import router as auth_router
from .config import get_settings
from .errors import install_error_handlers
from .orgs.router import router as orgs_router
from .ratelimit import limiter
from .vectorstore import open_client, open_collections

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    db.init_engines(s)
    s.CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    app.state.chroma = open_client(str(s.CHROMA_PATH))
    app.state.collections = open_collections(app.state.chroma, s.GEMINI_EMBEDDING_MODEL)
    try:
        yield
    finally:
        await db.dispose_engines()


def create_app() -> FastAPI:
    s = get_settings()
    logging.basicConfig(level=s.LOG_LEVEL)
    app = FastAPI(title="Support Issue Radar API", lifespan=lifespan)
    limiter.enabled = s.RATE_LIMIT_ENABLED
    app.state.limiter = limiter
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[s.WEB_ORIGIN],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(orgs_router)

    @app.get("/health")
    async def health(request: Request):
        status = {"status": "ok", "db": "ok", "chroma": "ok"}
        try:
            async with db.worker_session() as conn:
                await conn.execute(text("select 1"))
        except Exception:
            log.exception("health: db check failed")
            status.update(status="error", db="error")
        try:
            request.app.state.chroma.heartbeat()
        except Exception:
            log.exception("health: chroma check failed")
            status.update(status="error", chroma="error")
        return JSONResponse(status, status_code=200 if status["status"] == "ok" else 503)

    return app


app = create_app()
