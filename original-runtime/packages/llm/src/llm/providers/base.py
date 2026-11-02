"""
llm.providers.base — shared types every provider speaks
=========================================================

The types in this module are the canonical Leo LLM wire format — each
provider adapter converts to/from its own wire format at the edges.

This lives under ``providers/`` (not at ``llm/`` top-level) because they
are the shared vocabulary for every provider implementation. They're
re-exported by ``llm/__init__.py`` so callers write ``from llm import
Message`` not ``from llm.providers.base import Message``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

Role = Literal["system", "user", "assistant", "tool"]

# A content block inside ``Message.content`` — text, image_url, or
# tool-result. A bare ``str`` is auto-wrapped to
# ``[{"type":"text","text":...}]`` at the provider edge.
ContentBlock = Dict[str, Any]

# Response-format hint: ``text`` (default), ``json_object`` (loose JSON
# mode), ``json_schema`` (strict schema — OpenAI/Gemini only).
ResponseFormat = Literal["text", "json_object", "json_schema"]

# Tier vocabulary — every model in the registry belongs to exactly one.
Tier = Literal[
    "smart", "fast", "vision", "reasoning", "long_ctx", "cheap", "image_gen"
]

FinishReason = Literal[
    "stop", "length", "tool_calls", "content_filter", "error"
]


@dataclass(frozen=True)
class Message:
    """Canonical Leo message. Passed into every ``chat``/``complete_*`` call.

    A bare string prompt is auto-wrapped to a single
    ``Message(role="user", content=prompt)`` at the top-level dispatcher.
    """

    role: Role
    content: Union[str, List[ContentBlock]]
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


@dataclass(frozen=True)
class Tool:
    """Function-calling tool descriptor. Provider-agnostic.

    Converted to provider-specific shape inside each provider adapter.
    """

    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema


@dataclass(frozen=True)
class ToolCall:
    """Assistant's request to call a tool. Parsed from provider response."""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Per-call token accounting. Populated from provider response.

    ``cost_usd`` is computed lazily against ``llm.PRICING`` — no
    per-provider pricing logic lives here.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    model: str

    @property
    def cost_usd(self) -> float:
        """Compute USD cost via :data:`llm.PRICING`. Returns 0.0 on unknown model."""
        from llm.models.pricing import estimate_cost  # lazy to avoid cycle

        return estimate_cost(self, model=self.model)


@dataclass(frozen=True)
class ChatResponse:
    """Unified response envelope returned by :func:`llm.chat` / :func:`llm.chat_sync`.

    Replaces the raw ``dict[str, Any]`` shape of ``shared.llm``. Callers
    read ``.text`` instead of digging through
    ``data["choices"][0]["message"]["content"]``. ``.raw`` preserves the
    provider response for debugging / fixture capture.
    """

    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Usage] = None
    finish_reason: FinishReason = "stop"
    model: str = ""
    used_fallback: bool = False
    fallback_reason: Optional[str] = None
    attempt_count: int = 1
    raw: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class StreamChunk:
    """One chunk from :func:`llm.stream`.

    ``done=True`` marks the terminal chunk, which carries ``usage`` and
    ``finish_reason``. Intermediate chunks carry only ``delta``.
    """

    delta: str
    done: bool
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Usage] = None
    finish_reason: Optional[str] = None


__all__ = [
    "Role",
    "ContentBlock",
    "ResponseFormat",
    "Tier",
    "FinishReason",
    "Message",
    "Tool",
    "ToolCall",
    "Usage",
    "ChatResponse",
    "StreamChunk",
]
