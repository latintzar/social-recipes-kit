"""Channel-agnostic turn runner.

Every channel — web SSE, Instagram DM, TikTok DM, terminal repl —
runs the same Hermes turn through this single function. The pieces
that vary across channels (how the user message arrives, what the
"caller" does with the assistant text + widgets) live in the
channel's own adapter; everything in between is identical.

```
            ┌─────────────────────────────────────────┐
            │  POST /v1/chat/stream  (web SSE)        │
            │  POST /v1/instagram/webhook (IG DMs)    │
            │  POST /v1/tiktok/webhook    (TikTok)    │
            │  python -m agent.repl  (terminal)       │
            └────────────┬────────────────────────────┘
                         │  channel.run_turn(...)
                         ▼
                ┌────────────────────────┐
                │  session_source.load   │  Supabase mcp.chat_threads
                │  session_bridge.build  │
                │  agent_factory.build   │  Hermes AIAgent + mem0
                │  HermesSSEAdapter      │  callbacks → caller's queue
                │  agent.run_conversation│  THE LOOP
                │  session_source.persist│
                └────────────────────────┘
```

Public surface:

- :func:`run_turn` — synchronous, runs the Hermes loop directly.
  Channel adapters that need to stream call it from a background
  thread and consume the adapter's queue (web SSE) or wait for
  completion and use ``adapter.assistant_text`` / ``adapter.widgets``
  (DMs, repl).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

try:  # repo layout
    from . import agent_factory, session_bridge, session_source, tracing
    from .sse_adapter import HermesSSEAdapter
except ImportError:  # uvicorn invoked from /app/vitrina_agent
    import agent_factory  # type: ignore
    import session_bridge  # type: ignore
    import session_source  # type: ignore
    import tracing  # type: ignore
    from sse_adapter import HermesSSEAdapter  # type: ignore


logger = logging.getLogger(__name__)

_DM_HISTORY_LIMIT = 8
_DEFAULT_HISTORY_LIMIT = 40


@dataclass
class TurnResult:
    """Final state of a turn after the Hermes loop returns."""

    assistant_text: str
    widgets: list[dict[str, Any]]
    error: str | None = None
    tool_calls: int = 0


def run_turn(
    *,
    channel: str,
    messages: list[dict[str, Any]] | None = None,
    user_message: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
    memory_user_id: str | None = None,
    cart_key: str | None = None,
    cart_ids: list[str] | None = None,
    user_location: dict[str, Any] | None = None,
    adapter: HermesSSEAdapter | None = None,
    ephemeral_extra: str | None = None,
) -> TurnResult:
    """Run one Hermes turn and return the assistant text + widgets.

    Two input shapes are supported so every channel can use the same
    function:

    - ``messages=[...]`` — the full chat as the channel knows it
      (same shape the web SSE route uses today). Prior history is
      taken from the messages list; recent Supabase rows are used
      only if the messages list is empty (gateway warmup, etc.).

    - ``user_message="..."`` + (optional) ``thread_id`` — the
      simpler shape for DMs and the repl. Prior history is loaded
      from Supabase ``mcp.chat_threads`` automatically.

    The caller may pass an explicit :class:`HermesSSEAdapter`. The
    web SSE route does this so it can yield bytes from the adapter's
    queue concurrently with the Hermes thread; DMs and the repl let
    us instantiate one and just consume the final state.
    """
    adapter = adapter or HermesSSEAdapter()

    # Mem0 / Hermes identity: Instagram channels pass a stable string like
    # ``ig:{psid}`` here while keeping ``user_id=None`` so Supabase cart
    # rows stay anonymous (UUID ``carts.user_id`` cannot hold IG handles).
    mem_uid = memory_user_id or user_id

    # Start a turn id IF the caller didn't already (IG adapter starts its
    # own at webhook time so the IG-specific pre-work is part of the trace).
    turn_id = tracing.current_turn_id(thread_id=thread_id)
    started_locally = turn_id is None
    if started_locally:
        turn_id = tracing.start_turn(
            channel=channel,
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            user_message=user_message or (
                latest := session_bridge.latest_user_text(messages or [])
            ) or "",
        )

    # ── Build the conversation envelope ─────────────────────────────
    channel_messages = list(messages or [])
    if user_message is not None:
        # DM/repl shape: synthesize the messages list with persisted history.
        history_limit = _DM_HISTORY_LIMIT if channel.endswith("_dm") else _DEFAULT_HISTORY_LIMIT
        history = session_source.load_history(
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            limit=history_limit,
        )
        channel_messages = list(history) + [{"role": "user", "content": user_message}]

    turn = session_bridge.build_turn(
        messages=channel_messages,
        channel=channel,
        thread_id=thread_id,
        user_id=user_id,
        cart_key=cart_key,
        cart_ids=cart_ids,
        user_location=user_location,
        ephemeral_extra=ephemeral_extra,
    )
    tracing.trace(
        "run_turn.built",
        thread_id=thread_id,
        history_len=len(turn.conversation_history),
        sys_msg_chars=len(turn.system_message or ""),
        ephem_chars=len(turn.ephemeral_system_prompt or ""),
        user_message_chars=len(turn.user_message or ""),
        has_user_location=bool(user_location),
    )

    # Persist the user turn pre-flight so a crash mid-loop still
    # leaves a transcript (helps debug the "still on it" bug).
    session_source.persist_user_turn(
        thread_id=thread_id,
        user_id=user_id,
        cart_key=cart_key,
        text=turn.user_message,
    )

    agent = agent_factory.build_agent(
        session_id=thread_id,
        user_id=mem_uid,
        stream_delta_callback=adapter.on_stream_delta,
        tool_start_callback=adapter.on_tool_start,
        tool_complete_callback=adapter.on_tool_complete,
        thinking_callback=adapter.on_thinking,
        reasoning_callback=adapter.on_reasoning,
        step_callback=adapter.on_step,
        tool_progress_callback=adapter.on_tool_progress,
        status_callback=adapter.on_status,
        clarify_callback=adapter.on_clarify,
    )
    if agent is None:
        # No LLM credentials configured — fall through to the stub
        # reply so the frontend at least gets a "configure your keys"
        # bubble instead of a silent timeout.
        adapter.push_text(agent_factory.stub_reply(turn.user_message))
        session_source.persist_assistant_turn(
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            text=adapter.assistant_text,
            widgets=adapter.widgets,
        )
        return TurnResult(
            adapter.assistant_text,
            adapter.widgets,
            error=None,
            tool_calls=getattr(adapter, "tool_call_count", 0),
        )

    error: str | None = None
    try:
        # ``ephemeral_system_prompt`` is an ``AIAgent`` constructor
        # kwarg, not a ``run_conversation`` kwarg. Setting it on the
        # instance before the call has the same effect.
        agent.ephemeral_system_prompt = turn.ephemeral_system_prompt
        import time as _time
        _t0 = _time.perf_counter()
        tracing.trace(
            "run_conversation.start",
            thread_id=thread_id,
            provider=getattr(agent, "provider", None),
            model=getattr(agent, "model", None),
            max_iterations=getattr(agent, "max_iterations", None),
        )
        agent.run_conversation(
            user_message=turn.user_message,
            system_message=turn.system_message,
            conversation_history=turn.conversation_history,
        )
        tracing.trace(
            "run_conversation.done",
            thread_id=thread_id,
            elapsed_ms=int((_time.perf_counter() - _t0) * 1000),
            text_chars=len(adapter.assistant_text or ""),
            widget_types=[w.get("type") for w in (adapter.widgets or [])],
            bubbles=len(adapter.assistant_bubbles or []),
        )
    except Exception as exc:  # noqa: BLE001
        import sys as _sys, traceback as _tb
        tracing.trace(
            "run_conversation.error",
            thread_id=thread_id,
            exc_type=type(exc).__name__,
            exc=str(exc)[:300],
        )
        logger.error(
            "hermes turn failed — sys.path[0:3]=%s tools_in_modules=%s\n%s",
            _sys.path[:3],
            "tools" in _sys.modules,
            _tb.format_exc(),
        )
        error = str(exc)
        adapter.fail(exc)
    finally:
        if os.getenv("SOCIAL_RECIPES_RUNTIME") == "1":
            agent.close()

    # Persist the assistant turn (text + widgets + bubbles) so the
    # next page load reconstructs the bubble correctly. ``bubbles``
    # mirrors the live [BREAK]-split stream — without it, rehydration
    # would smash the multi-bubble turn into a single fat bubble and
    # widgets would float above their explanatory sentence.
    session_source.persist_assistant_turn(
        thread_id=thread_id,
        user_id=user_id,
        cart_key=cart_key,
        text=adapter.assistant_text,
        widgets=adapter.widgets,
        bubbles=adapter.assistant_bubbles,
    )

    if started_locally:
        # IG starts the turn at webhook time and finishes after delivery,
        # so we only finish here on web SSE / repl / TikTok DM.
        bubbles = len(adapter.assistant_bubbles or [])
        text = adapter.assistant_text or ""
        if error:
            status = "error"
        elif not text.strip() and not adapter.widgets:
            status = "silent"
        else:
            status = "ok"
        tracing.finish_turn(
            turn_id,
            status=status,
            error=error,
            assistant_text=text,
            widgets=adapter.widgets,
            bubbles=bubbles,
            tool_calls=getattr(adapter, "tool_call_count", 0),
        )

    return TurnResult(
        adapter.assistant_text,
        adapter.widgets,
        error=error,
        tool_calls=getattr(adapter, "tool_call_count", 0),
    )
