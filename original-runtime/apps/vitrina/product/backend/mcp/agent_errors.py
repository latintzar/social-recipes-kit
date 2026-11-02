"""Agent error ticket queue.

Every time a Leo agent tool call raises, we persist a row into the
``agent_errors`` table (schema defined in
``api/migrations/001_commerce_tables.sql``). The DB has an ``after insert``
trigger (``trg_agent_errors_slack``) that posts the row to Slack via
``pg_net``, so Peter is paged automatically without the backend needing
any outbound HTTP code.

This module is the thin data-access layer around that table:

- :func:`create_error` — insert a new ticket.
- :func:`get_error`     — fetch a single ticket by id.
- :func:`list_user_errors` — unresolved/resolved tickets for one user.
- :func:`list_unresolved`   — ops queue across all users.
- :func:`mark_resolved`     — close a ticket with an optional note.

The wrapping :mod:`agent.error_handling` decorator is what actually calls
:func:`create_error` from inside the agent loop; the raw functions here
are exposed so the MCP tool surface (see ``AGENT_ERROR_TOOLS`` at the
bottom) and the REST routes in ``api/routes/agent_errors.py`` can share
one implementation.

Shared with subagents A1 (migrations), A4 (supabase client), A9 (agent
loop). The MCP descriptors at the bottom of the file are re-exported for
PR 11 to register alongside the other commerce tools.
"""
from __future__ import annotations

from typing import Any

try:  # pragma: no cover - placeholder path when A4 has not landed yet.
    from supabase_client import Supabase, get_supabase  # type: ignore
except Exception:  # pragma: no cover
    Supabase = object  # type: ignore[assignment,misc]

    def get_supabase() -> Any:  # type: ignore[misc]
        raise RuntimeError("supabase_client_not_available")


_TABLE = "agent_errors"


# ── writes ──────────────────────────────────────────────────────────


def create_error(
    *,
    tool_name: str,
    args: dict,
    error_message: str,
    stack: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    cart_key: str | None = None,
    cart_id: str | None = None,
    recoverable: bool = True,
    posthog_event_id: str | None = None,
) -> dict:
    """Insert one ``agent_errors`` row and return the persisted record.

    Parameters map 1:1 to the DB columns; ``id`` / ``created_at`` are
    generated server-side. The DB trigger ``trg_agent_errors_slack`` fires
    right after this insert commits and posts to Slack — callers don't
    need to notify anyone, just write the row.

    Args:
        tool_name: The MCP tool that raised (e.g. ``"add_item"``).
        args:      The tool kwargs that were in flight, JSON-serialisable.
        error_message: ``str(exc)`` from the failing handler.
        stack:     Optional ``traceback.format_exc()`` output.
        thread_id: Agent conversation thread (if known).
        user_id:   Authenticated end-user (if known).
        cart_key:  Anonymous cart identifier (if no user yet).
        cart_id:   Resolved cart row id (if the tool had one in scope).
        recoverable: ``True`` when the agent should apologise and
            continue; ``False`` for hard-stops we surface more loudly.
        posthog_event_id: Matching PostHog event for cross-linking.

    Returns:
        The inserted row as a dict, including the generated ``id``.
    """
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "args": args or {},
        "error_message": error_message,
        "recoverable": recoverable,
    }
    if stack is not None:
        payload["stack"] = stack
    if thread_id is not None:
        payload["thread_id"] = thread_id
    if user_id is not None:
        payload["user_id"] = user_id
    if cart_key is not None:
        payload["cart_key"] = cart_key
    if cart_id is not None:
        payload["cart_id"] = cart_id
    if posthog_event_id is not None:
        payload["posthog_event_id"] = posthog_event_id

    sb = get_supabase()
    row = sb.table(_TABLE).insert(payload).single().execute()
    if not row:
        raise RuntimeError("agent_errors_insert_returned_empty")
    return row


