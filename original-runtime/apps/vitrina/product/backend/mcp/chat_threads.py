"""Chat threads + messages persistence for Vitrina (Phase 1 rebuild).

Backs the agent-side conversation memory: every Vitrina session (logged-in
user OR anonymous cart_key shopper) owns a list of ``chat_threads``; each
thread owns an ordered log of ``chat_messages`` (jsonb content, role-tagged
like OpenAI chat-completions). A thread can also be *seeded from an order*
so a shopper can tap "reorder" on last week's delivery and the agent picks
up with the original line-items already surfaced as context.

This module is the ONLY place where the ``chat_threads`` / ``chat_messages``
tables are touched. The REST router in ``api/routes/chat.py`` and the MCP
tool descriptors at the bottom of this file both call into these sync
handlers. Tests monkeypatch :func:`_get_supabase` onto the in-memory fake
at ``tests/fixtures/supabase_mock.py``.

## Public recipe entry

The recovered recipe launcher derives the sender's owner key and thread UUID
from a verified, business-account-scoped Instagram event. Its Supabase client
selects the dedicated social_recipes schema with server-only grants and RLS.
These handlers enforce that server-derived owner again. The model's recipe
MCP tool has neither session CRUD nor session-store credentials.

## Original broader integration — "trust the claim"

Each handler (except ``create_thread`` / ``seed_thread_from_order``) takes
``user_id=`` and/or ``cart_key=`` kwargs and checks that the thread row's
own ``user_id`` / ``cart_key`` column matches one of them. That's the same
pattern the A4 cart handlers use — we trust the route layer to have
already decoded the Supabase JWT (for logged-in users) or read the
``X-Vitrina-Cart-Key`` header (for anonymous shoppers). The service-role
client bypasses RLS, so owner enforcement remains in these handlers.

## What's deliberately NOT here

- Streaming. The agent loop calls :func:`append_message` once per
  completed turn; partial deltas belong in the runtime, not the DB.
- Full-text search across messages. Phase 2 — will live in a separate
  ``chat_search.py`` so this module stays narrow.
- Message editing / redaction. Phase 2 (GDPR erasure request handler).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

try:  # A4 writes supabase_client.py; fall back to a placeholder if absent.
    from supabase_client import Supabase, get_supabase  # type: ignore[import-not-found]
except Exception:  # pragma: no cover — coordinator reconciles this shape.
    class Supabase:  # type: ignore[no-redef]
        """Placeholder — real client lives in ``supabase_client.py``."""

    def get_supabase() -> Supabase:  # type: ignore[no-redef]
        raise RuntimeError("supabase_not_configured")


logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────

VALID_ROLES = frozenset({"user", "assistant", "tool", "system"})

_THREADS = "chat_threads"
_MESSAGES = "chat_messages"
_ORDERS = "orders"
_SHOPS = "shops"


# ── Exceptions ─────────────────────────────────────────────────────────


class ThreadNotFound(LookupError):
    """Raised when a thread row can't be found by id."""


class ThreadAccessDenied(PermissionError):
    """Raised when the auth kwargs don't match the thread's owner."""


class InvalidRole(ValueError):
    """Raised when :func:`append_message` gets a role outside VALID_ROLES."""


# ── Internal helpers ───────────────────────────────────────────────────


def _get_supabase() -> Supabase:
    """Test seam — patched onto a ``FakeSupabase`` instance in tests."""
    return get_supabase()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_content(content: dict | list | str) -> dict | list:
    """Serialise arbitrary message content into a JSONB-compatible shape.

    Strings are wrapped as ``{"kind": "text", "text": ...}`` so the
    database always stores structured content — consumers can rely on
    ``content`` being a dict/list (never a bare scalar).
    """
    if isinstance(content, str):
        return {"kind": "text", "text": content}
    if isinstance(content, (dict, list)):
        return content
    raise TypeError(
        f"message content must be dict|list|str, got {type(content).__name__}"
    )


