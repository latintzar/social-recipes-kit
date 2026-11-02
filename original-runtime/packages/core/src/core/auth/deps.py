"""
core.auth.deps — FastAPI JWT-based identity dependencies
=========================================================

Four dependencies for route-level auth:

- :func:`require_auth` — any valid Supabase JWT; returns :class:`CurrentUser`.
- :func:`optional_auth` — like :func:`require_auth` but returns ``None`` when
  no ``Authorization`` header is present (no exception).
- :func:`require_contractor` — JWT whose identity resolves to
  ``role == "contractor"``.
- :func:`require_homeowner` — JWT whose identity resolves to
  ``role == "homeowner"``.

Each flow involves 2 Supabase round-trips per request today:
``GET /auth/v1/user`` (JWT verify, cached) + ``GET /rest/v1/contractors?...``
(role resolution, uncached on purpose — people flip accounts).

See ``internal/refactor/packages/core/audit.md`` §auth invariant 1.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

import httpx
from fastapi import Header

from core.db import sb_key, sb_url
from core.errors import Forbidden, Unauthorized

from .jwt import verify_supabase_jwt
from .users import CurrentUser

logger = logging.getLogger(__name__)

__all__ = [
    "require_auth",
    "optional_auth",
    "require_contractor",
    "require_homeowner",
]


async def _contractor_id_for_auth_user(auth_user_id: str) -> Optional[str]:
    """Look up the claimed contractor row for a Supabase auth user."""
    rest = sb_url()
    key = sb_key()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{rest}/contractors",
                params={
                    "auth_user_id": f"eq.{auth_user_id}",
                    "claimed": "eq.true",
                    "select": "id",
                    "limit": "1",
                },
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    return rows[0]["id"]
    except Exception as e:
        logger.warning(
            "Contractor lookup failed for auth_user %s: %s",
            str(auth_user_id)[:8],
            e,
        )
    return None


async def _resolve_current_user(access_token: str) -> CurrentUser:
    """Verify JWT, then resolve role + contractor_id."""
    user = await verify_supabase_jwt(access_token)
    if not user or not user.get("id"):
        raise Unauthorized("Invalid or expired token", code="JWT_INVALID")

    email = user.get("email", "")
    if not email:
        raise Unauthorized("No email on this account", code="JWT_NO_EMAIL")

    contractor_id = await _contractor_id_for_auth_user(user["id"])
    role: Literal["contractor", "homeowner"] = (
        "contractor" if contractor_id else "homeowner"
    )

    return CurrentUser(
        id=user["id"],
        email=email,
        role=role,
        contractor_id=contractor_id,
    )


async def require_auth(
    authorization: Optional[str] = Header(None),
) -> CurrentUser:
    """Require any valid Supabase JWT (contractor or homeowner)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise Unauthorized(
            "Missing Authorization header",
            code="AUTH_HEADER_MISSING",
        )
    return await _resolve_current_user(
        authorization.removeprefix("Bearer ").strip()
    )


async def optional_auth(
    authorization: Optional[str] = Header(None),
) -> Optional[CurrentUser]:
    """Return the caller identity if a JWT is present, else ``None``.

    Used on public endpoints that vary by role when authenticated but
    still work unauthenticated (e.g., ``GET /v1/projects/:token``).
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return await _resolve_current_user(
            authorization.removeprefix("Bearer ").strip()
        )
    except Unauthorized:
        return None


async def require_contractor(
    authorization: Optional[str] = Header(None),
) -> CurrentUser:
    """Require a JWT belonging to a claimed contractor."""
    if not authorization or not authorization.startswith("Bearer "):
        raise Unauthorized(
            "Missing Authorization header",
            code="AUTH_HEADER_MISSING",
        )

    user = await _resolve_current_user(
        authorization.removeprefix("Bearer ").strip()
    )
    if user.role != "contractor":
        raise Forbidden(
            "Contractor account required",
            code="ROLE_NOT_CONTRACTOR",
        )
    return user


async def require_homeowner(
    authorization: Optional[str] = Header(None),
) -> CurrentUser:
    """Require a JWT belonging to a homeowner (non-contractor) account."""
    if not authorization or not authorization.startswith("Bearer "):
        raise Unauthorized(
            "Missing Authorization header",
            code="AUTH_HEADER_MISSING",
        )

    user = await _resolve_current_user(
        authorization.removeprefix("Bearer ").strip()
    )
    if user.role != "homeowner":
        raise Forbidden(
            "Homeowner account required",
            code="ROLE_NOT_HOMEOWNER",
        )
    return user
