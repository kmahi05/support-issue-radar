"""Auth routes (PRD §4 Auth). They run before a user session exists, so they use the worker role."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..config import get_settings
from ..db import user_session, worker_session
from ..errors import ApiError
from ..ratelimit import limiter
from .deps import CurrentUser, get_current_user
from .passwords import hash_password, needs_rehash, verify_password
from .tokens import create_access_token, hash_token, new_opaque_token

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
me_router = APIRouter(prefix="/api/v1", tags=["auth"])

Password = Field(min_length=10, max_length=128)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterIn(Strict):
    email: EmailStr
    password: str = Password
    display_name: str = Field(min_length=1, max_length=120)


class LoginIn(Strict):
    email: EmailStr
    password: str = Field(max_length=128)


class RefreshIn(Strict):
    refresh_token: str = Field(min_length=1, max_length=200)


class AcceptInviteIn(Strict):
    token: str = Field(min_length=1, max_length=200)
    password: str = Password
    display_name: str = Field(min_length=1, max_length=120)


class TokenPair(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str


def _invalid_credentials() -> ApiError:
    return ApiError(401, "invalid_credentials", "Invalid email or password")


async def _issue_tokens(conn: AsyncConnection, user_id: UUID, email: str) -> TokenPair:
    s = get_settings()
    token, token_hash = new_opaque_token()
    await conn.execute(
        text(
            "insert into refresh_tokens (user_id, token_hash, expires_at) "
            "values (:uid, :h, now() + make_interval(secs => :ttl))"
        ),
        {"uid": user_id, "h": token_hash, "ttl": s.REFRESH_TOKEN_TTL_SECONDS},
    )
    return TokenPair(
        access_token=create_access_token(user_id, email),
        expires_in=s.ACCESS_TOKEN_TTL_SECONDS,
        refresh_token=token,
    )


@router.post("/register", status_code=201)
async def register(body: RegisterIn) -> dict:
    async with worker_session() as conn:
        user_id = await conn.scalar(
            text(
                "insert into users (email, password_hash, display_name) values (:e, :p, :d) "
                "on conflict ((lower(email))) do nothing returning id"
            ),
            {"e": body.email, "p": hash_password(body.password), "d": body.display_name},
        )
    if user_id is None:
        raise ApiError(409, "email_taken", "An account with this email already exists")
    return {"user_id": str(user_id), "email": body.email}


@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, body: LoginIn) -> TokenPair:
    async with worker_session() as conn:
        row = (
            await conn.execute(
                text(
                    "select id, email, password_hash, is_active from users "
                    "where lower(email) = lower(:e)"
                ),
                {"e": body.email},
            )
        ).first()
        if not verify_password(row.password_hash if row else None, body.password) or not (
            row and row.is_active
        ):
            raise _invalid_credentials()
        if needs_rehash(row.password_hash):
            await conn.execute(
                text("update users set password_hash = :p where id = :id"),
                {"p": hash_password(body.password), "id": row.id},
            )
        return await _issue_tokens(conn, row.id, row.email)


@router.post("/refresh")
async def refresh(body: RefreshIn) -> TokenPair:
    reused = False
    async with worker_session() as conn:
        row = (
            await conn.execute(
                text(
                    "select rt.id, rt.user_id, rt.expires_at, rt.revoked_at, u.email, u.is_active "
                    "from refresh_tokens rt join users u on u.id = rt.user_id "
                    "where rt.token_hash = :h for update of rt"
                ),
                {"h": hash_token(body.refresh_token)},
            )
        ).first()
        if row and row.revoked_at is not None:
            # Reuse of a rotated token: assume theft, revoke the whole family for this user.
            await conn.execute(
                text(
                    "update refresh_tokens set revoked_at = now() "
                    "where user_id = :uid and revoked_at is null"
                ),
                {"uid": row.user_id},
            )
            reused = True
        elif row and row.expires_at > datetime.now(UTC) and row.is_active:
            pair = await _issue_tokens(conn, row.user_id, row.email)
            await conn.execute(
                text(
                    "update refresh_tokens set revoked_at = now(), "
                    "replaced_by = (select id from refresh_tokens where token_hash = :new) "
                    "where id = :id"
                ),
                {"new": hash_token(pair.refresh_token), "id": row.id},
            )
            return pair
    if reused:
        raise ApiError(401, "refresh_token_reused", "Refresh token reuse detected; log in again")
    raise ApiError(401, "invalid_refresh_token", "Invalid or expired refresh token")


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn) -> Response:
    async with worker_session() as conn:
        await conn.execute(
            text(
                "update refresh_tokens set revoked_at = now() "
                "where token_hash = :h and revoked_at is null"
            ),
            {"h": hash_token(body.refresh_token)},
        )
    return Response(status_code=204)


@router.post("/accept-invite")
async def accept_invite(body: AcceptInviteIn) -> TokenPair:
    async with worker_session() as conn:
        invite = (
            await conn.execute(
                text(
                    "select id, org_id, email, role from invites where token_hash = :h "
                    "and accepted_at is null and expires_at > now() for update"
                ),
                {"h": hash_token(body.token)},
            )
        ).first()
        if invite is None:
            raise ApiError(400, "invalid_invite", "Invite is invalid, used or expired")
        user = (
            await conn.execute(
                text("select id, email, password_hash from users where lower(email) = lower(:e)"),
                {"e": invite.email},
            )
        ).first()
        if user is None:
            user_id = await conn.scalar(
                text(
                    "insert into users (email, password_hash, display_name) "
                    "values (:e, :p, :d) returning id"
                ),
                {"e": invite.email, "p": hash_password(body.password), "d": body.display_name},
            )
            email = invite.email
        elif verify_password(user.password_hash, body.password):
            user_id, email = user.id, user.email
        else:
            raise _invalid_credentials()
        await conn.execute(
            text(
                "insert into org_members (org_id, user_id, role) values (:o, :u, :r) "
                "on conflict (org_id, user_id) do nothing"
            ),
            {"o": invite.org_id, "u": user_id, "r": invite.role},
        )
        await conn.execute(
            text("update invites set accepted_at = now() where id = :id"), {"id": invite.id}
        )
        return await _issue_tokens(conn, user_id, email)


@me_router.get("/me")
async def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    async with user_session(user.id) as conn:
        row = (
            await conn.execute(
                text("select id, email, display_name from users where id = :id"), {"id": user.id}
            )
        ).first()
        if row is None:
            raise ApiError(401, "unauthorized", "User no longer exists")
        orgs = (
            await conn.execute(
                text(
                    "select o.id, o.name, m.role from org_members m "
                    "join organizations o on o.id = m.org_id "
                    "where m.user_id = :id order by o.name"
                ),
                {"id": user.id},
            )
        ).all()
    return {
        "user_id": str(row.id),
        "email": row.email,
        "display_name": row.display_name,
        "orgs": [{"org_id": str(o.id), "name": o.name, "role": o.role} for o in orgs],
    }
