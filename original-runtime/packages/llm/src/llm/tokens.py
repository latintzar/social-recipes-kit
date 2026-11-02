"""
llm.tokens — pre-flight token approximation (tiktoken-based)
==============================================================

D7 realisation: provider ``usage`` is ground truth; tiktoken is the
pre-flight estimator callers use to decide "will this message fit?"
before committing a paid round-trip.

tiktoken is an optional dep. If missing, the counters fall back to
``len(text) // 4`` — enough to catch blatantly-oversized inputs
without failing imports on systems without the C extension.
"""

from __future__ import annotations

from typing import List

from llm.providers.base import Message


def _tiktoken_encoder():
    """Lazy-load a tiktoken encoder. Returns ``None`` if unavailable."""
    try:
        import tiktoken  # type: ignore[import-untyped]

        return tiktoken.get_encoding("cl100k_base")
    except (ImportError, Exception):
        return None


def count_tokens(text: str, *, model: str | None = None) -> int:
    """Estimate the token count of *text*.

    *model* is accepted for API symmetry with provider-native counters
    but currently ignored — cl100k_base is a close-enough proxy for
    Anthropic/OpenAI/Gemini across the registry.
    """
    enc = _tiktoken_encoder()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))


def count_message_tokens(
    messages: List[Message], *, model: str | None = None
) -> int:
    """Estimate tokens for a whole :class:`Message` list.

    Adds a fixed 4-token overhead per message (matches OpenAI's
    documented formula for chat completions).
    """
    total = 0
    for m in messages:
        total += 4
        if isinstance(m.content, str):
            total += count_tokens(m.content, model=model)
        else:
            for block in m.content:
                if block.get("type") == "text":
                    total += count_tokens(block.get("text", ""), model=model)
                elif block.get("type") == "image_url":
                    total += 256  # rough per-image budget
    return total


__all__ = ["count_tokens", "count_message_tokens"]
