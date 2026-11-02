"""Background-job helpers for the Vitrina chat agent.

Some agent tools take 30–90 s — most notably the recipe extractor
which needs to download a video, run scene detection, ship frames to a
multimodal LLM, and shop-match the result. Holding the SSE stream open
that long doesn't work in practice (Cloudflare/CDN timeouts; users
swipe away mid-extract; each retry redoes the whole thing). Instead
the agent's "async" tools enqueue a row here, return immediately with
an ack message ("On it — I'll ping you when ready") and let a
background worker post staged progress messages into
``chat_messages``.

The frontend ``useVitrinaChat.ts`` subscribes to Supabase Realtime on
``chat_messages`` for the active thread, so progress + final cards
appear live whether the user is still on the chat tab or not.

Tables touched:

* ``chat_jobs``   — see ``api/migrations/004_chat_jobs.sql``.
* ``chat_messages`` — staged "downloaded video / extracting recipe /
  matching shops / done" assistant rows posted by the worker.

Why threads, not asyncio
-----------------------

The agent's tool-dispatch loop runs in a *sync* generator inside
FastAPI's ``EventSourceResponse``. There's no live event loop in that
context to ``asyncio.create_task`` onto. A ``threading.Thread(daemon=True)``
keeps the worker decoupled from the request lifetime and works
identically on the Railway gunicorn deployment and the local
``uvicorn --reload`` dev runner. The actual extraction code is sync
already (it shells out to ``yt-dlp`` and ``ffmpeg`` via subprocess), so
no async wrapping is needed.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from supabase_client import get_supabase  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    def get_supabase():  # type: ignore[no-redef]
        raise RuntimeError("supabase_not_configured")


logger = logging.getLogger("vitrina.mcp.chat_jobs")

_TABLE = "chat_jobs"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── CRUD ──────────────────────────────────────────────────────────────


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _coerce_user_id(value: str | None) -> str | None:
    """Coerce a candidate ``user_id`` into a uuid-or-null for Postgres.

    ``chat_jobs.user_id`` is a Supabase ``uuid`` column. Anonymous
    Instagram shoppers don't have a Supabase user — only a
    ``cart_key="ig:{psid}"`` — and the model occasionally hallucinates
    ``user_id="ig:..."`` from the cart_key it sees in context. Inserting
    that string crashes the row with PostgreSQL ``22P02``. Treat any
    non-UUID-shaped ``user_id`` as anonymous; the cart_key column stays
    populated so the correlation is still usable.
    """
    if not value:
        return None
    if isinstance(value, str) and _UUID_RE.match(value):
        return value
    return None


def enqueue(
    *,
    thread_id: str,
    kind: str,
    payload: dict[str, Any],
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Insert a ``queued`` row and return it. Caller spawns the worker."""
    sb = get_supabase()
    row = {
        "id": str(uuid.uuid4()),
        "thread_id": thread_id,
        "user_id": _coerce_user_id(user_id),
        "cart_key": cart_key,
        "kind": kind,
        "status": "queued",
        "payload": payload,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    inserted = sb.table(_TABLE).insert(row).execute()
    return inserted[0] if isinstance(inserted, list) and inserted else (inserted or row)


def mark_running(job_id: str) -> None:
    _patch(job_id, {"status": "running", "updated_at": _now_iso()})


def mark_succeeded(job_id: str, result: dict[str, Any]) -> None:
    _patch(
        job_id,
        {
            "status": "succeeded",
            "result": result,
            "updated_at": _now_iso(),
            "finished_at": _now_iso(),
        },
    )


def mark_failed(job_id: str, error_message: str) -> None:
    _patch(
        job_id,
        {
            "status": "failed",
            "error_message": error_message,
            "updated_at": _now_iso(),
            "finished_at": _now_iso(),
        },
    )


def get(job_id: str) -> dict[str, Any] | None:
    sb = get_supabase()
    try:
        row = sb.table(_TABLE).select("*").eq("id", job_id).single().execute()
    except Exception:  # noqa: BLE001
        return None
    return row or None


def _patch(job_id: str, patch: dict[str, Any]) -> None:
    try:
        sb = get_supabase()
        sb.table(_TABLE).update(patch).eq("id", job_id).execute()
    except Exception:  # noqa: BLE001
        logger.warning("chat_jobs.patch_failed job_id=%s", job_id)


# ── Worker plumbing ───────────────────────────────────────────────────


def post_assistant_message(
    *,
    thread_id: str,
    text: str,
    widgets: list[dict[str, Any]] | None = None,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any] | None:
    """Append a structured assistant message to ``chat_messages``.

    Same shape :func:`agent.session._persist_assistant_turn` writes, so
    rehydration (``useVitrinaChat`` → ``widgetsFromPersistedContent``)
    handles progress messages identically to in-flight turns.
    """
    try:
        # Local import — chat_threads imports get_supabase too; we
        # avoid an upfront cross-import to keep test seams loose.
        import chat_threads  # type: ignore[import-not-found]

        out = chat_threads.append_message(
            thread_id,
            "assistant",
            {"kind": "assistant_turn", "text": text, "widgets": widgets or []},
            user_id=user_id,
            cart_key=cart_key,
        )
        _mirror_instagram_progress_dm(cart_key, text)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat_jobs.post_message_failed thread_id=%s err=%s", thread_id, exc)
        return None


def _mirror_instagram_progress_dm(cart_key: str | None, text: str) -> None:
    """Background recipe-extract stages only hit Supabase — mirror plain lines to IG DMs."""
    if not cart_key or not str(cart_key).startswith("ig:"):
        return
    psid = str(cart_key).removeprefix("ig:")
    try:
        from platforms.instagram_dm import notify_shopper_text  # type: ignore
    except ImportError:
        try:
            from agent.platforms.instagram_dm import notify_shopper_text  # type: ignore
        except ImportError:
            logger.warning(
                "chat_jobs: IG progress DM skipped — cannot import notify_shopper_text "
                "(MCP PYTHONPATH must include the agent tree so `platforms` resolves; "
                "see agent_factory._MCP_PYTHONPATH_CONTAINER / _resolve_mcp_command)"
            )
            return
    try:
        notify_shopper_text(psid, text)
    except Exception:
        logger.debug("mirror_instagram_dm failed", exc_info=True)


def spawn(target: Callable[[], None], *, name: str) -> threading.Thread:
    """Daemon-thread spawn helper so the agent doesn't have to know
    threading details. Returns the thread (mostly for tests)."""
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


__all__ = [
    "enqueue",
    "mark_running",
    "mark_succeeded",
    "mark_failed",
    "get",
    "post_assistant_message",
    "spawn",
]
