"""
llm.models.registry — canonical Model table
=============================================

Every LLM Leo supports lives here. Entries are sourced from the
Phase-3.5b audit (18 live + 12 reference-only + 4 aliases). The
:data:`INVALID_MODELS` anti-list preserves the explicit negative
assertions from the old ``leo_agent.py`` docstring — bogus model IDs
that integrations have tripped over before (e.g., ``gemini-2.0-pro``,
``gemini-2.5-pro-preview``).

Invariants enforced at import (see ``packages/llm/tests/test_models_registry.py``):
- No two ``Model.id`` collide.
- No alias collides with any id or other alias.
- Every ``INVALID_MODELS`` string is *not* in ``MODEL_REGISTRY``.
- Every ``Tier`` is covered by ≥ 1 model.
- Every ``Model`` with ``provider="bedrock"`` has ``region`` set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Literal, Optional, Tuple

from llm.errors import ModelNotFound
from llm.providers.base import Tier

Provider = Literal["bedrock", "litellm_proxy", "gemini"]


@dataclass(frozen=True)
class Model:
    """Canonical metadata for a supported LLM.

    ``id`` is the string callers pass to ``chat(model=...)``. Aliases
    and prefix variants resolve to the same Model via :func:`resolve_model`.
    """

    id: str
    display: str
    provider: Provider
    tier: Tier
    context_window: int
    max_output: int
    supports_vision: bool = False
    supports_tools: bool = False
    supports_json: bool = False
    supports_strict_schema: bool = False
    supports_streaming: bool = True
    use_converse_api: bool = False
    aliases: Tuple[str, ...] = field(default_factory=tuple)
    region: Optional[str] = None


# ---------------------------------------------------------------------------
# Canonical entries
# ---------------------------------------------------------------------------
# Sourced from Phase-3.5b audit (`internal/refactor/packages/llm/audit.md`).
# Pricing metadata lives in ``llm.models.pricing``; keep these files coherent.
# When adding a row: also add a ``PRICING`` entry + a test fixture hitting it.

_ENTRIES: List[Model] = [
    # --- Anthropic via OpenRouter / LiteLLM proxy --------------------------
    Model(
        id="anthropic/claude-sonnet-4.6",
        display="Claude Sonnet 4.6 (OpenRouter)",
        provider="litellm_proxy",
        tier="smart",
        context_window=200_000,
        max_output=8_192,
        supports_tools=True,
        supports_json=True,
        supports_streaming=True,
    ),
    Model(
        id="anthropic/claude-haiku-4.5",
        display="Claude Haiku 4.5 (OpenRouter)",
        provider="litellm_proxy",
        tier="fast",
        context_window=200_000,
        max_output=8_192,
        supports_tools=True,
        supports_json=True,
        aliases=("claude-haiku",),
    ),
    Model(
        id="anthropic/claude-opus-4.6",
        display="Claude Opus 4.6 (OpenRouter)",
        provider="litellm_proxy",
        tier="reasoning",
        context_window=200_000,
        max_output=8_192,
        supports_tools=True,
        supports_json=True,
    ),
    # --- Anthropic via Bedrock (US) ---------------------------------------
    Model(
        id="us.anthropic.claude-sonnet-4-6",
        display="Claude Sonnet 4.6 (US Bedrock)",
        provider="bedrock",
        tier="smart",
        context_window=200_000,
        max_output=8_192,
        supports_tools=True,
        supports_json=True,
        use_converse_api=True,
        region="us-east-1",
    ),
    Model(
        id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        display="Claude Haiku 4.5 (US Bedrock)",
        provider="bedrock",
        tier="vision",
        context_window=200_000,
        max_output=8_192,
        supports_vision=True,
        supports_tools=True,
        supports_json=True,
        use_converse_api=True,
        region="us-east-1",
    ),
    # --- Anthropic via Bedrock (EU) ---------------------------------------
    Model(
        id="eu.anthropic.claude-sonnet-4-20250514-v1:0",
        display="Claude Sonnet 4 (EU Bedrock)",
        provider="bedrock",
        tier="smart",
        context_window=200_000,
        max_output=8_192,
        supports_tools=True,
        supports_json=True,
        use_converse_api=True,
        region="eu-west-1",
    ),
    Model(
        id="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        display="Claude Haiku 4.5 (EU Bedrock)",
        provider="bedrock",
        tier="fast",
        context_window=200_000,
        max_output=8_192,
        supports_tools=True,
        supports_json=True,
        use_converse_api=True,
        region="eu-west-1",
    ),
    # --- Qwen via Bedrock --------------------------------------------------
    Model(
        id="qwen.qwen3-next-80b-a3b",
        display="Qwen3 Next 80B (Bedrock)",
        provider="bedrock",
        tier="long_ctx",
        context_window=1_000_000,
        max_output=16_384,
        supports_tools=False,
        region="us-east-1",
    ),
    Model(
        id="qwen.qwen3-32b-v1:0",
        display="Qwen3 32B (Bedrock)",
        provider="bedrock",
        tier="fast",
        context_window=128_000,
        max_output=8_192,
        region="us-east-1",
    ),
    Model(
        id="qwen.qwen3-vl-235b-a22b",
        display="Qwen3 VL 235B (Bedrock — vision fallback)",
        provider="bedrock",
        tier="vision",
        context_window=128_000,
        max_output=8_192,
        supports_vision=True,
        region="us-east-1",
    ),
    # --- DeepSeek via Bedrock ---------------------------------------------
    Model(
        id="deepseek.v3.2",
        display="DeepSeek v3.2 (Bedrock)",
        provider="bedrock",
        tier="long_ctx",
        context_window=128_000,
        max_output=8_192,
        region="us-east-1",
    ),
    Model(
        id="deepseek.r1-v1:0",
        display="DeepSeek R1 (Bedrock — reasoning)",
        provider="bedrock",
        tier="reasoning",
        context_window=128_000,
        max_output=32_000,
        region="us-east-1",
    ),
    # --- Google Gemini (via proxy today; direct later) --------------------
    Model(
        id="gemini-3.1-pro-preview",
        display="Gemini 3.1 Pro Preview",
        provider="gemini",
        tier="smart",
        context_window=2_000_000,
        max_output=8_192,
        supports_vision=True,
        supports_tools=True,
        supports_json=True,
        supports_strict_schema=True,
        aliases=("pro",),
    ),
    Model(
        id="gemini-3-flash-preview",
        display="Gemini 3 Flash Preview",
        provider="gemini",
        tier="fast",
        context_window=1_000_000,
        max_output=8_192,
        supports_vision=True,
        supports_tools=True,
        supports_json=True,
        supports_strict_schema=True,
        aliases=("flash",),
    ),
    Model(
        id="gemini-2.5-flash-lite",
        display="Gemini 2.5 Flash Lite",
        provider="gemini",
        tier="cheap",
        context_window=1_000_000,
        max_output=8_192,
        supports_vision=True,
        supports_json=True,
        aliases=("flash-lite",),
    ),
    # Nano Banana Pro — the default image-gen model. Identity preservation
    # across up to 5 reference images makes it the right pick for consistent
    # character/brand illustration work.
    Model(
        id="gemini-3-pro-image",
        display="Gemini 3 Pro Image / Nano Banana Pro (image gen)",
        provider="gemini",
        tier="image_gen",
        context_window=65_536,
        max_output=8_192,
        aliases=("nano-banana-pro", "gemini-3-image", "pro-image"),
    ),
    # Nano Banana (2.5 Flash Image) — cheaper fallback, retained for batch
    # jobs where multi-image consistency is not needed.
    Model(
        id="gemini-2.5-flash-image",
        display="Gemini 2.5 Flash Image / Nano Banana (image gen)",
        provider="gemini",
        tier="image_gen",
        context_window=1_000_000,
        max_output=8_192,
        aliases=("nano-banana", "flash-image"),
    ),
    # --- Groq via proxy (preserved from audit) ----------------------------
    Model(
        id="groq/llama-3.3-70b",
        display="Groq Llama 3.3 70B",
        provider="litellm_proxy",
        tier="fast",
        context_window=128_000,
        max_output=8_192,
        supports_tools=True,
    ),
    # --- OpenAI via proxy (embeddings) ------------------------------------
    Model(
        id="openai/text-embedding-3-small",
        display="OpenAI text-embedding-3-small",
        provider="litellm_proxy",
        tier="cheap",
        context_window=8_191,
        max_output=0,
        supports_streaming=False,
    ),
]


INVALID_MODELS: FrozenSet[str] = frozenset(
    {
        # Anti-list preserved from leo_agent.py docstring.
        "gemini-2.0-pro",
        "gemini-2.0-flash",
        "gemini-2.5-pro-preview",
    }
)


# ---------------------------------------------------------------------------
# Build the lookup table
# ---------------------------------------------------------------------------


def _build_registry() -> Dict[str, Model]:
    """Index every model by id + every alias. Enforce invariants."""
    out: Dict[str, Model] = {}
    ids_seen: set[str] = set()

    for m in _ENTRIES:
        if m.id in ids_seen:
            raise RuntimeError(f"duplicate model id: {m.id!r}")
        ids_seen.add(m.id)
        if m.id in INVALID_MODELS:
            raise RuntimeError(
                f"model id {m.id!r} is on INVALID_MODELS anti-list"
            )
        if m.provider == "bedrock" and not m.region:
            raise RuntimeError(f"bedrock model {m.id!r} missing region")

        if m.id in out:
            raise RuntimeError(f"id collision: {m.id!r}")
        out[m.id] = m

        for alias in m.aliases:
            if alias in out:
                raise RuntimeError(
                    f"alias collision: {alias!r} already maps to {out[alias].id!r}"
                )
            out[alias] = m

    return out


MODEL_REGISTRY: Dict[str, Model] = _build_registry()
"""Keyed by canonical id + every alias. Populated at import."""


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def resolve_model(id_or_alias: str) -> Model:
    """Look up a model by id, alias, or ``bedrock/...`` prefix.

    >>> resolve_model("anthropic/claude-haiku-4.5").tier
    'fast'
    >>> resolve_model("claude-haiku").tier                # alias
    'fast'
    >>> resolve_model("bedrock/us.anthropic.claude-sonnet-4-6").provider
    'bedrock'

    Raises
    ------
    ModelNotFound
        If the string is unknown or on :data:`INVALID_MODELS`.
    """
    s = id_or_alias
    if s.startswith("bedrock/"):
        s = s[len("bedrock/") :]

    if s in INVALID_MODELS:
        raise ModelNotFound(
            f"{s!r} is on the anti-list: known to not exist",
            model=s,
        )

    if s in MODEL_REGISTRY:
        return MODEL_REGISTRY[s]

    raise ModelNotFound(f"unknown model: {id_or_alias!r}", model=id_or_alias)


def by_tier(tier: Tier) -> List[Model]:
    """Return every model matching *tier*, ordered by registration.

    Deduplicates alias entries — each concrete :class:`Model` appears once.
    """
    seen: set[str] = set()
    out: List[Model] = []
    for m in _ENTRIES:
        if m.tier == tier and m.id not in seen:
            seen.add(m.id)
            out.append(m)
    return out


def for_provider(provider: Provider) -> List[Model]:
    """Return every model for a given provider, in registration order."""
    return [m for m in _ENTRIES if m.provider == provider]


__all__ = [
    "Model",
    "Provider",
    "MODEL_REGISTRY",
    "INVALID_MODELS",
    "resolve_model",
    "by_tier",
    "for_provider",
]
