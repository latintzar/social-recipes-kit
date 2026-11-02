"""Model registry, tier defaults, pricing for :mod:`llm`.

Re-exports the public surface (``Model``, ``MODEL_REGISTRY``, ``pick``,
``DEFAULT_*``, ``ModelPricing``, ``PRICING``, ``estimate_cost``).
Individual model entries live in :mod:`llm.models.registry`; tier
defaults in :mod:`llm.models.tiers`; pricing table in
:mod:`llm.models.pricing`.
"""

from llm.models.pricing import PRICING, ModelPricing, estimate_cost
from llm.models.registry import (
    INVALID_MODELS,
    MODEL_REGISTRY,
    Model,
    by_tier,
    for_provider,
    resolve_model,
)
from llm.models.tiers import (
    DEFAULT_CHEAP,
    DEFAULT_FAST,
    DEFAULT_IMAGE_GEN,
    DEFAULT_LONG_CTX,
    DEFAULT_REASONING,
    DEFAULT_SMART,
    DEFAULT_VISION,
    pick,
)

__all__ = [
    "Model",
    "MODEL_REGISTRY",
    "INVALID_MODELS",
    "resolve_model",
    "by_tier",
    "for_provider",
    "pick",
    "DEFAULT_SMART",
    "DEFAULT_FAST",
    "DEFAULT_VISION",
    "DEFAULT_REASONING",
    "DEFAULT_LONG_CTX",
    "DEFAULT_CHEAP",
    "DEFAULT_IMAGE_GEN",
    "ModelPricing",
    "PRICING",
    "estimate_cost",
]
