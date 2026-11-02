"""
llm.providers.bedrock — AWS Bedrock (Converse + InvokeModel)
=============================================================

Consolidates the Bedrock call paths previously scattered across
``shared.aws``, ``leo_agent/parse_media.py``, and callers. This module
**owns** Bedrock invocation for text + vision; callers route here
through :func:`llm.chat`.

Two APIs:
- ``use_converse_api=True`` (modern): Bedrock Converse API, unified
  across Anthropic/Qwen/DeepSeek. Preferred.
- ``use_converse_api=False``: legacy InvokeModel with provider-specific
  bodies. Used for image-gen / embedding-only models.

boto3 is an optional dependency — install via
``pip install packages/llm[bedrock]``. If boto3 is missing, any call to
:func:`complete` raises :exc:`~llm.errors.ProviderUnavailable` with a
clear message; tests without AWS don't depend on boto3.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from llm.errors import (
    InvalidResponseShape,
    ProviderError,
    ProviderUnavailable,
    RateLimitError,
)
from llm.models.registry import Model, resolve_model
from llm.providers.base import ChatResponse, Message, Tool, Usage

# ---------------------------------------------------------------------------
# Provider constants (moved from packages/domain/constants.py in Wave 2)
# ---------------------------------------------------------------------------
# Env-overrideable at process start. Callers who need a runtime override
# should read `BEDROCK_REGION` etc. directly — once frozen at import the
# value is immutable, same contract as the old domain/constants.py home.

BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
"""Default AWS region for ``bedrock-runtime`` clients. Overridable via
``BEDROCK_REGION`` env var."""

BEDROCK_CLAUDE_HAIKU = os.getenv(
    "BEDROCK_CLAUDE_HAIKU",
    "anthropic.claude-3-haiku-20240307-v1:0",
)
"""Bedrock model ID for Anthropic Claude Haiku. Used as the fast / cheap
path for short-form generations. Overridable via ``BEDROCK_CLAUDE_HAIKU``.

