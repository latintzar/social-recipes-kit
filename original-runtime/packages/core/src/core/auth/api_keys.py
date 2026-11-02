"""
core.auth.api_keys — ``leo_key_...`` API key verification + combined dep
========================================================================

Programmatic access for property managers / integrators uses API keys of
the form ``leo_key_<opaque>``. Keys live in the ``api_keys`` Supabase
table with ``is_active=true`` and a set of ``permissions``.

Two entry points
----------------
- :func:`verify_api_key` — internal: look up the key row, fire-and-forget
  a ``last_used_at=now()`` update.
- :func:`require_api_key_or_jwt` — FastAPI dep: accepts EITHER a JWT
  (``Authorization: Bearer ...``) OR an API key (``X-API-Key: leo_key_...``).
  If both are present the API key wins (audit §auth invariant 5).

See ``internal/refactor/packages/core/audit.md`` §auth for the
rationale behind the key-over-JWT precedence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import Header

from core.db import sb_headers, sb_url
from core.errors import Unauthorized

from .jwt import verify_supabase_jwt

logger = logging.getLogger(__name__)

__all__ = ["verify_api_key", "require_api_key_or_jwt"]


async def verify_api_key(raw_key: str) -> Optional[Dict[str, Any]]:
    """Look up an API key; return the row (or ``None`` on miss / error).

    On a hit, fires a ``PATCH api_keys.last_used_at=now()`` as
    fire-and-forget — a failed update must not reject the caller.
    """
    rest = sb_url()
    headers = sb_headers()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{rest}/api_keys",
                headers=headers,
                params={
                    "key": f"eq.{raw_key}",
                    "is_active": "eq.true",
                    "select": "id,name,permissions,owner_id",
                    "limit": "1",
                },
            )
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    try:
                        await client.patch(
                            f"{rest}/api_keys",
                            headers=headers,
                            params={"id": f"eq.{rows[0]['id']}"},
                            json={"last_used_at": "now()"},
                        )
                    except Exception:
                        pass
                    return rows[0]
    except Exception as e:
        logger.warning("API key lookup failed: %s", e)
    return None


async def require_api_key_or_jwt(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """FastAPI dep: accept a JWT or an API key.

    API-key precedence: if ``X-API-Key`` is present, it's used even when a
    JWT is also present (audit §auth invariant 5).

    Returns a caller dict:

    - API key:
      ``{"type": "api_key", "key_id": ..., "name": ..., "permissions": [...], "owner_id": ...}``
    - JWT:
      ``{"type": "jwt", "user_id": ..., "email": ...}``

    Raises :class:`core.errors.Unauthorized` on missing / invalid
    credentials.
    """
    if x_api_key:
        if not x_api_key.startswith("leo_key_"):
            raise Unauthorized(
                "Invalid API key format (must start with leo_key_)",
                code="API_KEY_INVALID_FORMAT",
            )
        key_row = await verify_api_key(x_api_key)
        if not key_row:
            raise Unauthorized(
                "Invalid or inactive API key",
                code="API_KEY_INVALID",
            )
        logger.info(
            "API key auth: %s (%s)",
            key_row["name"],
            str(key_row["id"])[:8],
        )
        return {
            "type": "api_key",
            "key_id": key_row["id"],
            "name": key_row["name"],
            "permissions": key_row.get("permissions", ["read"]),
            "owner_id": key_row.get("owner_id"),
        }

    if authorization and authorization.startswith("Bearer "):
        access_token = authorization.removeprefix("Bearer ").strip()
        user = await verify_supabase_jwt(access_token)
        if user and user.get("id"):
            return {
                "type": "jwt",
                "user_id": user["id"],
                "email": user.get("email", ""),
            }
        raise Unauthorized("Invalid or expired token", code="JWT_INVALID")

    raise Unauthorized(
        "Authentication required (X-API-Key or Authorization header)",
        code="AUTH_MISSING",
    )
