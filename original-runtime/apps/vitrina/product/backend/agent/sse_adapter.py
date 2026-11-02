"""Hermes -> AI-SDK v5 SSE bridge.

Hermes' :class:`run_agent.AIAgent` exposes a synchronous callback surface
(``stream_delta_callback``, ``tool_start_callback``, ``tool_complete_callback``,
``thinking_callback``, ``reasoning_callback``, ``step_callback``,
``tool_progress_callback``, ``status_callback``, ``clarify_callback``)
that fires inside its tool-calling loop.

The Vitrina frontend speaks the Vercel AI-SDK ``useChat`` SSE shape —
``text-delta`` / ``tool-call`` / ``tool-result`` / ``data-widget`` /
``finish`` / ``error`` / ``[DONE]``. We add ``data-status`` for the
ambient progress lines that come out of Hermes' reasoning callbacks
so the chat never goes silent during a slow turn.

This module is the only translator between the two. Everything else
(``server.py``, ``agent_factory.py``) stays Hermes-shaped on one side and
AI-SDK-shaped on the other.

Design notes:

- **Every Hermes callback is wired.** Even when we don't render the
  output (debug-only ``reasoning_callback`` for now), we still receive
  it so we can lift it into the UI later without re-deploying the
  agent.
- **Silent-turn safety net.** If Hermes ends a turn that produced
  widgets but zero stream-delta text (the model emitted only tool
  calls and considered itself done), :meth:`close` injects a single
  short follow-up sentence so the user always sees a bubble. This
  belts-and-braces SKILL.md, which also tells the model to write a
  one-liner after every widget — but model behaviour drifts and the
  safety net catches drifts.

Wire format reference: ``apps/vitrina/product/web/src/chat/useChatStream.ts``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
import traceback
import uuid
from typing import Any, AsyncIterator, Callable

try:
    from . import tracing
except ImportError:  # uvicorn invoked from /app/vitrina_agent
    import tracing  # type: ignore


logger = logging.getLogger(__name__)


def _json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def sse_frame(event: dict[str, Any]) -> bytes:
    """Encode one AI-SDK event dict as a ``data: <json>\\n\\n`` SSE frame."""
    return f"data: {_json(event)}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:16]}"


_BREAK = "[BREAK]"


def _partial_break_holdback(buf: str) -> int:
    """How many trailing chars of *buf* might still complete ``[BREAK]``."""
    if not buf:
        return 0
    tok = _BREAK
    for i in range(min(len(buf), len(tok) - 1), 0, -1):
        suf = tok[:i]
        if buf.endswith(suf):
            return i
    return 0


# ── event factories (frontend wire shape) ─────────────────────────────


def text_delta(msg_id: str, delta: str) -> dict[str, Any]:
    return {"type": "text-delta", "id": msg_id, "delta": delta}


def tool_call(tool_call_id: str, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool-call",
        "toolCallId": tool_call_id,
        "toolName": tool_name,
        "args": args,
    }


def tool_result(tool_call_id: str, result: Any) -> dict[str, Any]:
    return {"type": "tool-result", "toolCallId": tool_call_id, "result": result}


def data_widget(widget: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "data-widget", "data": {"widget": widget, "payload": payload}}


def data_status(message: str, kind: str = "progress") -> dict[str, Any]:
    """Ambient progress strip ("Checking shops…", "Thinking…").

    The frontend's ``useVitrinaChat`` consumer ignores unknown event
    types, so adding ``data-status`` is forward-compatible. Once the
    UI starts rendering it (separate frontend PR), the agent already
    has the right wire format flowing.
    """
    return {"type": "data-status", "data": {"kind": kind, "message": message}}


def finish(reason: str = "stop") -> dict[str, Any]:
    return {"type": "finish", "finishReason": reason}


def error(msg: str) -> dict[str, Any]:
    return {"type": "error", "error": msg}


# ── widget extraction ─────────────────────────────────────────────────


def _extract_widget(result: Any) -> dict[str, Any] | None:
    """Pull ``_widget`` envelope out of a tool result (dict or JSON string)."""
    parsed: Any = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            return None
    if not isinstance(parsed, dict):
        return None
    widget = parsed.get("_widget")
    if not isinstance(widget, dict):
        return None
    wtype = widget.get("type")
    payload = widget.get("payload") or {}
    if not isinstance(wtype, str) or not isinstance(payload, dict):
        return None
    return {"type": wtype, "payload": payload}


# ── silent-turn safety net ────────────────────────────────────────────


def _default_followup(widgets: list[dict[str, Any]]) -> str:
    """Pick a sensible one-liner for a turn that ended with widgets only.

    Keep these conservative: anything more specific risks contradicting
    what the model just did. The follow-up's job is "don't leave the
    user staring at nothing", not "summarize the action".
    """
    if not widgets:
        return ""
    last = widgets[-1].get("type", "")
    if last == "added_receipt":
        return "Added — anything else?"
    if last in {"basket", "basket_summary"}:
        return "Here's your basket. Want to keep going or check out?"
    if last in {"pay", "pay_cta"}:
        return "Tap pay when you're ready."
    if last == "stretch_card":
        return "Want me to add this on?"
    if last == "replan_proposal":
        return "Pick one and I'll update the basket."
    if last == "clarify_question":
        return "Let me know which works for you."
    if last == "shop_card":
        return "Let me know which one — or tell me what you're after."
    if last in {"product_card", "shop_product_card"}:
        return "Want me to add it to your basket?"
    if last == "recipe_video_card":
        return "Want me to add the ingredients to your basket?"
    return "Done — what's next?"


# ── adapter ───────────────────────────────────────────────────────────


class HermesSSEAdapter:
    """Bridge a single Hermes ``run_conversation`` call into an SSE stream.

    The Hermes loop is synchronous — it runs in a background thread.
    Each callback pushes an AI-SDK event into a thread-safe queue;
    :meth:`stream` reads from the queue and yields SSE bytes until the
    Hermes thread signals completion (or an exception bubbles up).
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: queue.Queue[Any] = queue.Queue()
        self._msg_id = new_message_id()
        self._final_text: list[str] = []
        self._raw_chunks: list[str] = []
        self._pending = ""
        self._collected_widgets: list[dict[str, Any]] = []
        self._tool_call_count = 0
        # Widgets queued by ``on_tool_complete`` but deferred until the
        # next text bubble closes (or the turn finishes), so the
        # explanatory sentence ("Found 3 shops…") always lands BEFORE
        # the visual card on the wire. Hermes' tool-call loop fires
        # ``tool_complete`` the instant the tool returns; without this
        # buffer the data-widget frame races the next text-delta to
        # the frontend and the user sees the card pop in mid-sentence.
        self._deferred_widgets: list[tuple[str, str, dict[str, Any]]] = []
        self._sent_text_since_widget = False
        self._stream_reasoning = (
            os.environ.get("VITRINA_AGENT_STREAM_REASONING", "") == "1"
        )
        self._last_status_emit = 0.0
        # Suppress repeats of the same status message: Hermes can fire
        # ``thinking_callback`` repeatedly during one model call.
        self._last_status_text: str = ""

    # ── Hermes-shaped callbacks ─────────────────────────────────────

    def on_stream_delta(self, delta: str | None) -> None:
        # Hermes calls with ``None`` to indicate end-of-stream for one
        # assistant turn segment. Split on the literal ``[BREAK]`` token so
        # each chunk becomes a separate assistant bubble (new ``msg_id``).
        if not delta:
            return
        self._raw_chunks.append(delta)
        self._pending += delta
        while _BREAK in self._pending:
            before, _, after = self._pending.partition(_BREAK)
            self._pending = after
            had_text = bool(before)
            if had_text:
                self._final_text.append(before)
                self._q.put(text_delta(self._msg_id, before))
                self._sent_text_since_widget = True
            # Only release deferred widgets at a BREAK that closed a
            # bubble containing real text. A leading BREAK (empty
            # `before`) would race the next text-delta to the wire,
            # making the card pop in mid-sentence.
            if had_text:
                self._flush_deferred_widgets()
            self._msg_id = new_message_id()
        hold = _partial_break_holdback(self._pending)
        safe_len = len(self._pending) - hold
        if safe_len > 0:
            chunk = self._pending[:safe_len]
            self._pending = self._pending[safe_len:]
            self._final_text.append(chunk)
            self._q.put(text_delta(self._msg_id, chunk))
            self._sent_text_since_widget = True

    def on_tool_start(self, tool_call_id: str, tool_name: str, args: Any) -> None:
        self._tool_call_count += 1
        if isinstance(args, str):
            try:
                args_dict = json.loads(args)
            except (TypeError, ValueError):
                args_dict = {"_raw": args}
        elif isinstance(args, dict):
            args_dict = args
        else:
            args_dict = {}
        self._q.put(tool_call(tool_call_id, tool_name, args_dict))

    def on_tool_complete(
        self,
        tool_call_id: str,
        tool_name: str,
        args: Any,
        result: Any,
    ) -> None:
        # Frontend wants a JSON-serialisable result — never the raw
        # function-return object.
        try:
            json.dumps(result)
            sane_result = result
        except (TypeError, ValueError):
            try:
                sane_result = json.loads(result) if isinstance(result, str) else str(result)
            except (TypeError, ValueError):
                sane_result = str(result)
        self._q.put(tool_result(tool_call_id, sane_result))
        widget = _extract_widget(sane_result)
        if widget:
            self._collected_widgets.append(widget)
            # Defer until the next [BREAK]/segment-break boundary so the
            # explanatory bubble streams BEFORE the visual card on the
            # wire. Without buffering, ``on_tool_complete`` enqueues
            # ``data-widget`` immediately and the next ``text-delta``
            # racing back from the model arrives second on the
            # frontend — making the card pop in mid-sentence.
            self._deferred_widgets.append(
                (widget["type"], widget.get("payload") or {}, widget)
            )
            self._sent_text_since_widget = False

    # ── Reasoning surface (the previously-dropped half of the API) ──

    def on_thinking(self, message: str | None = None, **_: Any) -> None:
        """Hermes is mid-reflection. Emit a soft "Thinking…" status.

        Hermes calls this with no payload most of the time; surface a
        generic label rather than nothing so the user sees the agent
        is alive.
        """
        text = message if isinstance(message, str) and message.strip() else "Thinking…"
        self._emit_status(text, kind="thinking")

    def on_reasoning(self, chunk: str | None) -> None:
        """Optional: stream raw chain-of-thought.

        Off by default — these chunks are noisy and not always helpful
        to end users. Toggle via ``VITRINA_AGENT_STREAM_REASONING=1``
        when debugging a stuck turn.
        """
        if not chunk or not self._stream_reasoning:
            return
        self._q.put(data_status(str(chunk).strip(), kind="reasoning"))

    def on_step(self, *args: Any, **kwargs: Any) -> None:
        """Tool-loop step boundary. Surface the step label if present.

        Hermes' signature varies (kwargs include ``label`` /
        ``description`` depending on the version), so we accept
        anything and pull whichever string we can find.
        """
        label = kwargs.get("label") or kwargs.get("description")
        if not label:
            for arg in args:
                if isinstance(arg, str) and arg.strip():
                    label = arg
                    break
        if not label:
            return
        self._emit_status(str(label), kind="step")

    def on_tool_progress(
        self,
        event_type: str = "",
        tool_name: str | None = None,
        preview: str | None = None,
        args: Any = None,
        *,
        duration: float | None = None,
        is_error: bool = False,
        **_: Any,
    ) -> None:
        """Hermes ``tool_progress_callback`` — shows "Checking <tool>…" status.

        Hermes 2026-04 calls this with a positional ``event_type`` lead
        (``"tool.started"`` / ``"tool.completed"`` / ``"_thinking"`` /
        ``"reasoning.available"``) followed by ``(tool_name, preview, args,
        *, duration, is_error)``. We only render the started event as an
        ambient status pill — completed and reasoning events are noisier
        than useful in the chat UI right now (their data shows up via
        ``tool_complete_callback`` and ``reasoning_callback`` already).
        """
        if event_type and event_type != "tool.started":
            return
        if not tool_name:
            return
        pretty = _humanize_tool_name(tool_name)
        text = f"{pretty}…" if not preview else f"{pretty}: {preview}"
        self._emit_status(text, kind="tool")

    def on_status(self, *args: Any, **_: Any) -> None:
        """Lifecycle / compression / rate-limit notices from Hermes.

        We don't render every one (compression warnings would be
        noisy); just keep them in the log for debugging.
        """
        try:
            logger.debug("hermes status: %s", args)
        except Exception:
            pass

    def on_clarify(self, question: str = "", choices: list[str] | None = None, **_: Any) -> Any:
        """Clarification path — not used in Vitrina yet.

        Hermes will only call this if the model invokes the ``clarify``
        tool. Vitrina never enables that toolset (we use
        ``replan.ask_user_constraints`` instead, which renders a real
        widget). If we ever do, returning ``None`` re-raises on the
        Hermes side — we'd need a real interactive flow then.
        """
        logger.debug("hermes clarify_callback fired: %s", question)
        return None

    # ── lifecycle ──────────────────────────────────────────────────

    def push_text(self, text: str) -> None:
        """Emit a non-LLM lead-in / status line as a text delta."""
        if not text:
            return
        self._raw_chunks.append(text)
        self._final_text.append(text)
        self._q.put(text_delta(self._msg_id, text))

    def push_event(self, event: dict[str, Any]) -> None:
        self._q.put(event)

    def _flush_deferred_widgets(self) -> None:
        if not self._deferred_widgets:
            return
        for w_type, payload, _w in self._deferred_widgets:
            self._q.put(data_widget(w_type, payload))
        self._deferred_widgets.clear()

    def close(self, reason: str = "stop") -> None:
        if self._pending:
            self._final_text.append(self._pending)
            self._q.put(text_delta(self._msg_id, self._pending))
            self._pending = ""
            self._sent_text_since_widget = True
        self._flush_deferred_widgets()
        injected_fallback = False
        # Silent-turn safety net: if the Hermes loop ended with widgets
        # but no streamed text, the user would see only a card with no
        # bubble — which reads as "the agent never replied". Inject a
        # one-line follow-up before the finish frame so every turn has
        # at least one assistant sentence in the chat.
        if (
            not "".join(self._raw_chunks).replace(_BREAK, "").strip()
            and self._collected_widgets
        ):
            fallback = _default_followup(self._collected_widgets)
            if fallback:
                self.push_text(fallback)
                injected_fallback = True
        tracing.trace(
            "sse.adapter.close",
            reason=reason,
            text_chars=sum(len(c) for c in self._raw_chunks),
            widget_types=[
                w.get("type") if isinstance(w, dict) else None
                for w in self._collected_widgets
            ],
            injected_fallback=injected_fallback,
        )
        self._q.put(finish(reason))
        self._q.put(self._SENTINEL)

    def fail(self, exc: BaseException) -> None:
        tracing.trace(
            "sse.adapter.fail",
            exc_type=type(exc).__name__,
            exc=str(exc)[:200],
        )
        self._q.put(error(str(exc)))
        self._q.put(finish("error"))
        self._q.put(self._SENTINEL)

    @property
    def assistant_text(self) -> str:
        raw = "".join(self._raw_chunks)
        return raw.replace(_BREAK, "\n\n")

    @property
    def assistant_bubbles(self) -> list[str]:
        """Per-[BREAK] message bubbles, trimmed and stripped of empties.

        Used by the frontend rehydrator to recreate the live multi-bubble
        layout exactly. Returning a list (instead of a `\n\n`-joined
        string) means a model that legitimately uses paragraph breaks
        inside one bubble doesn't get artificially split on rehydration.
        """
        raw = "".join(self._raw_chunks)
        out: list[str] = []
        for part in raw.split(_BREAK):
            chunk = part.strip()
            if chunk:
                out.append(chunk)
        return out

    @property
    def widgets(self) -> list[dict[str, Any]]:
        return list(self._collected_widgets)

    @property
    def tool_call_count(self) -> int:
        return self._tool_call_count

    # ── consumer side ──────────────────────────────────────────────

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield SSE bytes until ``close()``/``fail()`` drains the queue."""
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, self._q.get)
            if item is self._SENTINEL:
                break
            yield sse_frame(item)
        yield sse_done()

    # ── helpers ───────────────────────────────────────────────────

    def _emit_status(self, message: str, *, kind: str) -> None:
        text = message.strip()
        if not text:
            return
        # Throttle: don't spam the same status more than once per second.
        now = time.monotonic()
        if text == self._last_status_text and (now - self._last_status_emit) < 1.0:
            return
        self._last_status_text = text
        self._last_status_emit = now
        self._q.put(data_status(text, kind=kind))


# ── helpers ──────────────────────────────────────────────────────────


_TOOL_LABELS = {
    "search_across_shops": "Searching shops",
    "list_products_in_shop": "Browsing the shop",
    "list_demo_shops": "Pulling up shops",
    "basket.snapshot": "Checking your basket",
    "recipes.search": "Looking up that recipe",
    "recipes.get": "Reading the recipe",
    "recipes.discover_social": "Finding ideas",
    "recipes.extract_from_url_async": "Extracting the recipe",
    "recipes.synthesize_from_brief": "Putting a recipe together",
    "recipes.stretch_basket": "Looking for stretches",
    "recipes.add_to_basket": "Adding ingredients",
    "recipes.replace_recipe_group": "Updating the recipe",
    "recipes.substitute": "Swapping that out",
}


def _humanize_tool_name(tool_name: str) -> str:
    if tool_name in _TOOL_LABELS:
        return _TOOL_LABELS[tool_name]
    if tool_name.startswith("dev.ucp.shopping.cart."):
        return "Updating your basket"
    if tool_name.startswith("replan."):
        return "Re-planning your basket"
    if tool_name.startswith("chat."):
        return "Saving the conversation"
    if tool_name.startswith("recipes."):
        return "Working on the recipe"
    # Fallback: turn ``foo.bar_baz`` into "Foo bar baz".
    label = tool_name.replace(".", " ").replace("_", " ").strip()
    return label[:1].upper() + label[1:] if label else "Working"


def run_in_thread(target: Callable[[HermesSSEAdapter], None]) -> HermesSSEAdapter:
    """Run a Hermes turn in a background thread feeding a fresh adapter."""
    adapter = HermesSSEAdapter()

    def _runner() -> None:
        try:
            target(adapter)
        except Exception as exc:  # noqa: BLE001 — surface mid-stream
            traceback.print_exc()
            adapter.fail(exc)
        else:
            adapter.close("stop")

    threading.Thread(target=_runner, daemon=True).start()
    return adapter
