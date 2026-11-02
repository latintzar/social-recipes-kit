"""
llm.vision — vision-capable chat calls
=========================================

D15 realisation: distinct entry point. Takes an image (bytes, path, or
data-URL) + a text prompt; returns a :class:`~llm.providers.base.ChatResponse`.

Default model is ``DEFAULT_VISION`` (Bedrock Haiku 4.5 — PRIMARY, not
fallback per the parse_media invariant). Fallback chain for vision is
``vision.primary`` by default: ``[DEFAULT_VISION, DEFAULT_LONG_CTX]``.

Image input accepted:
- ``bytes``: raw bytes, auto-detected MIME.
- ``str`` starting with ``data:``: data-URL passed through.
- ``pathlib.Path``: file read, MIME inferred from suffix.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import List, Optional, Union

from llm.chat import chat, chat_sync
from llm.errors import ModelNotFound
from llm.models.registry import resolve_model
from llm.models.tiers import DEFAULT_VISION
from llm.providers.base import ChatResponse, ContentBlock, Message


def _to_data_url(image: Union[bytes, str, Path]) -> str:
    """Normalise image input to a ``data:`` URL."""
    if isinstance(image, str):
        if image.startswith("data:"):
            return image
        return _to_data_url(Path(image))

    if isinstance(image, Path):
        raw = image.read_bytes()
        suffix = image.suffix.lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(suffix, "image/jpeg")
    else:
        raw = image
        if raw.startswith(b"\x89PNG"):
            mime = "image/png"
        elif raw.startswith(b"GIF8"):
            mime = "image/gif"
        elif raw.startswith(b"RIFF") and b"WEBP" in raw[:16]:
            mime = "image/webp"
        else:
            mime = "image/jpeg"

    b64 = base64.b64encode(raw).decode()
    return f"data:{mime};base64,{b64}"


def _build_vision_message(
    prompt: str, image: Union[bytes, str, Path]
) -> Message:
    """Assemble a single user :class:`Message` with text + image_url blocks."""
    blocks: List[ContentBlock] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _to_data_url(image)}},
    ]
    return Message(role="user", content=blocks)


def _ensure_vision(model_id: str) -> None:
    """Raise :exc:`ModelNotFound` if *model_id* doesn't support vision."""
    m = resolve_model(model_id)
    if not m.supports_vision:
        raise ModelNotFound(
            f"model {m.id!r} does not support vision",
            model=m.id,
        )


async def complete_vision(
    prompt: str,
    image: Union[bytes, str, Path],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> ChatResponse:
    """Async vision call. Default model is :data:`~llm.models.tiers.DEFAULT_VISION`.

    Bedrock Haiku is PRIMARY (per the parse_media invariant — Qwen3 VL
    is fallback only on Bedrock 4xx/5xx).
    """
    model_id = model or DEFAULT_VISION
    _ensure_vision(model_id)
    return await chat(
        [_build_vision_message(prompt, image)],
        model=model_id,
        fallback="vision.primary" if model is None else None,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def complete_vision_sync(
    prompt: str,
    image: Union[bytes, str, Path],
    *,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> ChatResponse:
    """Sync vision call. No fallback — sync callers run single-shot."""
    model_id = model or DEFAULT_VISION
    _ensure_vision(model_id)
    return chat_sync(
        [_build_vision_message(prompt, image)],
        model=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
    )


__all__ = ["complete_vision", "complete_vision_sync"]
