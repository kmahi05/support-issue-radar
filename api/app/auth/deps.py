from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..errors import ApiError
from .tokens import decode_access_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    email: str


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser:
    unauthorized = ApiError(
        401, "unauthorized", "Missing or invalid access token", {"WWW-Authenticate": "Bearer"}
    )
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized
    try:
        claims = decode_access_token(creds.credentials)
        return CurrentUser(id=UUID(claims["sub"]), email=claims.get("email", ""))
    except (jwt.InvalidTokenError, ValueError):
        raise unauthorized from None
