"""
`llm` — Leo LLM abstraction
==============================

Single entrypoint for chat, structured output, vision, embeddings,
model registry, prompts, token counting, and cost accounting.

The 53-symbol public API below is the **contract** for every caller
migrating off ``shared.llm`` + ``leo_agent.leo_agent``. New code should
import from here only::

    from llm import chat, DEFAULT_FAST, Message, ProviderUnavailable

Everything else (providers, prompt internals, deprecated shims) lives
in submodules and is accessible under ``llm.<submodule>`` but **not**
re-exported here.

Design rationale + per-symbol justification: see
``internal/refactor/packages/llm/design.md``.
"""

from llm.chat import chat, chat_sync
from llm.config import (
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    proxy_api_key,
    proxy_base_url,
)
from llm.embed import embed, embed_sync
from llm.images import (
    generate_image,
    generate_image_async,
    generate_image_via_chat,
    generate_image_with_references,
)
from llm.errors import (
    ContextWindowExceeded,
    InvalidResponseShape,
    LLMError,
    ModelNotFound,
    ProviderError,
    ProviderUnavailable,
    PromptNotFound,
    RateLimitError,
)
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
from llm.prompts import Prompt, get_prompt, render_prompt
from llm.providers.base import (
    ChatResponse,
    Message,
    ResponseFormat,
    StreamChunk,
    Tool,
    ToolCall,
    Usage,
)
from llm.providers.bedrock import (
    BEDROCK_CLAUDE_HAIKU,
    BEDROCK_CLAUDE_SONNET,
    BEDROCK_REGION,
)
from llm.stream import stream
from llm.structured import complete_structured, complete_structured_sync
from llm.tokens import count_message_tokens, count_tokens
from llm.usage import record_usage, register_observer, unregister_observer
from llm.vision import complete_vision, complete_vision_sync

__all__ = [
    # chat
    "chat",
    "chat_sync",
    # structured
    "complete_structured",
    "complete_structured_sync",
    # vision
    "complete_vision",
    "complete_vision_sync",
    # stream (skeleton, D10)
    "stream",
    # embed
    "embed",
    "embed_sync",
    # images
    "generate_image",
    "generate_image_async",
    "generate_image_via_chat",
    "generate_image_with_references",
    # canonical types
    "Message",
    "Tool",
    "ToolCall",
    "ChatResponse",
    "StreamChunk",
    "ResponseFormat",
    "Usage",
    # models
    "Model",
    "MODEL_REGISTRY",
    "INVALID_MODELS",
    "pick",
    "by_tier",
    "for_provider",
    "resolve_model",
    "DEFAULT_SMART",
    "DEFAULT_FAST",
    "DEFAULT_VISION",
    "DEFAULT_REASONING",
    "DEFAULT_LONG_CTX",
    "DEFAULT_CHEAP",
    "DEFAULT_IMAGE_GEN",
    # pricing
    "ModelPricing",
    "PRICING",
    "estimate_cost",
    # prompts
    "Prompt",
    "render_prompt",
    "get_prompt",
    # tokens
    "count_tokens",
    "count_message_tokens",
    # usage / observability
    "record_usage",
    "register_observer",
    "unregister_observer",
    # errors
    "LLMError",
    "ProviderError",
    "ProviderUnavailable",
    "RateLimitError",
    "ContextWindowExceeded",
    "InvalidResponseShape",
    "ModelNotFound",
    "PromptNotFound",
    # config
    "LITELLM_BASE_URL",
    "LITELLM_API_KEY",
    "proxy_base_url",
    "proxy_api_key",
    # provider constants (Wave 2 — from packages/domain)
    "BEDROCK_REGION",
    "BEDROCK_CLAUDE_HAIKU",
    "BEDROCK_CLAUDE_SONNET",
]
