"""
core.errors — AppError exception hierarchy + api_error back-compat
==================================================================

Unified error envelope shared by every Leo HTTP surface. Replaces the
pre-refactor split between ``HTTPException({"detail": ...})`` and ad-hoc
``{"error": "..."}`` payloads.

Hierarchy
---------
:class:`AppError` is the base; each subclass sets a default ``status`` and
``default_code``. Call sites raise subclasses directly:

.. code-block:: python

    from core import NotFound, Forbidden

    if row is None:
        raise NotFound("Quote not found", code="QUOTE_NOT_FOUND")
    if row["contractor_id"] != caller.contractor_id:
        raise Forbidden("Not your quote")

A FastAPI global handler (registered in ``apps/scope-backend/main.py``
post-migration, in ``unmigrated/backend/main.py`` during the migration)
catches :class:`AppError` and calls :meth:`AppError.to_http`. The
``HTTPException.detail`` is always the same dict shape:

.. code-block:: json

    {"ok": false, "error": "message", "code": "CODE"}

The ``code`` key is omitted when the caller passes ``code=None``. The
``ok: false`` sentinel is load-bearing — the SPA keys off it to route
error toasts. Don't change.

Back-compat
-----------
:func:`api_error` (a factory returning a raw ``HTTPException``) is kept
for the 16 existing call-sites. New code should raise :class:`AppError`
subclasses directly. A ``DeprecationWarning`` will be emitted once the
shim-removal batch closes out the migration — see
``internal/refactor/packages/core/design.md`` decision D1.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException

__all__ = [
    "AppError",
    "NotFound",
    "Unauthorized",
    "Forbidden",
    "ValidationError",
    "Conflict",
    "RateLimited",
    "api_error",
]


class AppError(Exception):
    """Base for every user-facing HTTP error raised inside Leo.

    Subclasses set :attr:`status` (required) and :attr:`default_code`
    (optional). Call :meth:`to_http` to convert to a FastAPI
    :class:`HTTPException` with the canonical ``{"ok": false, ...}`` body.
    """

    status: int = 500
    default_code: Optional[str] = None

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code

    def to_detail(self) -> Dict[str, Any]:
        """Return the JSON-serialisable ``detail`` body for FastAPI."""
        detail: Dict[str, Any] = {"ok": False, "error": self.message}
        if self.code:
            detail["code"] = self.code
        return detail

    def to_http(self) -> HTTPException:
        """Return an :class:`HTTPException` with this error's status + body."""
        return HTTPException(status_code=self.status, detail=self.to_detail())


class NotFound(AppError):
    """HTTP 404 — resource (quote/contractor/job/etc.) could not be located."""

    status = 404
    default_code = "NOT_FOUND"


class Unauthorized(AppError):
    """HTTP 401 — caller is not authenticated (missing/invalid credentials)."""

    status = 401
    default_code = "UNAUTHORIZED"


class Forbidden(AppError):
    """HTTP 403 — caller is authenticated but not permitted to do this."""

    status = 403
    default_code = "FORBIDDEN"


class ValidationError(AppError):
    """HTTP 422 — request body/parameters failed validation."""

    status = 422
    default_code = "VALIDATION"


class Conflict(AppError):
    """HTTP 409 — state conflict (duplicate resource, stale update, …)."""

    status = 409
    default_code = "CONFLICT"


class RateLimited(AppError):
    """HTTP 429 — caller exceeded a quota or rate limit."""

    status = 429
    default_code = "RATE_LIMITED"


def api_error(
    status: int,
    message: str,
    *,
    code: Optional[str] = None,
) -> HTTPException:
    """Return an :class:`HTTPException` matching the unified error envelope.

    Raise the return value — ``raise api_error(404, "…")``.

    Kept as a backward-compat factory so the 16 existing ``raise
    api_error(...)`` call-sites can migrate lazily. New code should raise
    :class:`AppError` subclasses directly (``raise NotFound("…")``).
    """
    detail: Dict[str, Any] = {"ok": False, "error": message}
    if code:
        detail["code"] = code
    return HTTPException(status_code=status, detail=detail)
