"""
llm.models.tiers — tier defaults
===================================

``DEFAULT_SMART`` / ``DEFAULT_FAST`` / etc. are the per-tier default
canonical IDs. Callers write ``chat(messages, tier="smart")`` which
resolves here to ``pick("smart")``.

Each default is env-overridable via ``LEO_LLM_DEFAULT_<TIER>`` (e.g.,
``LEO_LLM_DEFAULT_FAST=anthropic/claude-haiku-4.5``). The override is
validated — a misspelled id raises :exc:`~llm.errors.ModelNotFound` at
import.

Preference order (built into each tier) comes from the registration
order in :mod:`llm.models.registry` — the first entry for a given tier
is the default.
"""

from __future__ import annotations

import os

from llm.errors import ModelNotFound
from llm.models.registry import MODEL_REGISTRY, by_tier
from llm.providers.base import Tier

_ENV_KEYS: dict[Tier, str] = {
    "smart": "LEO_LLM_DEFAULT_SMART",
    "fast": "LEO_LLM_DEFAULT_FAST",
    "vision": "LEO_LLM_DEFAULT_VISION",
    "reasoning": "LEO_LLM_DEFAULT_REASONING",
    "long_ctx": "LEO_LLM_DEFAULT_LONG_CTX",
    "cheap": "LEO_LLM_DEFAULT_CHEAP",
    "image_gen": "LEO_LLM_DEFAULT_IMAGE_GEN",
}


def pick(tier: Tier) -> str:
    """Return the canonical ``Model.id`` for *tier*'s default.

    Order:
    1. If ``LEO_LLM_DEFAULT_<TIER>`` is set and points to a registered
       model, use it.
    2. Else, the first model in :func:`~llm.models.registry.by_tier`
       for that tier.

    Raises
    ------
    ModelNotFound
        If the tier has zero registered models (should never happen —
        invariant is test-enforced).
    """
    env_key = _ENV_KEYS.get(tier)
    override = os.environ.get(env_key) if env_key else None
    if override:
        if override not in MODEL_REGISTRY:
            raise ModelNotFound(
                f"{env_key}={override!r} is not a registered model",
                model=override,
            )
        return MODEL_REGISTRY[override].id

    candidates = by_tier(tier)
    if not candidates:
        raise ModelNotFound(f"no model registered for tier {tier!r}")
    return candidates[0].id


# ---------------------------------------------------------------------------
# Module-level tier defaults
# ---------------------------------------------------------------------------
# Resolved at import. Tests that override env vars per-case use
# ``importlib.reload(llm.models.tiers)`` to re-evaluate.

DEFAULT_SMART: str = pick("smart")
DEFAULT_FAST: str = pick("fast")
DEFAULT_VISION: str = pick("vision")
DEFAULT_REASONING: str = pick("reasoning")
DEFAULT_LONG_CTX: str = pick("long_ctx")
DEFAULT_CHEAP: str = pick("cheap")
DEFAULT_IMAGE_GEN: str = pick("image_gen")


__all__ = [
    "pick",
    "DEFAULT_SMART",
    "DEFAULT_FAST",
    "DEFAULT_VISION",
    "DEFAULT_REASONING",
    "DEFAULT_LONG_CTX",
    "DEFAULT_CHEAP",
    "DEFAULT_IMAGE_GEN",
]
