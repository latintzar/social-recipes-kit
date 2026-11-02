"""
core.auth.jwt — Supabase JWT verification + TTL cache
======================================================

Every authenticated request verifies the caller's JWT with Supabase
(``GET /auth/v1/user``). Without a cache, that's 1 network round-trip per
request. This module adds a small TTL cache keyed by the token itself —
same token within TTL returns the cached payload.

Environment flags
-----------------
- ``LEO_DISABLE_JWT_CACHE=true`` — kill-switch for the cache. Every call
  hits Supabase.
- ``LEO_JWT_CACHE_TTL=60`` — seconds. Default 60 (refresh-token lifetime
  on Supabase is 60 min, stale-ish for 1 min is safe).
- ``LEO_JWT_CACHE_SIZE=1000`` — LRU cap.

Never cache role/contractor_id
------------------------------
The cache stores only the raw Supabase ``/auth/v1/user`` payload. Role
and contractor_id are looked up separately (see
:mod:`core.auth.deps`) because users can flip between contractor and
homeowner accounts — we always want a fresh DB read for that.

See ``internal/refactor/packages/core/design.md`` decision D6.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

import httpx

from core.db import sb_auth_url, sb_key

logger = logging.getLogger(__name__)

__all__ = ["verify_supabase_jwt", "clear_jwt_cache"]


# ---------------------------------------------------------------------------
# TTL cache — simple LRU+TTL, keyed by the raw JWT string
# ---------------------------------------------------------------------------


class _JwtCache:
    """Tiny thread-safe LRU+TTL cache. Keyed by the raw token."""

    def __init__(self, *, maxsize: int, ttl: float) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[float, Dict[str, Any]]]" = OrderedDict()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            expires_at, payload = hit
            if expires_at < now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return payload

    def set(self, key: str, payload: Dict[str, Any]) -> None:
        expires_at = time.monotonic() + self.ttl
        with self._lock:
            self._entries[key] = (expires_at, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _cache_enabled() -> bool:
    return os.environ.get("LEO_DISABLE_JWT_CACHE", "").lower() != "true"


def _ttl_seconds() -> float:
    try:
        return float(os.environ.get("LEO_JWT_CACHE_TTL", "60"))
    except (TypeError, ValueError):
        return 60.0


def _cache_size() -> int:
    try:
        return int(os.environ.get("LEO_JWT_CACHE_SIZE", "1000"))
    except (TypeError, ValueError):
        return 1000


_JWT_CACHE: _JwtCache = _JwtCache(maxsize=_cache_size(), ttl=_ttl_seconds())


def clear_jwt_cache() -> None:
    """Drop every entry from the JWT cache. Used by tests."""
    _JWT_CACHE.clear()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


async def verify_supabase_jwt(access_token: str) -> Optional[Dict[str, Any]]:
    """Verify a JWT via ``GET /auth/v1/user``.

    Returns the Supabase user payload on success, or ``None`` on any
    failure (invalid token, network error, non-200). Transient network
    errors are logged at WARNING and treated as failed auth — callers
    that need to distinguish 401 from "Supabase is down" should not use
    this helper.

    Uses the TTL cache unless ``LEO_DISABLE_JWT_CACHE=true``.
    """
    if _cache_enabled():
        cached = _JWT_CACHE.get(access_token)
        if cached is not None:
            return cached

    auth_url = sb_auth_url()
    key = sb_key()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{auth_url}/user",
                headers={
                    "apikey": key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
            if resp.status_code == 200:
                payload = resp.json()
                if _cache_enabled():
                    _JWT_CACHE.set(access_token, payload)
                return payload
    except Exception as e:
        logger.warning("JWT verification request failed: %s", e)
    return None
