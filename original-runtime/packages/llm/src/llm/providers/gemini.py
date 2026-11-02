"""
llm.providers.gemini — direct Gemini REST (currently via proxy, spec'd for native)
===================================================================================

Today: Gemini requests flow through the LiteLLM proxy (the proxy
handles Gemini auth + routing). This provider adapter is an identity
wrapper over :mod:`llm.providers.litellm_proxy` so that model-dispatch
still routes on ``provider="gemini"`` cleanly.

Tomorrow (post-Phase-4): swap the body of :func:`complete` for direct
`generativelanguage.googleapis.com` calls. The public contract doesn't
change — callers see the same :class:`~llm.providers.base.ChatResponse`.

D10 streaming & D15 vision: this module will grow native support when
the proxy-only path is retired. For now, both delegate to the proxy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from llm.providers import litellm_proxy
from llm.providers.base import ChatResponse, Message, Tool


async def complete(
    messages: List[Message],
    *,
    model: str,
    tools: Optional[List[Tool]] = None,
    response_format: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> ChatResponse:
    """Async Gemini completion. Currently proxied; spec'd for native."""
    return await litellm_proxy.complete(
        messages,
        model=model,
        tools=tools,
        response_format=response_format,
        response_schema=response_schema,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
    )


def complete_sync(
    messages: List[Message],
    *,
    model: str,
    tools: Optional[List[Tool]] = None,
    response_format: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    client: Optional[httpx.Client] = None,
) -> ChatResponse:
    """Sync Gemini completion. Currently proxied; spec'd for native."""
    return litellm_proxy.complete_sync(
        messages,
        model=model,
        tools=tools,
        response_format=response_format,
        response_schema=response_schema,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
    )


__all__ = ["complete", "complete_sync"]