def _require_thread(thread_id: str) -> dict[str, Any]:
    """Fetch a thread row by id or raise :class:`ThreadNotFound`.

    Supabase REST's ``.single()`` returns HTTP 406 with PGRST116 when the
    row is missing (instead of an empty list). Translate that into a
    proper ``ThreadNotFound`` so callers — particularly the IG DM
    adapter's first-DM path — can fall through to ``create_thread``.
    """
    sb = _get_supabase()
    try:
        row = (
            sb.table(_THREADS)
            .select("*")
            .eq("id", thread_id)
            .single()
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "PGRST116" in msg or "0 rows" in msg or " 406" in msg:
            raise ThreadNotFound(thread_id) from exc
        raise
    if not row:
        raise ThreadNotFound(thread_id)
    return row


def _enforce_access(
    thread: dict[str, Any],
    *,
    user_id: str | None,
    cart_key: str | None,
) -> None:
    """Raise :class:`ThreadAccessDenied` unless the thread belongs to caller.

    Either the user_id matches the thread's ``user_id`` column, or the
    cart_key matches the ``cart_key`` column. Both can be present on a
    request (e.g. a shopper who just logged in); either match is enough.
    Matching against a NULL column never counts.
    """
    owner_user = thread.get("user_id")
    owner_key = thread.get("cart_key")
    if user_id is not None and owner_user is not None and str(owner_user) == str(user_id):
        return
    if cart_key is not None and owner_key is not None and str(owner_key) == str(cart_key):
        return
    raise ThreadAccessDenied(thread.get("id"))


def _bump_thread_updated(thread_id: str) -> None:
    """Refresh ``updated_at`` on the parent thread after a message write."""
    sb = _get_supabase()
    sb.table(_THREADS).update({"updated_at": _now_iso()}).eq("id", thread_id).execute()


# ── Thread CRUD ────────────────────────────────────────────────────────


def create_thread(
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
    title: str | None = None,
    seeded_from_order_id: str | None = None,
    id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new empty chat thread.

    At least one of ``user_id`` / ``cart_key`` should be supplied — an
    anonymous-and-keyless thread would never be reachable again. We don't
    hard-fail on that though; the row is still useful for debugging.

    Pass ``id`` to use a deterministic UUID (e.g. Instagram DM threads).
    """
    sb = _get_supabase()
    now = _now_iso()
    row = {
        "id": id or str(uuid.uuid4()),
        "user_id": user_id,
        "cart_key": cart_key,
        "title": title,
        "pinned": False,
        "seeded_from_order_id": seeded_from_order_id,
        "created_at": now,
        "updated_at": now,
    }
    if metadata:
        row["metadata"] = metadata
    result = sb.table(_THREADS).insert(row).execute()
    return result[0] if isinstance(result, list) else (result or row)


def get_thread(
    thread_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Fetch a thread row, enforcing owner auth."""
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    return thread


def patch_thread_metadata(
    thread_id: str,
    patch: dict[str, Any],
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Merge *patch* into ``chat_threads.metadata`` (jsonb)."""
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    current = thread.get("metadata") or {}
    if not isinstance(current, dict):
        current = {}
    merged: dict[str, Any] = {**current, **patch}
    sb = _get_supabase()
    sb.table(_THREADS).update(
        {"metadata": merged, "updated_at": _now_iso()}
    ).eq("id", thread_id).execute()
    return merged


def list_user_threads(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """List threads owned by a logged-in user, newest-first."""
    sb = _get_supabase()
    rows = (
        sb.table(_THREADS)
        .select("*")
        .eq("user_id", user_id)
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(rows or [])


def list_threads_by_key(cart_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """List threads owned by an anonymous cart key, newest-first."""
    sb = _get_supabase()
    rows = (
        sb.table(_THREADS)
        .select("*")
        .eq("cart_key", cart_key)
        .order("updated_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(rows or [])


def pin_thread(
    thread_id: str,
    pinned: bool,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Pin or unpin a thread. Pinned threads surface at the top in the UI."""
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    sb = _get_supabase()
    result = (
        sb.table(_THREADS)
        .update({"pinned": bool(pinned), "updated_at": _now_iso()})
        .eq("id", thread_id)
        .execute()
    )
    return (result[0] if isinstance(result, list) and result else result) or {
        **thread,
        "pinned": bool(pinned),
    }


def update_thread_title(
    thread_id: str,
    title: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Rename a thread."""
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    sb = _get_supabase()
    result = (
        sb.table(_THREADS)
        .update({"title": title, "updated_at": _now_iso()})
        .eq("id", thread_id)
        .execute()
    )
    return (result[0] if isinstance(result, list) and result else result) or {
        **thread,
        "title": title,
    }


def delete_thread(
    thread_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Delete a thread.

    Cascade is handled by the DB (``chat_messages.thread_id`` FK has
    ``ON DELETE CASCADE``). We also fire a defensive message-delete first
    so the in-memory fake backend matches production semantics — the real
    Supabase cascade already handles it server-side.
    """
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    sb = _get_supabase()
    sb.table(_MESSAGES).delete().eq("thread_id", thread_id).execute()
    sb.table(_THREADS).delete().eq("id", thread_id).execute()
    return {"ok": True, "id": thread_id}


# ── Messages ───────────────────────────────────────────────────────────


def append_message(
    thread_id: str,
    role: str,
    content: dict | list | str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Append a message to a thread and bump the thread's ``updated_at``."""
    if role not in VALID_ROLES:
        raise InvalidRole(f"role={role!r} not in {sorted(VALID_ROLES)}")
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    sb = _get_supabase()
    row = {
        "id": str(uuid.uuid4()),
        "thread_id": thread_id,
        "role": role,
        "content": _normalize_content(content),
        "created_at": _now_iso(),
    }
    result = sb.table(_MESSAGES).insert(row).execute()
    inserted = result[0] if isinstance(result, list) and result else (result or row)
    _bump_thread_updated(thread_id)
    return inserted


def list_messages(
    thread_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return messages in the thread in chronological order (oldest first)."""
    thread = _require_thread(thread_id)
    _enforce_access(thread, user_id=user_id, cart_key=cart_key)
    sb = _get_supabase()
    rows = (
        sb.table(_MESSAGES)
        .select("*")
        .eq("thread_id", thread_id)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return list(rows or [])


# ── Seed-from-order ────────────────────────────────────────────────────


def _fetch_order(order_id: str) -> dict[str, Any]:
    sb = _get_supabase()
    row = sb.table(_ORDERS).select("*").eq("id", order_id).single().execute()
    if not row:
        raise ThreadNotFound(f"order:{order_id}")
    return row


def _fetch_shop_name(shop_id: str | None) -> str:
    if not shop_id:
        return "the shop"
    sb = _get_supabase()
    try:
        row = sb.table(_SHOPS).select("*").eq("id", shop_id).single().execute()
    except Exception:  # pragma: no cover — shops table optional in tests
        return str(shop_id)
    if not row:
        return str(shop_id)
    return row.get("name_en") or row.get("name_bg") or str(shop_id)


def seed_thread_from_order(
    order_id: str,
    *,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Create a new thread that "reopens" a past order for re-ordering.

    We record the source order via ``seeded_from_order_id`` (so the UI can
    badge the thread as "repeat of …") and plant two Phase-1 seed
    messages:

    1. A ``user`` role text message with the cadence-setting prompt:
       ``Repeating order {id} — {N items} from {shop_name}. What would
       you like to change?``
    2. A ``tool`` role message carrying the original order's ``line_items``
       as a ``kind=tool_result`` payload, so the agent can consult it
       without re-fetching the order row.

    Returns the thread row (after seeding), with an additional
    ``_seed_messages`` list for the caller's convenience.
    """
    order = _fetch_order(order_id)
    line_items = order.get("line_items") or []
    shop_name = _fetch_shop_name(order.get("shop_id"))
    n_items = len(line_items) if isinstance(line_items, list) else 0

    thread = create_thread(
        user_id=user_id,
        cart_key=cart_key,
        title=f"Repeat order {str(order_id)[:8]}",
        seeded_from_order_id=str(order_id),
    )
    thread_id = thread["id"]

    prompt = (
        f"Repeating order {order_id} — {n_items} item"
        f"{'s' if n_items != 1 else ''} from {shop_name}. "
        "What would you like to change?"
    )
    msg_user = append_message(
        thread_id,
        "user",
        prompt,
        user_id=user_id,
        cart_key=cart_key,
    )
    msg_tool = append_message(
        thread_id,
        "tool",
        {
            "kind": "tool_result",
            "tool": "seed_from_order",
            "order_id": str(order_id),
            "shop_id": order.get("shop_id"),
            "line_items": line_items,
        },
        user_id=user_id,
        cart_key=cart_key,
    )
    thread["_seed_messages"] = [msg_user, msg_tool]
    return thread


# ── MCP tool descriptors ───────────────────────────────────────────────

CHAT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "chat.create_thread",
        "description": "Create an empty chat thread for a user or cart_key.",
        "handler": create_thread,
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
                "seeded_from_order_id": {"type": ["string", "null"]},
            },
        },
    },
    {
        "name": "chat.get_thread",
        "description": "Fetch a chat thread by id, with owner auth.",
        "handler": get_thread,
        "input_schema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
            },
        },
    },
    {
        "name": "chat.list_user_threads",
        "description": "List threads owned by a logged-in user.",
        "handler": list_user_threads,
        "input_schema": {
            "type": "object",
            "required": ["user_id"],
            "properties": {
                "user_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "chat.list_threads_by_key",
        "description": "List threads owned by an anonymous cart_key.",
        "handler": list_threads_by_key,
        "input_schema": {
            "type": "object",
            "required": ["cart_key"],
            "properties": {
                "cart_key": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "chat.append_message",
        "description": "Append a role-tagged message to a thread.",
        "handler": append_message,
        "input_schema": {
            "type": "object",
            "required": ["thread_id", "role", "content"],
            "properties": {
                "thread_id": {"type": "string"},
                "role": {"type": "string", "enum": sorted(VALID_ROLES)},
                "content": {"type": ["object", "array", "string"]},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
            },
        },
    },
    {
        "name": "chat.list_messages",
        "description": "List messages in a thread (chronological).",
        "handler": list_messages,
        "input_schema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
                "limit": {"type": "integer", "default": 500},
            },
        },
    },
    {
        "name": "chat.pin_thread",
        "description": "Pin or unpin a thread.",
        "handler": pin_thread,
        "input_schema": {
            "type": "object",
            "required": ["thread_id", "pinned"],
            "properties": {
                "thread_id": {"type": "string"},
                "pinned": {"type": "boolean"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
            },
        },
    },
    {
        "name": "chat.update_thread_title",
        "description": "Rename a thread.",
        "handler": update_thread_title,
        "input_schema": {
            "type": "object",
            "required": ["thread_id", "title"],
            "properties": {
                "thread_id": {"type": "string"},
                "title": {"type": "string"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
            },
        },
    },
    {
        "name": "chat.delete_thread",
        "description": "Delete a thread and its messages (cascade).",
        "handler": delete_thread,
        "input_schema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {"type": "string"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
            },
        },
    },
    {
        "name": "chat.seed_thread_from_order",
        "description": "Create a repeat-order thread pre-seeded with the order's line_items.",
        "handler": seed_thread_from_order,
        "input_schema": {
            "type": "object",
            "required": ["order_id"],
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
            },
        },
    },
]


__all__ = [
    "VALID_ROLES",
    "ThreadNotFound",
    "ThreadAccessDenied",
    "InvalidRole",
    "create_thread",
    "get_thread",
    "list_user_threads",
    "list_threads_by_key",
    "pin_thread",
    "update_thread_title",
    "delete_thread",
    "append_message",
    "list_messages",
    "seed_thread_from_order",
    "CHAT_TOOLS",
]
