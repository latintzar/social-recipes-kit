"""
llm.structured — schema-validated structured output
=====================================================

D9 realisation: wraps :func:`llm.chat` with a Pydantic schema.

Strategy:
1. If the model supports ``json_schema`` response format (Gemini today;
   OpenAI via proxy), pass the JSON Schema through and trust it.
2. Otherwise, set ``response_format="json_object"`` and parse/validate
   the string result against the Pydantic model ourselves.

Returns a validated :class:`~pydantic.BaseModel` instance. Validation
errors raise :exc:`~llm.errors.InvalidResponseShape` with the raw text
attached for debugging.
"""

from __future__ import annotations

import json
from typing import List, Optional, Type, TypeVar, Union

from pydantic import BaseModel, ValidationError

from llm.chat import chat, chat_sync
from llm.errors import InvalidResponseShape
from llm.models.registry import resolve_model
from llm.models.tiers import pick
from llm.providers.base import Message

T = TypeVar("T", bound=BaseModel)


def _choose_mode(model_id: str) -> str:
    """Pick ``json_schema`` if the model supports strict schema; else
    ``json_object``.
    """
    return "json_schema" if resolve_model(model_id).supports_strict_schema else "json_object"


def _parse(raw_text: str, *, schema: Type[T], model_id: str) -> T:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidResponseShape(
            f"structured response not JSON: {raw_text[:200]!r}",
            model=model_id,
            raw=exc,
        ) from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise InvalidResponseShape(
            f"structured response failed schema: {exc}",
            model=model_id,
            raw=exc,
        ) from exc


async def complete_structured(
    messages_or_prompt: Union[str, List[Message]],
    *,
    schema: Type[T],
    model: Optional[str] = None,
    tier: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> T:
    """Async structured-output call. Returns a validated *schema* instance."""
    model_id = resolve_model(model or pick(tier or "fast")).id  # type: ignore[arg-type]
    mode = _choose_mode(model_id)
    resp = await chat(
        messages_or_prompt,
        model=model_id,
        response_format=mode,
        response_schema=schema.model_json_schema() if mode == "json_schema" else None,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return _parse(resp.text, schema=schema, model_id=model_id)


def complete_structured_sync(
    messages_or_prompt: Union[str, List[Message]],
    *,
    schema: Type[T],
    model: Optional[str] = None,
    tier: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> T:
    """Sync structured-output call. Returns a validated *schema* instance."""
    model_id = resolve_model(model or pick(tier or "fast")).id  # type: ignore[arg-type]
    mode = _choose_mode(model_id)
    resp = chat_sync(
        messages_or_prompt,
        model=model_id,
        response_format=mode,
        response_schema=schema.model_json_schema() if mode == "json_schema" else None,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return _parse(resp.text, schema=schema, model_id=model_id)


__all__ = ["complete_structured", "complete_structured_sync"]
