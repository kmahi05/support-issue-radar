from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SLUG = "support-radar"


class Settings(BaseSettings):
    """All env vars are read here and nowhere else (CLAUDE.md conventions)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_DATABASE_URL: str = "postgresql+asyncpg://sir_app:sir_app@localhost:5432/support_radar"
    WORKER_DATABASE_URL: str = (
        "postgresql+asyncpg://sir_worker:sir_worker@localhost:5432/support_radar"
    )

    JWT_SECRET: str
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    REFRESH_TOKEN_TTL_SECONDS: int = 14 * 24 * 3600
    INVITE_TTL_SECONDS: int = 7 * 24 * 3600

    APP_ENCRYPTION_KEY: str  # Fernet key (urlsafe base64, 32 bytes)

    GOOGLE_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_MODEL_STRONG: str = "gemini-2.5-pro"
    GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"
    # Backfill estimate for CSV imports (§6); check current Gemini pricing before relying on it.
    LLM_COST_PER_1K_TICKETS_USD: float = 1.30

    CHROMA_PATH: Path = Path("./data/chroma")
    STORAGE_DIR: Path = Path("./data/storage")
    MAX_CSV_UPLOAD_BYTES: int = 50 * 1024 * 1024

    WEB_ORIGIN: str = "http://localhost:3003"
    RATE_LIMIT_ENABLED: bool = True
    LOG_LEVEL: str = "INFO"

    @field_validator("JWT_SECRET")
    @classmethod
    def _jwt_secret_long_enough(cls, v: str) -> str:
        if len(v.encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return v

    @field_validator("APP_ENCRYPTION_KEY")
    @classmethod
    def _fernet_key(cls, v: str) -> str:
        from cryptography.fernet import Fernet

        try:
            Fernet(v)
        except ValueError:
            raise ValueError("APP_ENCRYPTION_KEY must be a Fernet key (see .env.example)") from None
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
