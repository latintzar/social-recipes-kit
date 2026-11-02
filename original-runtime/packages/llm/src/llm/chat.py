"""
llm.chat — the two public entry points
=========================================

D1 consolidation: one ``chat`` + one ``chat_sync``. Both accept
**either** a ``str`` prompt **or** a list of :class:`~llm.providers.base.Message`.

Dispatch: caller specifies ``model=`` OR ``tier=``. ``tier`` resolves
via :func:`llm.models.tiers.pick`. ``model=`` may be a canonical id, an
alias, or a ``bedrock/...`` prefix — :func:`~llm.models.registry.resolve_model`
handles all three.

After dispatch the request runs through :func:`~llm.providers.fallback.run_with_fallback`
if ``fallback=[...]`` is supplied, and the final response flows through
:func:`llm.usage.record_usage` so observers see every call.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from llm.errors import ModelNotFound
from llm.models.registry import Model, resolve_model
from llm.models.tiers import pick
from llm.providers import bedrock as _bedrock
from llm.providers import gemini as _gemini
from llm.providers import litellm_proxy as _proxy
from llm.providers.base import ChatResponse, Message, Tool
from llm.providers.fallback import resolve_chain, run_with_fallback


def _normalise(
    messages_or_prompt: Union[str, List[Message]],
) -> List[Message]:
    """Accept ``str`` → wrap in single user :class:`Message`."""
    if isinstance(messages_or_prompt, str):
        return [Message(role="user", content=messages_or_prompt)]
    return list(messages_or_prompt)


def _resolve_model_or_tier(
    model: Optional[str], tier: Optional[str]
) -> Model:
    if model and tier:
        raise ModelNotFound(
            "chat() takes model= OR tier=, not both",
            model=f"model={model!r} tier={tier!r}",
        )
    if model:
        return resolve_model(model)
    if tier:
        return resolve_model(pick(tier))  # type: ignore[arg-type]
    return resolve_model(pick("fast"))


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------


async def chat(
    messages_or_prompt: Union[str, List[Message]],
    *,
    model: Optional[str] = None,
    tier: Optional[str] = None,
    tools: Optional[List[Tool]] = None,
    response_format: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    fallback: Optional[Union[str, List[str]]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ChatResponse:
    """Unified async chat entry point. Dispatches on provider.

    >>> r = await chat("hola", tier="fast")
    >>> r.text
    ...
    """
    messages = _normalise(messages_or_prompt)
    target = _resolve_model_or_tier(model, tier)

    async def _invoke(model_id: str) -> ChatResponse:
        m = resolve_model(model_id)
        if m.provider == "bedrock":
            return await _bedrock.complete(
                messages,
                model=model_id,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        if m.provider == "gemini":
            return await _gemini.complete(
                messages,
                model=model_id,
                tools=tools,
                response_format=response_format,
                response_schema=response_schema,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return await _proxy.complete(
            messages,
            model=model_id,
            tools=tools,
            response_format=response_format,
            response_schema=response_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
        )

    if fallback:
        chain = resolve_chain(fallback)
        resp = await run_with_fallback(target.id, chain, invoke=_invoke)
    else:
        resp = await _invoke(target.id)

    _emit_usage(resp)
    return resp


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------


def chat_sync(
    messages_or_prompt: Union[str, List[Message]],
    *,
    model: Optional[str] = None,
    tier: Optional[str] = None,
    tools: Optional[List[Tool]] = None,
    response_format: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ChatResponse:
    """Sync version of :func:`chat`. No fallback chain — sync callers
    (i18n_engine, sales_tracker, profile_import) run single-shot.

    Migrators that need fallback in sync code should adopt async or
    wrap with ``asyncio.run(chat(..., fallback=[...]))``.
    """
    messages = _normalise(messages_or_prompt)
    target = _resolve_model_or_tier(model, tier)

    if target.provider == "bedrock":
        resp = _bedrock.complete_sync(
            messages,
            model=target.id,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    elif target.provider == "gemini":
        resp = _gemini.complete_sync(
            messages,
            model=target.id,
            tools=tools,
            response_format=response_format,
            response_schema=response_schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    else:
        resp = _proxy.complete_sync(
            messages,
            model=target.id,
            tools=tools,
            response_format=response_format,
            response_schema=response_schema,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
        )

    _emit_usage(resp)
    return resp


def _emit_usage(resp: ChatResponse) -> None:
    """Dispatch the response to observability; swallow observer bugs."""
    try:
        from llm.usage import record_usage

        record_usage(resp)
    except Exception:  # noqa: BLE001 — observers must never break a call
        import logging

        logging.getLogger(__name__).exception("llm.chat: usage emitter raised")


__all__ = ["chat", "chat_sync"]
