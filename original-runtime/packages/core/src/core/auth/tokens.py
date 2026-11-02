"""
core.auth.tokens — header-based ops/tracker token dependencies
===============================================================

Two thin FastAPI deps for staff-only routes that don't have (or want) a
full Supabase JWT:

- :func:`require_ops_token` — ``X-Ops-Token`` must match ``OPS_TOKEN`` env.
- :func:`require_tracker_token` — ``X-Tracker-Token`` must match
  ``SALES_TRACKER_TOKEN`` env.

Fail-closed in prod, dev-allow under ``LEO_ENV=dev``
-----------------------------------------------------
Pre-refactor behaviour: if the env var was unset, requests were allowed
through with a warning log. That was convenient for dev but a footgun in
prod. The refactor flips the default to **fail-closed**, gating the
open-allow behaviour behind an explicit ``LEO_ENV=dev`` flag.

The old hardcoded fallback ``"leo-sales-dev-token-2026"`` for
``SALES_TRACKER_TOKEN`` is dropped entirely. See
``internal/refactor/packages/core/design.md`` decision D8.

Constant-time comparison
------------------------
Both tokens are compared via :func:`hmac.compare_digest` to avoid
timing side-channels.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import Header

from core.errors import Forbidden

logger = logging.getLogger(__name__)

__all__ = [
    "require_ops_token",
    "require_tracker_token",
    "tracker_token_valid",
]


def _is_dev_env() -> bool:
    return os.environ.get("LEO_ENV", "").lower() == "dev"


def tracker_token_valid(token: Optional[str]) -> bool:
    """Predicate form of :func:`require_tracker_token` for routes that
    only *conditionally* require a valid ``X-Tracker-Token``.

    Shared by routes that gate on a runtime flag (e.g. chat with
    ``mode=sales``), where raising ``Forbidden`` unconditionally would
    block legitimate non-sales traffic. Returns ``True`` iff ``token``
    matches ``SALES_TRACKER_TOKEN`` (constant-time compare), ``False``
    otherwise. In ``LEO_ENV=dev`` with the env unset, returns ``True``
    to match the fail-open-in-dev semantics of the dep.
    """
    expected = os.environ.get("SALES_TRACKER_TOKEN", "")
    if not expected:
        return _is_dev_env()
    if not token:
        return False
    return hmac.compare_digest(str(token), str(expected))


def _check_token(
    header_value: Optional[str],
    env_var: str,
    header_name: str,
    code_prefix: str,
) -> None:
    expected = os.environ.get(env_var, "")
    if not expected:
        if _is_dev_env():
            logger.warning(
                "%s: %s unset; LEO_ENV=dev → allowing all",
                header_name,
                env_var,
            )
            return
        raise Forbidden(
            f"{env_var} not configured",
            code=f"{code_prefix}_MISSING",
        )
    if not header_value or not hmac.compare_digest(
        str(header_value), str(expected)
    ):
        raise Forbidden(
            f"Invalid {header_name}",
            code=f"{code_prefix}_INVALID",
        )


async def require_ops_token(x_ops_token: Optional[str] = Header(None)) -> None:
    """Require ``X-Ops-Token`` matching ``OPS_TOKEN`` env var.

    - Raises :class:`core.errors.Forbidden` (403) if ``OPS_TOKEN`` is
      unset in prod, or if the header doesn't match.
    - Allows all under ``LEO_ENV=dev`` with a warning log.
    """
    _check_token(x_ops_token, "OPS_TOKEN", "X-Ops-Token", "OPS_TOKEN")


async def require_tracker_token(
    x_tracker_token: Optional[str] = Header(None),
) -> None:
    """Require ``X-Tracker-Token`` matching ``SALES_TRACKER_TOKEN`` env var.

    Same fail-closed semantics as :func:`require_ops_token`.
    """
    _check_token(
        x_tracker_token,
        "SALES_TRACKER_TOKEN",
        "X-Tracker-Token",
        "TRACKER_TOKEN",
    )
