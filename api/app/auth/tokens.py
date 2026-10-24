import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from ..config import SLUG, get_settings

ISSUER = SLUG
AUDIENCE = f"{SLUG}-api"


def create_access_token(user_id: UUID, email: str, now: datetime | None = None) -> str:
    s = get_settings()
    now = now or datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=s.ACCESS_TOKEN_TTL_SECONDS),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "typ": "access",
    }
    return jwt.encode(claims, s.JWT_SECRET, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError on any problem (signature, exp, iss, aud, typ)."""
    claims = jwt.decode(
        token,
        get_settings().JWT_SECRET,
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["sub", "exp", "iat", "nbf", "iss", "aud"]},
    )
    if claims.get("typ") != "access":
        raise jwt.InvalidTokenError("not an access token")
    return claims


def new_opaque_token() -> tuple[str, str]:
    """Return (token, sha256 hex). Only the hash is stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
