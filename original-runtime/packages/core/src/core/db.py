"""
core.db — Supabase REST client (singleton + escape-hatch helpers)
==================================================================

Single source of truth for Supabase REST connectivity. Reads env vars
directly (``SUPABASE_URL``, ``SUPABASE_SERVICE_ROLE_KEY``,
``SUPABASE_ANON_KEY``) — no dependency on any app-level ``config.settings``.

Three tiers of API
------------------
1. **Primary** (new code): :func:`get_supabase` returns the pooled
   :class:`Supabase` singleton. The class wraps an ``httpx.AsyncClient``
   and exposes :meth:`~Supabase.select`, :meth:`~Supabase.insert`,
   :meth:`~Supabase.upsert`, :meth:`~Supabase.update`,
   :meth:`~Supabase.delete`, plus :meth:`~Supabase.request` for raw calls.

2. **Escape hatch**: :func:`sb_url`, :func:`sb_auth_url`, :func:`sb_key`,
   :func:`sb_headers`. These 4 exist so the webhook handlers and other
   one-shot call-sites can build their own ``httpx.AsyncClient`` when the
   pooled client's 300s read timeout is not appropriate.

3. **Lifecycle**: :func:`close_supabase_pool` (called from FastAPI's
   ``lifespan`` teardown) and :func:`check_supabase_health` (hits
   ``/rest/v1/?limit=0`` — 200 or 404 both count as healthy).

Invariants
----------
- Pooled client uses **300s read timeout** + 30s connect timeout. The
  agent runtime can block Supabase waiting for LLM generations — any
  shorter read timeout causes spurious failures. See
  ``internal/refactor/packages/core/audit.md`` §db invariant 1.
- ``Prefer: return=representation`` is on every write. Supabase returns
  the inserted/updated row in the body; callers rely on this. To suppress
  it on a given write, pass ``prefer="return=minimal"`` or
  ``prefer=None``.
- :func:`sb_key` prefers ``SUPABASE_SERVICE_ROLE_KEY``; falls back to
  ``SUPABASE_ANON_KEY``. Some dev setups run without a service-role key.
- :func:`check_supabase_health` treats 404 as healthy — a 404 means the
  request authenticated and routed, just that ``/rest/v1/`` has no index.

See ``internal/refactor/packages/core/design.md`` decision D4 for the
rationale behind this three-tier API.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Literal, Optional

import httpx

logger = logging.getLogger(__name__)


__all__ = [
    "Supabase",
    "get_supabase",
    "sb_url",
    "sb_auth_url",
    "sb_key",
    "sb_headers",
    "close_supabase_pool",
    "check_supabase_health",
]


# ---------------------------------------------------------------------------
# URL / key / headers — read env at call time so tests can stub via monkeypatch
# ---------------------------------------------------------------------------


def _supabase_url_base() -> str:
    return os.environ.get(
        "SUPABASE_URL",
        os.environ.get("NEXT_PUBLIC_SUPABASE_URL", ""),
    )


def sb_url() -> str:
    """Supabase REST base URL, i.e. ``{SUPABASE_URL}/rest/v1``."""
    return f"{_supabase_url_base()}/rest/v1"


def sb_auth_url() -> str:
    """Supabase Auth API base URL, i.e. ``{SUPABASE_URL}/auth/v1``."""
    return f"{_supabase_url_base()}/auth/v1"


def sb_key() -> str:
    """Service-role key if set, else anon key.

    Some dev environments run without a service-role key — falling back
    to the anon key keeps them functional (with anon-level RLS, of course).
    """
    service = os.environ.get(
        "SUPABASE_SERVICE_ROLE_KEY",
        os.environ.get("SUPABASE_SERVICE_KEY", ""),
    )
    anon = os.environ.get(
        "SUPABASE_ANON_KEY",
        os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""),
    )
    return service or anon


def sb_headers() -> Dict[str, str]:
    """Standard headers for Supabase REST calls.

    - ``apikey``: the key from :func:`sb_key`
    - ``Authorization: Bearer …``: same key (Supabase convention)
    - ``Content-Type: application/json``
    - ``Prefer: return=representation``: writes return the affected row
    """
    key = sb_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


# ---------------------------------------------------------------------------
# Supabase — high-level pooled client
# ---------------------------------------------------------------------------


class Supabase:
    """High-level Supabase REST client backed by a pooled :class:`httpx.AsyncClient`.

    Process-wide singleton — create once, reuse everywhere. Access via
    :func:`get_supabase` rather than instantiating directly.

    Most callers should use :meth:`select` / :meth:`insert` / :meth:`update`
    / :meth:`upsert` / :meth:`delete`. For rare cases needing raw HTTP
    control, use :meth:`request`.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying :class:`httpx.AsyncClient`."""
        return self._client

    # ---- convenience methods -------------------------------------------------

    async def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Run ``GET /rest/v1/{table}`` with PostgREST-style query params.

        ``filters`` is a ``{column: "op.value"}`` mapping passed through as
        query string — e.g. ``{"status": "eq.paid"}``.
        """
        params: Dict[str, str] = {"select": columns}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if order:
            params["order"] = order
        resp = await self._client.get(f"{sb_url()}/{table}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def insert(
        self,
        table: str,
        row: Dict[str, Any],
        *,
        prefer: Optional[str] = "return=representation",
    ) -> Any:
        """Run ``POST /rest/v1/{table}`` with a single row payload."""
        headers = dict(sb_headers())
        if prefer is not None:
            headers["Prefer"] = prefer
        resp = await self._client.post(
            f"{sb_url()}/{table}",
            json=row,
            headers=headers,
        )
        resp.raise_for_status()
        if prefer and "representation" in prefer:
            rows = resp.json()
            return rows[0] if isinstance(rows, list) and rows else rows
        return None

    async def upsert(
        self,
        table: str,
        row: Dict[str, Any],
        *,
        on_conflict: Optional[str] = None,
        prefer: Optional[str] = "return=representation",
    ) -> Any:
        """Run ``POST /rest/v1/{table}`` with ``Prefer: resolution=merge-duplicates``."""
        headers = dict(sb_headers())
        merged_prefer = "resolution=merge-duplicates"
        if prefer:
            merged_prefer = f"{merged_prefer},{prefer}"
        headers["Prefer"] = merged_prefer
        params: Dict[str, str] = {}
        if on_conflict:
            params["on_conflict"] = on_conflict
        resp = await self._client.post(
            f"{sb_url()}/{table}",
            json=row,
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        if prefer and "representation" in prefer:
            rows = resp.json()
            return rows[0] if isinstance(rows, list) and rows else rows
        return None

    async def update(
        self,
        table: str,
        patch: Dict[str, Any],
        *,
        filters: Dict[str, str],
        prefer: Optional[str] = "return=representation",
    ) -> List[Dict[str, Any]]:
        """Run ``PATCH /rest/v1/{table}`` with PostgREST filters on the URL."""
        headers = dict(sb_headers())
        if prefer is not None:
            headers["Prefer"] = prefer
        resp = await self._client.patch(
            f"{sb_url()}/{table}",
            params=filters,
            json=patch,
            headers=headers,
        )
        resp.raise_for_status()
        if prefer and "representation" in prefer:
            return resp.json()
        return []

    async def delete(
        self,
        table: str,
        *,
        filters: Dict[str, str],
        prefer: Optional[str] = "return=representation",
    ) -> List[Dict[str, Any]]:
        """Run ``DELETE /rest/v1/{table}`` with PostgREST filters."""
        headers = dict(sb_headers())
        if prefer is not None:
            headers["Prefer"] = prefer
        resp = await self._client.delete(
            f"{sb_url()}/{table}",
            params=filters,
            headers=headers,
        )
        resp.raise_for_status()
        if prefer and "representation" in prefer:
            return resp.json()
        return []

    async def request(
        self,
        method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"],
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Escape-hatch raw request. Returns the :class:`httpx.Response`.

        ``path`` is appended to :func:`sb_url` (i.e. pass ``"quotes?id=eq.1"``
        to hit ``/rest/v1/quotes?id=eq.1``).
        """
        merged_headers = dict(sb_headers())
        if headers:
            merged_headers.update(headers)
        return await self._client.request(
            method,
            f"{sb_url()}/{path.lstrip('/')}",
            params=params,
            json=json,
            headers=merged_headers,
        )


# ---------------------------------------------------------------------------
# Pooled singleton
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None
_supabase: Optional[Supabase] = None


def _build_client() -> httpx.AsyncClient:
    """Build a new pooled :class:`httpx.AsyncClient`.

    Invariants preserved from ``shared.db``:
    - 300s read, 30s connect (agent runtime can block on LLM generations)
    - ``Prefer: return=representation`` default header
    - Pool cap at 50 total connections, 20 keepalive (from pooled-client default)
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(300.0, connect=30.0),
        headers=sb_headers(),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )


async def get_supabase() -> Supabase:
    """Return the process-wide :class:`Supabase` singleton.

    Lazily creates the pooled :class:`httpx.AsyncClient` on first call.
    Subsequent calls return the same instance.
    """
    global _client, _supabase
    if _supabase is None:
        _client = _build_client()
        _supabase = Supabase(_client)
        logger.info("Supabase REST client created")
    return _supabase


async def close_supabase_pool() -> None:
    """Close the pooled client (call on FastAPI shutdown)."""
    global _client, _supabase
    if _client is not None:
        await _client.aclose()
        _client = None
        _supabase = None
        logger.info("Supabase REST client closed")


async def check_supabase_health() -> bool:
    """Probe Supabase reachability.

    Hits ``GET /rest/v1/?limit=0``. Returns True for 200 OR 404 (both
    indicate the request authenticated and routed successfully; a 404
    just means ``/rest/v1/`` has no index listing, which is expected).
    """
    try:
        sb = await get_supabase()
        resp = await sb.client.get(f"{sb_url()}/", params={"limit": "0"})
        return resp.status_code in (200, 404)
    except Exception as e:
        logger.error("Supabase health check failed: %s", e)
        return False
