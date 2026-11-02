"""
core.auth — authentication primitives for FastAPI routes
=========================================================

Submodules
----------
- :mod:`core.auth.users` — the :class:`~core.auth.users.CurrentUser` dataclass.
- :mod:`core.auth.jwt` — Supabase JWT verification + TTL-cached user lookup.
- :mod:`core.auth.api_keys` — ``leo_key_...`` programmatic access.
- :mod:`core.auth.deps` — FastAPI dependencies (``require_auth``, ``require_contractor``,
  ``require_homeowner``, ``optional_auth``, ``require_api_key_or_jwt``).
- :mod:`core.auth.tokens` — header-based ops/tracker tokens (fail-closed in prod,
  dev fallback under ``LEO_ENV=dev``).
- :mod:`core.auth.webhooks` — constant-time HMAC helper used by Resend, Meta,
  and internal webhook routes.

Everything worth importing flat is re-exported here so callers write::

    from core.auth import require_contractor, CurrentUser

rather than reaching into the submodules. Internals remain addressable at
``core.auth.jwt``, ``core.auth.tokens``, etc. for power users.
"""

from __future__ import annotations

from .api_keys import require_api_key_or_jwt
from .deps import (
    optional_auth,
    require_auth,
    require_contractor,
    require_homeowner,
)
from .tokens import require_ops_token, require_tracker_token
from .users import CurrentUser
from .webhooks import verify_webhook_signature

__all__ = [
    "CurrentUser",
    "require_auth",
    "optional_auth",
    "require_contractor",
    "require_homeowner",
    "require_ops_token",
    "require_tracker_token",
    "require_api_key_or_jwt",
    "verify_webhook_signature",
]
