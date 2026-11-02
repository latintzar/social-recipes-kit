"""
core.http — general-purpose HTTP client factories
===================================================

Three context managers that every route / task uses instead of
instantiating ``httpx.AsyncClient`` ad-hoc:

- :func:`http_client` — async, short-lived. The default for 30+ route
  files. ``connect=min(timeout, 10.0)``, ``follow_redirects=False``.
- :func:`http_client_sync` — sync counterpart for CLI tools.
- :func:`sb_request` — Supabase-scoped short-lived client (pre-set
  base_url + auth headers). Use this for webhook handlers or other
  one-shot call-sites that shouldn't touch the pooled :class:`Supabase`
  singleton (which has 300s read timeout — inappropriate for webhooks).

Invariants
----------
- ``connect`` timeout is capped at 10s regardless of the ``timeout``
  argument. (A 30s request timeout shouldn't also mean a 30s connect.)
- ``follow_redirects=False`` by default; callers that expect redirects
  pass ``follow_redirects=True`` explicitly.

See ``internal/refactor/packages/core/audit.md`` §http for the
connection-churn trade-off and ``internal/refactor/packages/core/design.md``
decision D5 (the old ``HTTPClient`` Protocol was deleted — zero callers).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

import httpx

from core.db import sb_headers, sb_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10

__all__ = [
    "DEFAULT_TIMEOUT",
    "http_client",
    "http_client_sync",
    "sb_request",
]


@asynccontextmanager
async def http_client(
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    """General-purpose async client with Leo's defaults.

    Connect timeout is capped at 10s (``min(timeout, 10.0)``). Redirects
    are disabled by default; override with ``follow_redirects=True``.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        follow_redirects=kwargs.pop("follow_redirects", False),
        **kwargs,
    ) as client:
        yield client


@asynccontextmanager
async def sb_request(
    timeout: float = DEFAULT_TIMEOUT,
) -> AsyncIterator[httpx.AsyncClient]:
    """Short-lived Supabase-scoped async client (base_url + auth headers).

    Use for webhook handlers that don't want to share the pooled
    :class:`core.db.Supabase` singleton's 300s read timeout. The base_url
    is the Supabase host root (i.e. without the ``/rest/v1`` suffix) so
    callers can hit both ``/rest/v1/...`` and ``/auth/v1/...`` paths.
    """
    base = sb_url().rsplit("/rest/v1", 1)[0]
    async with httpx.AsyncClient(
        base_url=base,
        headers=sb_headers(),
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
    ) as client:
        yield client


@contextmanager
def http_client_sync(
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> Iterator[httpx.Client]:
    """Sync counterpart to :func:`http_client`.

    Used by CLI tools and scripts outside the async backend. Same
    defaults: connect cap at 10s, no redirects.
    """
    with httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
        follow_redirects=kwargs.pop("follow_redirects", False),
        **kwargs,
    ) as client:
        yield client
