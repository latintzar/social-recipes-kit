# `@scope/llm` — LLM abstraction

Single entry point for chat, structured output, vision, embeddings,
model registry, prompts, token counting, and cost accounting across
Leo's LLM-touching services (Hermes, Care, Sales, Enrich).

**Replaces** `unmigrated/backend/shared/llm.py`,
`unmigrated/backend/leo_agent/leo_agent.py` (LLM-dispatch portions),
and the Bedrock invocation scatter in `shared/aws.py` +
`leo_agent/parse_media.py`.

## Layout

```
packages/llm/
├── pyproject.toml                # httpx, pydantic, jinja2, pyyaml; optional: boto3, tiktoken
├── README.md                     # (this file)
└── src/llm/
    ├── __init__.py               # curated re-exports (53 public symbols)
    ├── config.py                 # the single LITELLM_* env reader (D12)
    ├── errors.py                 # LLMError hierarchy (8 classes)
    ├── chat.py                   # chat / chat_sync — the two entry points (D1)
    ├── structured.py             # complete_structured(_sync) — Pydantic-validated JSON (D9)
    ├── vision.py                 # complete_vision(_sync) — image + text (D15)
    ├── stream.py                 # stream() — skeleton per D10
    ├── embed.py                  # embed(_sync) — OpenAI-compatible embeddings
    ├── tokens.py                 # count_tokens / count_message_tokens (D7)
    ├── usage.py                  # record_usage + observer registry (D8, D16)
    # (Phase-4 complete: _deprecated shims removed)
    ├── models/
    │   ├── registry.py           # Model dataclass + MODEL_REGISTRY (D3)
    │   ├── tiers.py              # DEFAULT_SMART / FAST / ... (env-overridable)
    │   └── pricing.py            # ModelPricing + PRICING table (D8)
    ├── prompts/
    │   ├── loader.py             # .md + YAML frontmatter → Prompt
    │   ├── registry.py           # PromptRegistry + render_prompt (D6)
    │   └── templates/            # 6 migrated templates
    │       ├── concern_classifier/{v1_en,v1_es}.md
    │       ├── variation_extraction/v1_es.md
    │       ├── parse_media_structure/v1.md
    │       ├── i18n_translate/v1.md
    │       ├── profile_import_extract/v1.md
    │       └── sales_tracker_enrich/v1.md
    └── providers/
        ├── base.py               # Message, Tool, ChatResponse, Usage, StreamChunk
        ├── litellm_proxy.py      # async + sync transport over the LiteLLM proxy
        ├── bedrock.py            # AWS Bedrock Converse API (D4)
        ├── gemini.py             # Gemini (proxy-passthrough today; native spec'd)
        └── fallback.py           # named chains + run_with_fallback runner (D5)
```

## Public API — 53 symbols

```python
from llm import (
    # chat
    chat, chat_sync,
    # structured output
    complete_structured, complete_structured_sync,
    # vision
    complete_vision, complete_vision_sync,
    # stream / embed
    stream, embed, embed_sync,
    # canonical types
    Message, Tool, ToolCall, ChatResponse, StreamChunk, ResponseFormat, Usage,
    # models
    Model, MODEL_REGISTRY, INVALID_MODELS,
    pick, by_tier, for_provider, resolve_model,
    DEFAULT_SMART, DEFAULT_FAST, DEFAULT_VISION,
    DEFAULT_REASONING, DEFAULT_LONG_CTX, DEFAULT_CHEAP, DEFAULT_IMAGE_GEN,
    # pricing
    ModelPricing, PRICING, estimate_cost,
    # prompts
    Prompt, render_prompt, get_prompt,
    # tokens
    count_tokens, count_message_tokens,
    # usage / observability
    record_usage, register_observer, unregister_observer,
    # errors
    LLMError, ProviderError, ProviderUnavailable, RateLimitError,
    ContextWindowExceeded, InvalidResponseShape, ModelNotFound, PromptNotFound,
    # config
    LITELLM_BASE_URL, LITELLM_API_KEY, proxy_base_url, proxy_api_key,
)
```

### Typical calls

```python
# Simple prompt → text
resp = await chat("hola", tier="fast")
print(resp.text)

# With fallback chain
resp = await chat(
    messages,
    model="anthropic/claude-sonnet-4.6",
    fallback=["anthropic/claude-haiku-4.5"],  # or "chat.balanced"
)

# Structured output
from pydantic import BaseModel
class Concern(BaseModel):
    category: str
    confidence: float

c = await complete_structured(
    render_prompt("concern_classifier",
                  variables={"message": msg},
                  language="es"),
    schema=Concern,
)

# Vision
resp = await complete_vision(
    "Describe this invoice",
    image=Path("scan.jpg"),
)
```

## Tier

Tier 1 (`layer_rule.py`): `llm` may import only from `core` + `domain`.
No `shared.`, no `leo_agent.`, no `routes.`, no `models.` imports.

## Invariants (test-enforced)

1. No duplicate `Model.id` in `MODEL_REGISTRY`.
2. No alias/id collisions.
3. Every `Model(provider="bedrock")` has a non-empty `region`.
4. Every `INVALID_MODELS` string is **not** in `MODEL_REGISTRY`.
5. Every concrete `Model.id` has a pricing row in `PRICING`.
6. Every tier (`smart`, `fast`, `vision`, ...) has ≥ 1 registered model.
7. `LITELLM_BASE_URL` does **not** end with `/v1` (D12 fix for the
   `shared.constants` vs `leo_agent.py` drift).
8. Every `supports_strict_schema=True` model also has `supports_json=True`.
9. Fallback chain does **not** trigger on `InvalidResponseShape` or
   `ModelNotFound` (non-retryable by design).
10. `ContextWindowExceeded` only advances to a strictly-larger-context
    model, else re-raises.

## Testing

```bash
# Unit tests (172 tests across 14 files)
cd /root/leo/packages/llm
python -m pytest tests/ -q

# Integration with the other Tier-1 packages
cd /root/leo/packages
for p in core domain llm; do
  (cd "$p" && python -m pytest tests/ -q)
done
```

## Notes

- `boto3` and `tiktoken` are **optional** deps. Install with
  `pip install -e packages/llm[bedrock,tokens]` to enable Bedrock-direct
  routing and native tiktoken-based token counting. Without them, Bedrock
  routes raise `ProviderUnavailable` and `count_tokens` falls back to
  `len(text) // 4`.
- Back-compat shims (`LiteLLMClient`, `get_client`, `complete`,
  `complete_sync`) were removed at the end of Phase 4. Call sites now
  use `from llm import chat, chat_sync` exclusively.
