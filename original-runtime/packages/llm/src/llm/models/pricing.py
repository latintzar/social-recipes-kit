"""
llm.models.pricing — model pricing table
==========================================

USD per 1K tokens, sourced from provider pricing pages on the ``as_of``
date. Keep this file in lockstep with :data:`llm.models.registry.MODEL_REGISTRY`
— every new model row needs a pricing row.

Missing entries return ``0.0`` (see :func:`estimate_cost`) rather than
raising — an unknown model shouldn't break a live request just because
nobody updated the table. :class:`~llm.providers.base.Usage`
``cost_usd`` surfaces the zero so observability can flag it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from llm.providers.base import Usage


@dataclass(frozen=True)
class ModelPricing:
    """Per-model USD pricing."""

    input_per_1k_usd: float
    output_per_1k_usd: float
    currency: str = "USD"
    as_of: Optional[str] = None


PRICING: Dict[str, ModelPricing] = {
    # --- Anthropic ---------------------------------------------------------
    "anthropic/claude-sonnet-4.6": ModelPricing(0.003, 0.015, as_of="2026-01-15"),
    "anthropic/claude-haiku-4.5": ModelPricing(0.0008, 0.004, as_of="2026-01-15"),
    "anthropic/claude-opus-4.6": ModelPricing(0.015, 0.075, as_of="2026-01-15"),
    "us.anthropic.claude-sonnet-4-6": ModelPricing(0.003, 0.015, as_of="2026-01-15"),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": ModelPricing(
        0.0008, 0.004, as_of="2026-01-15"
    ),
    "eu.anthropic.claude-sonnet-4-20250514-v1:0": ModelPricing(
        0.003, 0.015, as_of="2026-01-15"
    ),
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0": ModelPricing(
        0.0008, 0.004, as_of="2026-01-15"
    ),
    # --- Qwen --------------------------------------------------------------
    "qwen.qwen3-next-80b-a3b": ModelPricing(0.00015, 0.0006, as_of="2026-01-15"),
    "qwen.qwen3-32b-v1:0": ModelPricing(0.00015, 0.0006, as_of="2026-01-15"),
    "qwen.qwen3-vl-235b-a22b": ModelPricing(0.0003, 0.0012, as_of="2026-01-15"),
    # --- DeepSeek ----------------------------------------------------------
    "deepseek.v3.2": ModelPricing(0.0001, 0.0004, as_of="2026-01-15"),
    "deepseek.r1-v1:0": ModelPricing(0.0002, 0.0008, as_of="2026-01-15"),
    # --- Gemini ------------------------------------------------------------
    "gemini-3.1-pro-preview": ModelPricing(0.00125, 0.005, as_of="2026-01-15"),
    "gemini-3-flash-preview": ModelPricing(0.00015, 0.0006, as_of="2026-01-15"),
    "gemini-2.5-flash-lite": ModelPricing(0.00001, 0.00004, as_of="2026-01-15"),
    "gemini-2.5-flash-image": ModelPricing(0.00002, 0.00004, as_of="2026-01-15"),
    # Nano Banana Pro: $2/M input, $12/M output (per OpenRouter 2026-04-22).
    "gemini-3-pro-image": ModelPricing(0.002, 0.012, as_of="2026-04-22"),
    # --- Groq / OpenAI -----------------------------------------------------
    "groq/llama-3.3-70b": ModelPricing(0.00059, 0.00079, as_of="2026-01-15"),
    "openai/text-embedding-3-small": ModelPricing(0.00002, 0.0, as_of="2026-01-15"),
}


def estimate_cost(usage: Usage, *, model: str) -> float:
    """Return USD cost of *usage* on *model*.

    Missing model in :data:`PRICING` → returns ``0.0`` (observability
    fires a ``log.warning`` in caller context; never raises here).
    """
    p = PRICING.get(model)
    if p is None:
        return 0.0
    return (
        p.input_per_1k_usd * usage.input_tokens / 1000
        + p.output_per_1k_usd * usage.output_tokens / 1000
    )


__all__ = ["ModelPricing", "PRICING", "estimate_cost"]
