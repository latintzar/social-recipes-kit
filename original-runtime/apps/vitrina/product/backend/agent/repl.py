"""Terminal repl for the Vitrina agent.

Same code path as the production web stream — :func:`channel.run_turn`
— but stdin / stdout instead of FastAPI / SSE. Use this to:

- reproduce a hung turn locally without redeploying;
- test SKILL.md edits before pushing;
- watch what mem0 actually injects (set
  ``VITRINA_AGENT_STREAM_REASONING=1``);
- iterate on the silent-turn safety net.

Usage::

    cd apps/vitrina/product/backend
    PYTHONPATH=agent:mcp:.. python -m agent.repl \\
        --thread-id smoke-2026-04-28 \\
        --user-id peter \\
        --channel terminal

Tool calls and widgets are pretty-printed inline so the conversation
stays legible. The full transcript is persisted to Supabase
``mcp.chat_threads`` exactly like the web channel — pass an existing
``--thread-id`` to resume a chat the user opened in the browser.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass
from typing import Any

try:  # repo layout
    from . import sse_adapter
    from .platforms import channel as channel_gateway
except ImportError:  # python -m agent.repl when agent/ is on PYTHONPATH
    import sse_adapter  # type: ignore
    from platforms import channel as channel_gateway  # type: ignore


# ── ANSI colours (kept tiny, no dependency) ──────────────────────────


_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_BLUE = "\x1b[34m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_MAGENTA = "\x1b[35m"
_RESET = "\x1b[0m"


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{_RESET}"


# ── Streaming consumer (drains the SSE adapter queue live) ───────────


@dataclass
class _Counts:
    text_chars: int = 0
    tool_calls: int = 0
    widgets: int = 0


def _consume(adapter: sse_adapter.HermesSSEAdapter, counts: _Counts) -> None:
    """Pull events off the adapter queue, printing them to stdout.

    Runs in a thread alongside the Hermes loop so the user sees text
    and tool calls land in real time, exactly like the web SSE route
    streams them to the frontend.
    """
    sentinel = sse_adapter.HermesSSEAdapter._SENTINEL  # private but stable
    started_text = False
    while True:
        item = adapter._q.get()  # noqa: SLF001
        if item is sentinel:
            if started_text:
                sys.stdout.write("\n")
                sys.stdout.flush()
            return
        ev_type = item.get("type") if isinstance(item, dict) else None
        if ev_type == "text-delta":
            delta = item.get("delta", "")
            if not started_text:
                sys.stdout.write(_color("\nVitrina: ", _GREEN + _BOLD))
                started_text = True
            sys.stdout.write(delta)
            sys.stdout.flush()
            counts.text_chars += len(delta)
        elif ev_type == "tool-call":
            if started_text:
                sys.stdout.write("\n")
                started_text = False
            args_preview = _short(item.get("args", {}))
            line = f"  → {item.get('toolName')}({args_preview})"
            print(_color(line, _BLUE + _DIM))
            counts.tool_calls += 1
        elif ev_type == "tool-result":
            preview = _short(item.get("result"), limit=120)
            line = f"  ← {preview}"
            print(_color(line, _DIM))
        elif ev_type == "data-widget":
            widget = item.get("data", {}).get("widget", "?")
            print(_color(f"  [widget: {widget}]", _MAGENTA))
            counts.widgets += 1
        elif ev_type == "data-status":
            data = item.get("data", {})
            kind = data.get("kind", "status")
            text = data.get("message", "")
            print(_color(f"  · {kind}: {text}", _DIM))
        elif ev_type == "error":
            print(_color(f"\n[error] {item.get('error')}", _YELLOW))
        elif ev_type == "finish":
            pass


def _short(value: Any, limit: int = 60) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except (TypeError, ValueError):
        text = str(value)
    text = text.replace("\n", " ")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


# ── Main loop ────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent.repl", description=__doc__)
    parser.add_argument("--thread-id", default=None, help="Persist to this Supabase thread.")
    parser.add_argument("--user-id", default=None, help="Mem0 user identifier.")
    parser.add_argument("--cart-key", default=None)
    parser.add_argument(
        "--channel",
        default="terminal",
        choices=["terminal", "web", "instagram_dm", "tiktok_dm"],
        help="What channel block the agent sees in its system prompt.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Wait for each turn before printing; useful for non-TTY output.",
    )
    args = parser.parse_args()

    print(
        _color(
            "Vitrina agent repl. Empty line to exit, /clear to drop history.\n",
            _DIM,
        )
    )

    while True:
        try:
            user = input(_color("You: ", _BOLD))
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user.strip():
            return 0
        if user.strip() == "/clear":
            args.thread_id = None
            print(_color("(history cleared — running with no thread_id)", _DIM))
            continue

        adapter = sse_adapter.HermesSSEAdapter()
        counts = _Counts()
        consumer = threading.Thread(
            target=_consume, args=(adapter, counts), daemon=True
        )
        consumer.start()

        def _runner() -> None:
            try:
                channel_gateway.run_turn(
                    channel=args.channel,
                    user_message=user,
                    thread_id=args.thread_id,
                    user_id=args.user_id,
                    cart_key=args.cart_key,
                    adapter=adapter,
                )
            except Exception as exc:  # noqa: BLE001
                adapter.fail(exc)
            else:
                adapter.close("stop")

        runner = threading.Thread(target=_runner, daemon=True)
        runner.start()
        runner.join()
        consumer.join()
        print(
            _color(
                f"  ({counts.text_chars} chars, {counts.tool_calls} tool calls, "
                f"{counts.widgets} widgets)",
                _DIM,
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
