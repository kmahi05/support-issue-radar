"""Org routes (PRD §4 Auth & session). Orgs are created only through public.create_org()."""

import secrets
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ..auth.deps import CurrentUser, get_current_user
from ..auth.tokens import new_opaque_token
from ..config import get_settings
from ..crypto import encrypt
from ..db import user_session, worker_session
from ..errors import ApiError

router = APIRouter(prefix="/api/v1/orgs", tags=["orgs"])

Role = Literal["admin", "lead", "agent", "viewer"]


class CreateOrgIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    timezone: str = "UTC"

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v.strip()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown IANA timezone") from None
        return v


class AddMemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: Role = "agent"


async def require_role(conn: AsyncConnection, org_id: UUID, allowed: set[str] | None) -> str:
    """Membership check in the RLS session. Non-members get 404 so org ids do not leak."""
    role = await conn.scalar(text("select public.org_role(:o)"), {"o": org_id})
    if role is None:
        raise ApiError(404, "not_found", "Organization not found")
    if allowed is not None and role not in allowed:
        raise ApiError(403, "forbidden", "Your role cannot do this")
    return role


@router.post("", status_code=201)
async def create_org(body: CreateOrgIn, user: CurrentUser = Depends(get_current_user)) -> dict:
    ingest_secret = secrets.token_urlsafe(32)
    async with user_session(user.id) as conn:
        org_id = await conn.scalar(
            text("select public.create_org(:n, :tz, :s)"),
            {"n": body.name, "tz": body.timezone, "s": encrypt(ingest_secret)},
        )
    return {
        "org_id": str(org_id),
        "name": body.name,
        "timezone": body.timezone,
        "role": "admin",
        "ingest_secret": ingest_secret,  # shown once; stored only encrypted
    }


@router.get("")
async def list_my_orgs(user: CurrentUser = Depends(get_current_user)) -> dict:
    async with user_session(user.id) as conn:
        rows = (
            await conn.execute(
                text(
                    "select o.id, o.name, o.timezone, m.role, o.created_at "
                    "from organizations o join org_members m on m.org_id = o.id "
                    "where m.user_id = :u order by o.created_at"
                ),
                {"u": user.id},
            )
        ).all()
    return {
        "items": [
            {
                "org_id": str(r.id),
                "name": r.name,
                "timezone": r.timezone,
                "role": r.role,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.get("/{org_id}/members")
async def list_members(org_id: UUID, user: CurrentUser = Depends(get_current_user)) -> dict:
    async with user_session(user.id) as conn:
        await require_role(conn, org_id, None)
    # Other users' emails are not visible to sir_app (users RLS = own row), so read them as worker
    # after the membership check, filtered by org_id.
    async with worker_session() as conn:
        rows = (
            await conn.execute(
                text(
                    "select u.id, u.email, u.display_name, m.role, m.created_at "
                    "from org_members m join users u on u.id = m.user_id "
                    "where m.org_id = :o order by m.created_at"
                ),
                {"o": org_id},
            )
        ).all()
    return {
        "items": [
            {
                "user_id": str(r.id),
                "email": r.email,
                "display_name": r.display_name,
                "role": r.role,
                "joined_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/{org_id}/members", status_code=201)
async def add_member(
    org_id: UUID, body: AddMemberIn, user: CurrentUser = Depends(get_current_user)
) -> dict:
    async with user_session(user.id) as conn:
        await require_role(conn, org_id, {"admin"})
    async with worker_session() as conn:
        existing = await conn.scalar(
            text("select id from users where lower(email) = lower(:e)"), {"e": body.email}
        )
        if existing is not None:
            added = await conn.scalar(
                text(
                    "insert into org_members (org_id, user_id, role) values (:o, :u, :r) "
                    "on conflict (org_id, user_id) do nothing returning user_id"
                ),
                {"o": org_id, "u": existing, "r": body.role},
            )
            if added is None:
                raise ApiError(409, "already_member", "This user is already a member")
            return {"user_id": str(existing), "role": body.role}
        token, token_hash = new_opaque_token()
        expires_at = await conn.scalar(
            text(
                "insert into invites (org_id, email, role, token_hash, invited_by, expires_at) "
                "values (:o, :e, :r, :h, :by, now() + make_interval(secs => :ttl)) "
                "returning expires_at"
            ),
            {
                "o": org_id,
                "e": body.email,
                "r": body.role,
                "h": token_hash,
                "by": user.id,
                "ttl": get_settings().INVITE_TTL_SECONDS,
            },
        )
    # No email sending in v1: the admin shares the link containing this token. Shown once.
    return {"invite_token": token, "role": body.role, "expires_at": expires_at.isoformat()}
