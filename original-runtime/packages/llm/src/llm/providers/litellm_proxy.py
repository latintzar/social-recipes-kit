"""
llm.providers.litellm_proxy — OpenAI-style chat via the LiteLLM proxy
======================================================================

Thin passthrough over the local LiteLLM proxy: the proxy owns retry,
fallback, and audit policy (see ``litellm-config.yaml::num_retries=2,
request_timeout=120``) — the client just shapes the request and parses
the response.

Body format: OpenAI Chat Completions v1. Endpoint:
``<LITELLM_BASE_URL>/v1/chat/completions``. The ``/v1`` suffix lives
here (not in :mod:`llm.config`) — see D12 in ``design.md``.

Error mapping:
- 429 → :exc:`~llm.errors.RateLimitError`
- 5xx / connect-refused / timeout → :exc:`~llm.errors.ProviderUnavailable`
- 4xx (non-429) → :exc:`~llm.errors.ProviderError`
- response missing ``choices[0].message.content`` → :exc:`~llm.errors.InvalidResponseShape`
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from llm.config import proxy_api_key, proxy_base_url
from llm.errors import (
    InvalidResponseShape,
    ProviderError,
    ProviderUnavailable,
    RateLimitError,
)
from llm.providers.base import (
    ChatResponse,
    ContentBlock,
    Message,
    Tool,
    ToolCall,
    Usage,
)

_DEFAULT_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Wire-format conversion
# ---------------------------------------------------------------------------


def _msg_to_wire(m: Message) -> Dict[str, Any]:
    """Leo :class:`Message` → OpenAI Chat message dict.

    Bare ``str`` content passes through; a :class:`list` of
    :data:`ContentBlock` passes through unchanged (OpenAI/Anthropic
    multipart shape).
    """
    out: Dict[str, Any] = {"role": m.role, "content": m.content}
    if m.name:
        out["name"] = m.name
    if m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    return out


def _tool_to_wire(t: Tool) -> Dict[str, Any]:
    """Leo :class:`Tool` → OpenAI function-calling schema."""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }


def _parse_response(data: Dict[str, Any], *, model: str) -> ChatResponse:
    """OpenAI response JSON → :class:`ChatResponse`. Raises
    :exc:`InvalidResponseShape` on missing fields.
    """
    try:
        choice = data["choices"][0]
        message = choice["message"]
        raw_text = message.get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidResponseShape(
            f"proxy response missing choices[0].message: {data!r}",
            provider="litellm_proxy",
            model=model,
            raw=exc,
        ) from exc

    # content can be a str OR a list of content blocks (Anthropic multipart
    # on Bedrock via proxy). Flatten to string if list.
    if isinstance(raw_text, list):
        raw_text = "".join(
            part.get("text", "")
            for part in raw_text
            if isinstance(part, dict) and part.get("type") == "text"
        )
    text: str = raw_text or ""

    tool_calls: List[ToolCall] = []
    for call in message.get("tool_calls", []) or []:
        try:
            fn = call["function"]
            import json

            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                args = json.loads(args)
            tool_calls.append(
                ToolCall(id=call.get("id", ""), name=fn["name"], arguments=args)
            )
        except (KeyError, ValueError, TypeError):
            continue

    usage_data = data.get("usage") or {}
    usage: Optional[Usage] = None
    if usage_data:
        usage = Usage(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
            model=model,
        )

    return ChatResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=choice.get("finish_reason", "stop"),
        model=model,
        raw=data,
    )


def _map_http_error(exc: httpx.HTTPError, *, model: str) -> Exception:
    """Translate httpx exceptions into :mod:`llm.errors` taxonomy."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return RateLimitError(
                f"proxy rate-limited (429): {exc.response.text[:200]}",
                provider="litellm_proxy",
                model=model,
                raw=exc,
            )
        if status >= 500:
            return ProviderUnavailable(
                f"proxy 5xx: {status} {exc.response.text[:200]}",
                provider="litellm_proxy",
                model=model,
                raw=exc,
            )
        return ProviderError(
            f"proxy 4xx: {status} {exc.response.text[:200]}",
            provider="litellm_proxy",
            model=model,
            raw=exc,
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException)):
        return ProviderUnavailable(
            f"proxy transport error: {exc}",
            provider="litellm_proxy",
            model=model,
            raw=exc,
        )
    return ProviderError(
        f"proxy unexpected error: {exc}",
        provider="litellm_proxy",
        model=model,
        raw=exc,
    )


# ---------------------------------------------------------------------------
# Request builders + transports
# ---------------------------------------------------------------------------


def _build_body(
    *,
    model: str,
    messages: List[Message],
    tools: Optional[List[Tool]],
    response_format: Optional[str],
    response_schema: Optional[Dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    extra: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": [_msg_to_wire(m) for m in messages],
    }
    if tools:
        body["tools"] = [_tool_to_wire(t) for t in tools]
    if response_format == "json_object":
        body["response_format"] = {"type": "json_object"}
    elif response_format == "json_schema" and response_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "Response", "schema": response_schema, "strict": True},
        }
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if extra:
        body.update(extra)
    return body


def _build_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {proxy_api_key()}",
        "Content-Type": "application/json",
    }


def _endpoint() -> str:
    return f"{proxy_base_url()}/v1/chat/completions"


async def complete(
    messages: List[Message],
    *,
    model: str,
    tools: Optional[List[Tool]] = None,
    response_format: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.AsyncClient] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ChatResponse:
    """Async chat completion via the LiteLLM proxy."""
    body = _build_body(
        model=model,
        messages=messages,
        tools=tools,
        response_format=response_format,
        response_schema=response_schema,
        temperature=temperature,
        max_tokens=max_tokens,
        extra=extra,
    )
    headers = _build_headers()
    url = _endpoint()

    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await http.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return _parse_response(resp.json(), model=model)
    except httpx.HTTPError as exc:
        raise _map_http_error(exc, model=model) from exc
    finally:
        if owned:
            await http.aclose()


def complete_sync(
    messages: List[Message],
    *,
    model: str,
    tools: Optional[List[Tool]] = None,
    response_format: Optional[str] = None,
    response_schema: Optional[Dict[str, Any]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    client: Optional[httpx.Client] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> ChatResponse:
    """Sync chat completion via the LiteLLM proxy.

    Used by i18n_engine, sales_tracker, profile_import — one-shot
    ``httpx.Client`` per call (matches the ``shared.llm`` behaviour).
    """
    body = _build_body(
        model=model,
        messages=messages,
        tools=tools,
        response_format=response_format,
        response_schema=response_schema,
        temperature=temperature,
        max_tokens=max_tokens,
        extra=extra,
    )
    headers = _build_headers()
    url = _endpoint()

    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return _parse_response(resp.json(), model=model)
    except httpx.HTTPError as exc:
        raise _map_http_error(exc, model=model) from exc
    finally:
        if owned:
            http.close()


__all__ = ["complete", "complete_sync"]
