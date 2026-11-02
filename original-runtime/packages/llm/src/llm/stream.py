"""
llm.stream — streaming chat (skeleton per D10)
================================================

D10 decision: spec'd, skeleton only. No production caller uses
streaming today; WhatsApp replies are one-shot. We ship the public
shape now so callers that want to stream in the future hit a stable
API contract.

Current behaviour: yields a single terminal :class:`~llm.providers.base.StreamChunk`
containing the full completion. When a caller needs *true* streaming,
implement provider-specific streaming in the provider adapters and
replace the body of :func:`stream` — the caller signature doesn't
change.
"""

from __future__ import annotations

from typing import AsyncIterator, Dict, List, Optional, Union

from llm.chat import chat
from llm.providers.base import Message, StreamChunk, Tool


async def stream(
    messages_or_prompt: Union[str, List[Message]],
    *,
    model: Optional[str] = None,
    tier: Optional[str] = None,
    tools: Optional[List[Tool]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> AsyncIterator[StreamChunk]:
    """Stream completion chunks. Terminal chunk carries ``usage``.

    Current implementation yields once at the end — providers will
    grow native streaming support behind this contract.
    """
    resp = await chat(
        messages_or_prompt,
        model=model,
        tier=tier,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    yield StreamChunk(
        delta=resp.text,
        done=True,
        tool_calls=resp.tool_calls,
        usage=resp.usage,
        finish_reason=resp.finish_reason,
    )


__all__ = ["stream"]
