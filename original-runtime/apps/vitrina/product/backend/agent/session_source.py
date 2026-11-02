"""Single source of truth for Vitrina chat-thread persistence.

All channels (web SSE, Instagram DM, TikTok DM, terminal repl) read
prior conversation history from here and write each completed turn
back to the same place. The store is Supabase ``mcp.chat_threads`` /
``chat_messages`` (the frontend transcript hydrator already reads from
those tables — single source of truth).

This module deliberately does NOT use Hermes' ``gateway/session.py``
SessionStore. That store is SQLite, file-backed under ``HERMES_HOME``,
and read by exactly nobody on the Vitrina side; layering it on top
would be the kind of parallel implementation the rebuild plan is
trying to delete.

Public surface:

- :func:`load_history` — return prior turns for an agent in
  ``[{"role", "content"}]`` shape ready to feed into
  ``AIAgent.run_conversation(conversation_history=...)``.
- :func:`persist_user_turn` — record the live user message before the
  Hermes loop runs (so even a crashed turn leaves a trace).
- :func:`persist_assistant_turn` — record the assistant text + widget
  envelopes after the loop finishes successfully.

Both write methods are no-ops when ``thread_id`` is None (cron jobs,
DM warmup, etc.) so callers don't need to branch.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# ── Module loader (mcp/ is not on PYTHONPATH in repo layout) ─────────


def _load_chat_threads() -> Any:
    """Import ``mcp.chat_threads`` regardless of repo vs Docker layout.

    In Docker the agent runs from ``/app/vitrina_agent`` (we cannot
    use ``/app/agent`` because Hermes ships its own top-level
    ``agent`` package). In repo layout we resolve
    sibling ``backend/mcp/chat_threads.py`` relative to this file. We
    cache via ``sys.modules`` so subsequent calls don't re-exec.
    """
    cached = sys.modules.get("_vitrina_chat_threads")
    if cached is not None:
        return cached

    # Docker / PYTHONPATH path first.
    try:
        import chat_threads as direct  # type: ignore[import-not-found]

        sys.modules["_vitrina_chat_threads"] = direct
        return direct
    except Exception:
        pass

    mcp_dir = Path(__file__).resolve().parent.parent / "mcp"
    mcp_dir_str = str(mcp_dir)
    spec = importlib.util.spec_from_file_location(
        "_vitrina_chat_threads", mcp_dir / "chat_threads.py"
    )
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_vitrina_chat_threads"] = mod
    # chat_threads.py imports supabase_client which lives in the same mcp/
    # directory.  Temporarily add mcp/ to sys.path only for the duration of
    # exec_module, then remove it so it never shadows Hermes' `tools` package.
    _added = False
    if mcp_dir_str not in sys.path:
        sys.path.insert(0, mcp_dir_str)
        _added = True
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_vitrina_chat_threads", None)
        logger.debug("session_source: failed to import chat_threads", exc_info=True)
        return None
    finally:
        if _added and mcp_dir_str in sys.path:
            sys.path.remove(mcp_dir_str)
    return mod


# ── Public API ───────────────────────────────────────────────────────


def load_history(
    *,
    thread_id: str | None,
    user_id: str | None,
    cart_key: str | None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return prior conversation turns shaped for ``run_conversation``.

    Returns ``[]`` when there is no thread, no DB, or any read error
    (we'd rather start a fresh-looking turn than crash the request).

    The shape is ``[{"role": "user"|"assistant", "content": str}, ...]``
    in chronological order. Tool messages and persisted widget rows
    are flattened to plain text so Hermes' message-role-alternation
    invariant holds.
    """
    if not thread_id:
        return []
    mod = _load_chat_threads()
    if mod is None:
        if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
            raise RuntimeError("Configure your project session store")
        return []
    try:
        rows = mod.list_messages(
            thread_id, user_id=user_id, cart_key=cart_key, limit=limit
        )
    except Exception:
        if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
            raise
        logger.debug("load_history: list_messages failed", exc_info=True)
        return []

    history: list[dict[str, Any]] = []
    for row in rows:
        role = row.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = row.get("content")
        text = _content_to_text(content)
        if not text:
            continue
        history.append({"role": role, "content": text})
    return history


def persist_user_turn(
    *,
    thread_id: str | None,
    user_id: str | None,
    cart_key: str | None,
    text: str,
) -> None:
    """Record the live user message before the Hermes loop runs.

    Writing the user turn pre-flight means a crashed assistant turn
    still leaves a trace in the transcript, which matters for
    debugging and for restoring history on the next page load.
    """
    if not thread_id or not text:
        return
    _append(thread_id=thread_id, role="user", content=text, user_id=user_id, cart_key=cart_key)


def persist_assistant_turn(
    *,
    thread_id: str | None,
    user_id: str | None,
    cart_key: str | None,
    text: str,
    widgets: list[dict[str, Any]] | None = None,
    bubbles: list[str] | None = None,
) -> None:
    """Record the assistant text + widget envelopes after a turn ends.

    The frontend rehydrator (``useVitrinaChat.ts``) prefers
    ``bubbles`` (one entry per ``[BREAK]`` segment) when present so
    reloading a chat looks identical to the live multi-bubble stream.
    ``text`` (``\n\n``-joined) is kept for backward-compat with older
    rows / clients.
    """
    if not thread_id:
        return
    if not (text and text.strip()) and not widgets:
        return
    payload: dict[str, Any] = {
        "kind": "assistant_turn",
        "text": text,
        "widgets": widgets or [],
    }
    if bubbles:
        payload["bubbles"] = bubbles
    _append(thread_id=thread_id, role="assistant", content=payload, user_id=user_id, cart_key=cart_key)


# ── helpers ──────────────────────────────────────────────────────────


def _append(
    *,
    thread_id: str,
    role: str,
    content: Any,
    user_id: str | None,
    cart_key: str | None,
) -> None:
    mod = _load_chat_threads()
    if mod is None:
        if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
            raise RuntimeError("Configure your project session store")
        return
    try:
        mod.append_message(
            thread_id, role, content, user_id=user_id, cart_key=cart_key
        )
    except Exception:
        if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
            raise
        logger.debug("session_source: append_message failed", exc_info=True)


def _content_to_text(content: Any) -> str:
    """Flatten persisted message content to plain text for Hermes history.

    User rows store strings. Assistant rows are usually
    ``{"kind": "assistant_turn", "text": "...", "widgets": [...]}``;
    for replay we only need the text. Older rows (legacy stream
    format) sometimes stored raw OpenAI-shaped dicts — be tolerant.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str) and text.strip():
            return text
        # Some legacy rows use OpenAI's content list shape.
        parts = content.get("content")
        if isinstance(parts, list):
            joined = " ".join(
                p.get("text", "") for p in parts if isinstance(p, dict)
            ).strip()
            if joined:
                return joined
        return ""
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        ).strip()
    return ""
