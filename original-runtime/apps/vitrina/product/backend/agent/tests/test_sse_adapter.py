"""SSE adapter wire-frame tests.

The legacy ``test_stream.py`` asserted the AI-SDK SSE shape was exactly
what the frontend's ``useChat`` hook expects. The Hermes-backed agent
keeps that wire contract intact; the only thing that changed is who
emits the events (Hermes callbacks vs. a hand-rolled OpenRouter
translator). These tests pin the SSE shape so any future regression
(e.g. a Hermes upgrade adding extra callback args) is caught here.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import sse_adapter


# ── Frame shape ───────────────────────────────────────────────────────


def test_sse_frame_format() -> None:
    out = sse_adapter.sse_frame({"type": "text-delta", "id": "msg_1", "delta": "hi"})
    assert out == b'data: {"type":"text-delta","id":"msg_1","delta":"hi"}\n\n'


def test_sse_done_terminator() -> None:
    assert sse_adapter.sse_done() == b"data: [DONE]\n\n"


def test_event_factory_shapes() -> None:
    assert sse_adapter.text_delta("m1", "x") == {
        "type": "text-delta",
        "id": "m1",
        "delta": "x",
    }
    assert sse_adapter.tool_call("tc1", "add_item", {"sku": "X"}) == {
        "type": "tool-call",
        "toolCallId": "tc1",
        "toolName": "add_item",
        "args": {"sku": "X"},
    }
    assert sse_adapter.tool_result("tc1", {"ok": True}) == {
        "type": "tool-result",
        "toolCallId": "tc1",
        "result": {"ok": True},
    }
    assert sse_adapter.data_widget("pay_cta", {"amount": 10}) == {
        "type": "data-widget",
        "data": {"widget": "pay_cta", "payload": {"amount": 10}},
    }
    assert sse_adapter.finish("stop") == {"type": "finish", "finishReason": "stop"}
    assert sse_adapter.error("nope") == {"type": "error", "error": "nope"}


# ── Callback round-trip (text only) ──────────────────────────────────


def _drain(adapter: sse_adapter.HermesSSEAdapter) -> list[Any]:
    async def _go() -> list[Any]:
        return [chunk async for chunk in adapter.stream()]

    frames = asyncio.run(_go())
    decoded: list[Any] = []
    for f in frames:
        text = f.decode("utf-8")
        assert text.startswith("data: ") and text.endswith("\n\n")
        payload = text[len("data: "): -2]
        if payload == "[DONE]":
            decoded.append("[DONE]")
        else:
            decoded.append(json.loads(payload))
    return decoded


def test_text_deltas_share_message_id_and_terminate() -> None:
    a = sse_adapter.HermesSSEAdapter()
    a.on_stream_delta("Hello ")
    a.on_stream_delta("world")
    a.close("stop")
    decoded = _drain(a)
    deltas = [e for e in decoded if isinstance(e, dict) and e["type"] == "text-delta"]
    assert "".join(d["delta"] for d in deltas) == "Hello world"
    ids = {d["id"] for d in deltas}
    assert len(ids) == 1
    assert decoded[-2] == {"type": "finish", "finishReason": "stop"}
    assert decoded[-1] == "[DONE]"


def test_tool_call_then_result_with_widget_extraction() -> None:
    a = sse_adapter.HermesSSEAdapter()
    a.on_tool_start("tc_1", "basket__snapshot", {"cart_key": "k"})
    a.on_tool_complete(
        "tc_1",
        "basket__snapshot",
        {"cart_key": "k"},
        {
            "items": [{"qty": 2}],
            "total_eur": 8.4,
            "_widget": {"type": "basket_summary", "payload": {"summary": "2 items"}},
        },
    )
    a.close("stop")
    decoded = _drain(a)
    types = [e["type"] for e in decoded if isinstance(e, dict)]
    assert types[0] == "tool-call"
    assert types[1] == "tool-result"
    assert types[2] == "data-widget"
    widget_event = next(e for e in decoded if isinstance(e, dict) and e["type"] == "data-widget")
    assert widget_event["data"]["widget"] == "basket_summary"
    assert widget_event["data"]["payload"]["summary"] == "2 items"


def test_failure_emits_error_then_finish_then_done() -> None:
    a = sse_adapter.HermesSSEAdapter()
    a.on_stream_delta("started")
    a.fail(RuntimeError("kaboom"))
    decoded = _drain(a)
    assert {"type": "error", "error": "kaboom"} in decoded
    assert decoded[-2]["type"] == "finish"
    assert decoded[-1] == "[DONE]"


def test_silent_widget_only_turn_gets_followup_text() -> None:
    """If the model emits a widget but no text, the adapter injects a
    one-liner before finish so the user always sees an assistant
    bubble."""
    a = sse_adapter.HermesSSEAdapter()
    a.on_tool_complete(
        "tc_1",
        "recipes__add_to_basket",
        {},
        {"ok": True, "_widget": {"type": "added_receipt", "payload": {"sku": "X"}}},
    )
    a.close("stop")
    decoded = _drain(a)
    deltas = [e for e in decoded if isinstance(e, dict) and e["type"] == "text-delta"]
    assert deltas, "expected the safety-net follow-up to land before finish"
    text = "".join(d["delta"] for d in deltas)
    assert "Added" in text or "Anything" in text or "anything" in text


def test_no_followup_when_model_already_streamed_text() -> None:
    a = sse_adapter.HermesSSEAdapter()
    a.on_stream_delta("Done.")
    a.on_tool_complete(
        "tc_2",
        "recipes__add_to_basket",
        {},
        {"_widget": {"type": "added_receipt", "payload": {}}},
    )
    a.close("stop")
    decoded = _drain(a)
    deltas = [e for e in decoded if isinstance(e, dict) and e["type"] == "text-delta"]
    assert "".join(d["delta"] for d in deltas) == "Done."


def test_thinking_callback_emits_data_status() -> None:
    a = sse_adapter.HermesSSEAdapter()
    a.on_thinking()
    a.close("stop")
    decoded = _drain(a)
    statuses = [e for e in decoded if isinstance(e, dict) and e["type"] == "data-status"]
    assert statuses
    assert statuses[0]["data"]["kind"] == "thinking"


def test_tool_progress_humanizes_tool_name() -> None:
    a = sse_adapter.HermesSSEAdapter()
    # Mirror the Hermes 2026-04 callback shape: ``(event_type, name, preview, args)``.
    a.on_tool_progress("tool.started", "recipes.stretch_basket", None, {})
    a.on_tool_progress("tool.started", "dev.ucp.shopping.cart.add_item", None, {})
    a.close("stop")
    decoded = _drain(a)
    statuses = [
        e for e in decoded if isinstance(e, dict) and e["type"] == "data-status"
    ]
    msgs = [s["data"]["message"] for s in statuses]
    assert any("Looking" in m or "stretch" in m.lower() for m in msgs)
    assert any("basket" in m.lower() for m in msgs)


def test_tool_progress_completed_event_is_silent() -> None:
    """Completed events come with their own duration/is_error fields but
    we don't render them — :class:`HermesSSEAdapter` is the place that
    decides what shows up in the chat, not Hermes."""

    a = sse_adapter.HermesSSEAdapter()
    a.on_tool_progress(
        "tool.completed", "recipes.stretch_basket", None, None,
        duration=1.2, is_error=False,
    )
    a.close("stop")
    decoded = _drain(a)
    statuses = [
        e for e in decoded if isinstance(e, dict) and e["type"] == "data-status"
    ]
    assert not statuses


def test_string_result_with_widget_envelope_parses() -> None:
    """Tool returns are JSON strings out of the trampoline — widget
    extraction must still find the envelope."""
    a = sse_adapter.HermesSSEAdapter()
    a.on_tool_complete(
        "tc_x",
        "anything",
        {},
        json.dumps({"ok": True, "_widget": {"type": "added_receipt", "payload": {"sku": "X"}}}),
    )
    a.close("stop")
    decoded = _drain(a)
    widget_events = [e for e in decoded if isinstance(e, dict) and e["type"] == "data-widget"]
    assert widget_events == [
        {"type": "data-widget", "data": {"widget": "added_receipt", "payload": {"sku": "X"}}}
    ]