def mark_resolved(
    ticket_id: str,
    *,
    resolved_by: str,
    resolution_note: str | None = None,
) -> dict:
    """Close a ticket. Sets ``resolved_at = now()`` via Postgres default.

    This endpoint is server-side-only (see ``api/routes/agent_errors.py``).
    The frontend never marks tickets resolved; Peter does that from the
    Supabase dashboard or via ops tooling.

    Args:
        ticket_id: ``agent_errors.id`` uuid.
        resolved_by: ``users.id`` of the operator resolving the ticket.
        resolution_note: Free-form note explaining the fix/root-cause.
    """
    patch: dict[str, Any] = {
        # Supabase rejects sentinel strings for timestamptz; use ISO now().
        # We fill it here (not in the DB) so the mock backend stays simple.
        "resolved_at": _now_iso(),
        "resolved_by": resolved_by,
    }
    if resolution_note is not None:
        patch["resolution_note"] = resolution_note

    sb = get_supabase()
    row = (
        sb.table(_TABLE)
        .update(patch)
        .eq("id", ticket_id)
        .single()
        .execute()
    )
    if not row:
        raise LookupError(f"agent_error_not_found: {ticket_id}")
    return row


# ── reads ───────────────────────────────────────────────────────────


def get_error(ticket_id: str) -> dict:
    """Fetch a single ticket by id. Raises :class:`LookupError` if missing."""
    sb = get_supabase()
    row = (
        sb.table(_TABLE)
        .select("*")
        .eq("id", ticket_id)
        .single()
        .execute()
    )
    if not row:
        raise LookupError(f"agent_error_not_found: {ticket_id}")
    return row


def list_user_errors(
    user_id: str,
    *,
    resolved: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    """List tickets for one user, newest first.

    Args:
        user_id: ``users.id`` uuid.
        resolved: ``None`` returns both; ``False`` returns only open
            tickets (``resolved_at IS NULL``); ``True`` returns only
            closed ones.
        limit: Max rows (default 100).
    """
    sb = get_supabase()
    q = sb.table(_TABLE).select("*").eq("user_id", user_id)
    if resolved is False:
        q = q.is_("resolved_at", None)
    elif resolved is True:
        q = q.neq("resolved_at", None)
    return q.order("created_at", desc=True).limit(limit).execute() or []


def list_unresolved(limit: int = 200) -> list[dict]:
    """Ops queue: every open ticket across all users, newest first."""
    sb = get_supabase()
    return (
        sb.table(_TABLE)
        .select("*")
        .is_("resolved_at", None)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        or []
    )


# ── helpers ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    """ISO-8601 timestamp in UTC. Factored out for test monkeypatching."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ── MCP tool descriptors ────────────────────────────────────────────
#
# Re-exported so PR 11's agent loop can register these alongside the
# existing commerce tools in ``tools.py``. We keep them here (not in
# ``tools.py``) so the ticket-queue surface stays co-located with the
# implementation.

AGENT_ERROR_TOOLS: list[dict[str, Any]] = [
    {
        "name": "agent_errors.create",
        "description": (
            "Record a new agent error ticket. Normally invoked by the "
            "capture_tool_errors decorator, not by the LLM directly."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["tool_name", "args", "error_message"],
            "properties": {
                "tool_name": {"type": "string"},
                "args": {"type": "object"},
                "error_message": {"type": "string"},
                "stack": {"type": "string"},
                "thread_id": {"type": "string", "format": "uuid"},
                "user_id": {"type": "string", "format": "uuid"},
                "cart_key": {"type": "string"},
                "cart_id": {"type": "string", "format": "uuid"},
                "recoverable": {"type": "boolean", "default": True},
                "posthog_event_id": {"type": "string"},
            },
        },
    },
    {
        "name": "agent_errors.get",
        "description": "Fetch a single agent error ticket by id.",
        "inputSchema": {
            "type": "object",
            "required": ["ticket_id"],
            "properties": {"ticket_id": {"type": "string", "format": "uuid"}},
        },
    },
    {
        "name": "agent_errors.list_user",
        "description": "List agent error tickets for a specific user.",
        "inputSchema": {
            "type": "object",
            "required": ["user_id"],
            "properties": {
                "user_id": {"type": "string", "format": "uuid"},
                "resolved": {"type": ["boolean", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
        },
    },
    {
        "name": "agent_errors.list_unresolved",
        "description": "Ops queue: every open agent error ticket, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
        },
    },
    {
        "name": "agent_errors.resolve",
        "description": (
            "Mark a ticket resolved. Server-side only — the frontend never "
            "calls this; Peter closes tickets from the Supabase dashboard."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["ticket_id", "resolved_by"],
            "properties": {
                "ticket_id": {"type": "string", "format": "uuid"},
                "resolved_by": {"type": "string", "format": "uuid"},
                "resolution_note": {"type": "string"},
            },
        },
    },
]
