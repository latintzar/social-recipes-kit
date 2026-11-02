"""Structured per-turn tracing for the Vitrina shopper agent.

**Goal — one place that knows everything about every conversation.**
Every channel (web SSE, Instagram DM, TikTok DM, repl) funnels into one
``run_turn`` and emits a complete, queryable timeline of what happened:
the user message that opened the turn, every Hermes tool call, every
inbound/outbound bubble, every error, the wall-clock latency of each
step, and the final assistant reply with its widgets.

Two storage tiers, both opt-in friendly:

1. **Hetzner stdout (always on)** — every event is also a structured
   JSON line prefixed ``[TRACE] {...}`` so ``ssh root@5.161.178.146
   'journalctl -u vitrina-agent -n 1000 --no-pager'`` shows the live
   cross-channel timeline. No infra dependency.

2. **Supabase ``mcp.agent_turns`` + ``mcp.agent_trace_events`` (opt-in)**
   when migration `011_agent_traces.sql` is applied. Best-effort; a
   missing table or DB outage logs a warning and never blocks the turn.

A *turn* is one user→assistant cycle. Every event inside the turn
shares its ``turn_id`` so reconstructing the trace is just a SQL
``WHERE turn_id = ...`` (or ``rg <turn_id>`` against Railway logs).

## Adding a trace point

```python
from tracing import trace
trace("recipes.extract.queued", url=url, brief=brief)
```

Call sites pick the event name freely (``<area>.<action>`` convention);
``fields`` is whatever JSON-serialisable kwargs are useful. The
``turn_id`` is read from the contextvar set by :func:`start_turn` (or
explicitly passed via ``turn_id=``).

## Thread propagation

Hermes runs tool handlers on its own thread pool, where contextvars are
not auto-propagated. We keep a ``thread_id → turn_id`` fallback map (the
same trick :mod:`_session_ctx` uses for session metadata) so a tool
handler running on a worker thread still resolves the right ``turn_id``
via :func:`current_turn_id`.

See ``apps/vitrina/product/backend/agent/README.md`` § *Tracing* for the
end-to-end story (deployment, querying, retention, viewer CLI).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger("vitrina.tracing")


# ── Per-turn correlation -------------------------------------------------

_CURRENT_TURN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vitrina_turn_id", default=None
)

_LOCK = threading.Lock()
# Thread-pool fallback (Hermes tool workers don't inherit contextvars).
_TURN_BY_THREAD: dict[str, str] = {}
# Per-turn bookkeeping so finish_turn can compute elapsed + seq.
_TURN_T0: dict[str, float] = {}
_TURN_SEQ: dict[str, int] = {}
_TURN_META: dict[str, dict[str, Any]] = {}


def current_turn_id(*, thread_id: str | None = None) -> str | None:
    """Resolve the active turn id from contextvar, then thread map."""
    cv = _CURRENT_TURN.get()
    if cv:
        return cv
    if thread_id:
        with _LOCK:
            return _TURN_BY_THREAD.get(str(thread_id))
    return None


def _next_seq(turn_id: str) -> int:
    with _LOCK:
        n = _TURN_SEQ.get(turn_id, 0)
        _TURN_SEQ[turn_id] = n + 1
        return n


# ── Optional Supabase persistence ----------------------------------------

_SB_ENABLED_ENV = "VITRINA_AGENT_TRACE_SUPABASE"


def _supabase_enabled() -> bool:
    """Persist to ``mcp.agent_turns`` + ``mcp.agent_trace_events`` when set.

    Off by default until migration ``011_agent_traces.sql`` is applied
    on the Supabase project. Setting ``VITRINA_AGENT_TRACE_SUPABASE=1``
    flips it on; missing tables / RLS errors log a warning the first
    time and we silently degrade to stdout-only afterwards.
    """
    return (os.getenv(_SB_ENABLED_ENV) or "").strip() in {"1", "true", "TRUE"}


_SB_FAILED_ONCE = False


def _persist_event(turn_id: str, seq: int, event: str, fields: dict[str, Any]) -> None:
    """Best-effort insert into ``mcp.agent_trace_events``."""
    global _SB_FAILED_ONCE
    if not _supabase_enabled() or _SB_FAILED_ONCE:
        return
    try:
        from core import sb_client, sb_headers  # noqa: PLC0415
        url = f"{os.environ['SUPABASE_URL']}/rest/v1/agent_trace_events"
        body = {
            "turn_id": turn_id,
            "seq": seq,
            "event": event,
            "fields": fields,
        }
        # mcp schema → use Content-Profile header.
        with sb_client(timeout=2.0) as client:
            client.post(
                url,
                headers={**sb_headers(), "Content-Profile": "mcp", "Prefer": "return=minimal"},
                json=body,
            )
    except Exception:
        _SB_FAILED_ONCE = True
        logger.warning("tracing: supabase persist disabled after first failure", exc_info=True)


def _persist_turn_start(turn_id: str, fields: dict[str, Any]) -> None:
    global _SB_FAILED_ONCE
    if not _supabase_enabled() or _SB_FAILED_ONCE:
        return
    try:
        from core import sb_client, sb_headers  # noqa: PLC0415
        url = f"{os.environ['SUPABASE_URL']}/rest/v1/agent_turns"
        body = {"id": turn_id, **fields}
        with sb_client(timeout=2.0) as client:
            client.post(
                url,
                headers={**sb_headers(), "Content-Profile": "mcp", "Prefer": "return=minimal"},
                json=body,
            )
    except Exception:
        _SB_FAILED_ONCE = True
        logger.warning("tracing: supabase persist disabled after first failure", exc_info=True)


def _persist_turn_finish(turn_id: str, fields: dict[str, Any]) -> None:
    global _SB_FAILED_ONCE
    if not _supabase_enabled() or _SB_FAILED_ONCE:
        return
    try:
        from core import sb_client, sb_headers  # noqa: PLC0415
        url = f"{os.environ['SUPABASE_URL']}/rest/v1/agent_turns?id=eq.{turn_id}"
        with sb_client(timeout=2.0) as client:
            client.patch(
                url,
                headers={**sb_headers(), "Content-Profile": "mcp", "Prefer": "return=minimal"},
                json=fields,
            )
    except Exception:
        _SB_FAILED_ONCE = True
        logger.warning("tracing: supabase persist disabled after first failure", exc_info=True)


# ── Public API -----------------------------------------------------------


def start_turn(
    *,
    channel: str,
    thread_id: str | None = None,
    user_id: str | None = None,
    cart_key: str | None = None,
    user_message: str = "",
    source_msg_id: str | None = None,
) -> str:
    """Begin a turn, return the new ``turn_id``.

    Call this at the *very* top of every channel adapter so every
    downstream emit (tool dispatch, adapter close, IG send) is tagged.
    """
    turn_id = uuid.uuid4().hex
    _CURRENT_TURN.set(turn_id)
    with _LOCK:
        _TURN_T0[turn_id] = time.perf_counter()
        _TURN_SEQ[turn_id] = 0
        _TURN_META[turn_id] = {
            "channel": channel,
            "thread_id": thread_id,
            "user_id": user_id,
            "cart_key": cart_key,
        }
        if thread_id:
            _TURN_BY_THREAD[str(thread_id)] = turn_id
    fields = {
        "channel": channel,
        "thread_id": thread_id,
        "user_id": user_id,
        "cart_key": cart_key,
        "source_msg_id": source_msg_id,
        "user_message_chars": len(user_message or ""),
        "user_message_preview": (user_message or "")[:160],
    }
    trace("turn.start", _turn_id=turn_id, **fields)
    _persist_turn_start(
        turn_id,
        {
            "channel": channel,
            "thread_id": thread_id,
            "user_id": user_id,
            "cart_key": cart_key,
            "source_msg_id": source_msg_id,
            "user_message": (user_message or "")[:4000],
        },
    )
    return turn_id


def finish_turn(
    turn_id: str | None = None,
    *,
    status: str = "ok",
    error: str | None = None,
    assistant_text: str = "",
    widgets: list[dict[str, Any]] | None = None,
    bubbles: int = 0,
    tool_calls: int = 0,
) -> None:
    """Mark a turn complete (success / silent / error)."""
    turn_id = turn_id or _CURRENT_TURN.get()
    if not turn_id:
        return
    with _LOCK:
        t0 = _TURN_T0.pop(turn_id, None)
        _TURN_SEQ.pop(turn_id, None)
        meta = _TURN_META.pop(turn_id, {})
        # Keep ``_TURN_BY_THREAD`` populated — the next ``start_turn``
        # for the same ``thread_id`` overwrites it. This way any late
        # events (``sse.adapter.close``, IG outbound bubbles flushed
        # after ``finish_turn``) still correlate to the right turn,
        # not show up as orphan ``turn_id=null`` lines.
    elapsed_ms = int((time.perf_counter() - t0) * 1000) if t0 else 0
    widget_types = [w.get("type") for w in (widgets or []) if isinstance(w, dict)]
    fields = {
        "status": status,
        "error": (error or "")[:300],
        "elapsed_ms": elapsed_ms,
        "text_chars": len(assistant_text or ""),
        "bubbles": bubbles,
        "widget_types": widget_types,
        "tool_calls": tool_calls,
        **{k: v for k, v in meta.items() if k != "channel"},  # for grep convenience
    }
    trace("turn.finish", _turn_id=turn_id, **fields)
    _persist_turn_finish(
        turn_id,
        {
            "finished_at": "now()",
            "status": status,
            "error": (error or "")[:1000],
            "elapsed_ms": elapsed_ms,
            "assistant_text": (assistant_text or "")[:8000],
            "widget_types": widget_types,
            "bubbles": bubbles,
            "tool_calls": tool_calls,
        },
    )
    # Don't reset ``_CURRENT_TURN``. Late events (sse.adapter.close,
    # IG outbound bubbles flushed after the run) are still part of this
    # turn's story; the next ``start_turn`` overwrites the contextvar
    # cleanly so there's no leak across turns.


def trace(event: str, *, _turn_id: str | None = None, **fields: Any) -> None:
    """Emit one trace event. Tagged with the active ``turn_id``.

    ``event`` should be ``<area>.<action>`` (e.g. ``ig.webhook``,
    ``tool.dispatch``, ``adapter.close``, ``run_turn.error``). Pass any
    JSON-serialisable kwargs as ``fields``; they end up in the
    ``fields`` JSONB column and inline in the stdout JSON line.
    """
    if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
        # Public runtime traces are operational metadata; conversation content stays in its session store.
        allowed = {"channel", "elapsed_ms", "elapsed_s", "status", "tool_calls", "history_len", "text_chars", "user_message_chars", "shared_urls", "handled", "skipped", "tool_name", "phase", "seq"}
        fields = {k: v for k, v in fields.items() if k in allowed and isinstance(v, (str, int, float, bool, type(None)))}
    turn_id = _turn_id or _CURRENT_TURN.get()
    # If still no turn_id, allow callers to pass thread_id explicitly to
    # resolve via the fallback map (Hermes worker threads).
    if not turn_id:
        thread_id = fields.get("thread_id")
        if thread_id:
            turn_id = current_turn_id(thread_id=str(thread_id))
    seq = _next_seq(turn_id) if turn_id else 0
    payload = {
        "ts": time.time(),
        "turn_id": turn_id,
        "seq": seq,
        "event": event,
        "fields": fields,
    }
    try:
        line = json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        line = json.dumps(
            {"ts": time.time(), "turn_id": turn_id, "seq": seq, "event": event, "fields_repr": repr(fields)[:500]},
            ensure_ascii=False,
        )
    logger.info("[TRACE] %s", line)
    if turn_id:
        _persist_event(turn_id, seq, event, fields)


# ── Context manager sugar ------------------------------------------------


class turn_scope:  # noqa: N801 — lowercase reads better in `with` statements
    """Convenience wrapper. ::

        with turn_scope(channel="instagram_dm", thread_id=tid, ...) as turn_id:
            ...
            trace("recipes.extract.queued", url=url)
    """

    def __init__(self, **start_kwargs: Any) -> None:
        self._kwargs = start_kwargs
        self.turn_id: str | None = None
        self._error: BaseException | None = None

    def __enter__(self) -> str:
        self.turn_id = start_turn(**self._kwargs)
        return self.turn_id

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        if exc is not None:
            finish_turn(self.turn_id, status="error", error=f"{exc_type.__name__}: {exc}")
        # finish_turn(status='ok') is the caller's responsibility on the
        # success path so they can pass assistant_text/widgets/etc.
        # We only emit a finish for the exception case.
        return False