Note: this is a bare provider constant, NOT the Leo-alias tier system.
For tier-based routing (``DEFAULT_FAST`` etc.) see ``llm.models.tiers``."""

BEDROCK_CLAUDE_SONNET = os.getenv(
    "BEDROCK_CLAUDE_SONNET",
    "anthropic.claude-3-5-sonnet-20241022-v2:0",
)
"""Bedrock model ID for Anthropic Claude Sonnet. Overridable via
``BEDROCK_CLAUDE_SONNET`` env var. See ``BEDROCK_CLAUDE_HAIKU`` note."""


def _bedrock_client(region: str) -> Any:
    """Return a ``bedrock-runtime`` client for *region* with Leo billing
    discipline.

    Delegates to :func:`core.aws.bedrock_client` so every Bedrock call
    goes through the ``LEO_AWS_ACCOUNT`` / ``AWS_ACCESS_KEY_ID_<ACCOUNT>``
    resolution and never falls back to ambient credentials. That's the
    billing-correctness invariant; without it a Leo process could charge
    a developer's personal account.

    Raises :exc:`ProviderUnavailable` if ``core.aws`` can't resolve
    credentials (missing env vars / boto3 not installed) — callers get
    the same contract as before.
    """
    try:
        from core.aws import bedrock_client as _core_bedrock_client
    except ImportError as exc:
        raise ProviderUnavailable(
            "core.aws unavailable — packages/llm depends on packages/core",
            provider="bedrock",
            raw=exc,
        ) from exc
    try:
        return _core_bedrock_client(region)
    except RuntimeError as exc:
        raise ProviderUnavailable(
            f"bedrock unavailable: {exc}",
            provider="bedrock",
            raw=exc,
        ) from exc
    except ImportError as exc:
        raise ProviderUnavailable(
            "boto3 not installed — run `pip install packages/llm[bedrock]`",
            provider="bedrock",
            raw=exc,
        ) from exc


def _msg_to_converse(m: Message) -> Dict[str, Any]:
    """Leo :class:`Message` → Bedrock Converse API message shape."""
    if isinstance(m.content, str):
        content = [{"text": m.content}]
    else:
        content = []
        for block in m.content:
            if block.get("type") == "text":
                content.append({"text": block["text"]})
            elif block.get("type") == "image_url":
                url = block["image_url"]["url"]
                if url.startswith("data:"):
                    import base64
                    header, b64 = url.split(",", 1)
                    fmt = "jpeg"
                    if "png" in header:
                        fmt = "png"
                    elif "webp" in header:
                        fmt = "webp"
                    content.append(
                        {
                            "image": {
                                "format": fmt,
                                "source": {"bytes": base64.b64decode(b64)},
                            }
                        }
                    )
            elif block.get("type") == "image":
                content.append({"image": block["image"]})
    role = m.role if m.role in ("user", "assistant") else "user"
    return {"role": role, "content": content}


def _parse_converse_response(data: Dict[str, Any], *, model: str) -> ChatResponse:
    try:
        output = data["output"]["message"]
        blocks = output.get("content", [])
    except (KeyError, TypeError) as exc:
        raise InvalidResponseShape(
            f"bedrock response missing output.message.content: {data!r}",
            provider="bedrock",
            model=model,
            raw=exc,
        ) from exc

    text = "".join(b.get("text", "") for b in blocks if "text" in b)
    usage_data = data.get("usage") or {}
    usage = Usage(
        input_tokens=usage_data.get("inputTokens", 0),
        output_tokens=usage_data.get("outputTokens", 0),
        total_tokens=usage_data.get("totalTokens", 0),
        model=model,
    )
    return ChatResponse(
        text=text,
        usage=usage,
        finish_reason=data.get("stopReason", "stop"),
        model=model,
        raw=data,
    )


def _map_botocore_error(exc: Exception, *, model: str) -> Exception:
    """Translate a botocore ``ClientError`` → :mod:`llm.errors` hierarchy."""
    response = getattr(exc, "response", None) or {}
    err = response.get("Error", {})
    code = err.get("Code", "")
    message = err.get("Message", str(exc))

    if code in ("ThrottlingException", "TooManyRequestsException"):
        return RateLimitError(
            f"bedrock throttled: {message}",
            provider="bedrock",
            model=model,
            raw=exc,
        )
    if code in ("ServiceUnavailableException", "InternalServerException"):
        return ProviderUnavailable(
            f"bedrock 5xx: {message}",
            provider="bedrock",
            model=model,
            raw=exc,
        )
    return ProviderError(
        f"bedrock error {code}: {message}",
        provider="bedrock",
        model=model,
        raw=exc,
    )


def complete_sync(
    messages: List[Message],
    *,
    model: str,
    tools: Optional[List[Tool]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    client: Optional[Any] = None,
) -> ChatResponse:
    """Sync Bedrock Converse call. Runs on the caller's thread —
    callers from async code MUST wrap in ``asyncio.to_thread`` (see
    :func:`llm.providers.bedrock.complete`).
    """
    m: Model = resolve_model(model)
    if m.provider != "bedrock":
        raise ProviderError(
            f"model {model!r} is not on Bedrock (provider={m.provider!r})",
            provider="bedrock",
            model=model,
        )

    bedrock = client or _bedrock_client(m.region or "us-east-1")

    converse_messages = [_msg_to_converse(msg) for msg in messages if msg.role != "system"]
    system_blocks = [
        {"text": msg.content if isinstance(msg.content, str) else str(msg.content)}
        for msg in messages
        if msg.role == "system"
    ]

    inference_config: Dict[str, Any] = {}
    if temperature is not None:
        inference_config["temperature"] = temperature
    if max_tokens is not None:
        inference_config["maxTokens"] = max_tokens

    try:
        kwargs: Dict[str, Any] = {
            "modelId": m.id,
            "messages": converse_messages,
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if inference_config:
            kwargs["inferenceConfig"] = inference_config
        resp = bedrock.converse(**kwargs)
    except Exception as exc:
        raise _map_botocore_error(exc, model=model) from exc

    return _parse_converse_response(resp, model=model)


async def complete(
    messages: List[Message],
    *,
    model: str,
    tools: Optional[List[Tool]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    client: Optional[Any] = None,
) -> ChatResponse:
    """Async wrapper over :func:`complete_sync` via ``asyncio.to_thread``.

    boto3 is blocking — we offload rather than introduce aiobotocore
    (heavier footprint, harder to mock in tests).
    """
    import asyncio

    return await asyncio.to_thread(
        complete_sync,
        messages,
        model=model,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
    )


__all__ = [
    "complete",
    "complete_sync",
    "BEDROCK_REGION",
    "BEDROCK_CLAUDE_HAIKU",
    "BEDROCK_CLAUDE_SONNET",
]
