# Original runtime and tool source

The Instagram agent and the local-grocery product shared a turn engine. This release preserves that implementation and makes the recipe experience its explicit entry point.

The original sources were selected from the retained Leo revision `dbac69422281b857b9425ee01bb1876ddbdb7c2c`. The Instagram work dates to April–May 2026; the retained runtime record marks it dormant from 7 May. [The lineage manifest](../provenance/original-runtime.json) maps each selected original path and hash to its public copy. Original deployment notes remain dated engineering references; [agent-runtime.md](agent-runtime.md) is the runnable public setup.

## Runtime modules

| Source | Responsibility |
| --- | --- |
| [instagram_dm.py](../original-runtime/apps/vitrina/product/backend/agent/platforms/instagram_dm.py) | Signed event admission, duplicate IDs, per-sender burst queue, turn orchestration and streamed Graph replies |
| [ig_webhook_inbound.py](../original-runtime/apps/vitrina/product/backend/agent/platforms/ig_webhook_inbound.py) | Normalize text, shared attachments and Meta messaging payloads |
| [ig_reel_permalink.py](../original-runtime/apps/vitrina/product/backend/agent/platforms/ig_reel_permalink.py) | Recover a public Reel URL through Graph media/share lookup |
| [instagram_platform.py](../original-runtime/apps/vitrina/product/backend/agent/platforms/instagram_platform.py) | Stable thread identity, channel formatting, bubble splitting and Graph transport helpers |
| [channel.py](../original-runtime/apps/vitrina/product/backend/agent/platforms/channel.py) | The shared load-history → build-turn → Hermes → persist-response loop |
| [session_bridge.py](../original-runtime/apps/vitrina/product/backend/agent/session_bridge.py) | Stable skill/system prompt, ephemeral channel context and conversation envelope |
| [session_source.py](../original-runtime/apps/vitrina/product/backend/agent/session_source.py) | Read and append thread history through the existing thread handlers |
| [agent_factory.py](../original-runtime/apps/vitrina/product/backend/agent/agent_factory.py) | Provider/SDK construction and the recipe MCP configuration |
| [chat_threads.py](../original-runtime/apps/vitrina/product/backend/mcp/chat_threads.py) | Thread/message persistence with owner checks |
| [recipe profile](../runtime/profiles/recipe/SKILL.md) / [MCP binding](../runtime/social_recipes_agent/mcp_server.py) | Recipe behavior and binding to the published extraction pipeline |

## The broader shopper profile

The preserved [vitrina-shopper SKILL](../original-runtime/apps/vitrina/skills/vitrina-shopper/SKILL.md) and [SOUL](../original-runtime/apps/vitrina/skills/vitrina-shopper/SOUL.md) describe the original conversational product, its tool choices and voice. Its [MCP server](../original-runtime/apps/vitrina/product/backend/mcp/server.py) and modules include:

- [Recipe tools](../original-runtime/apps/vitrina/product/backend/mcp/recipes_tools.py): asynchronous extraction, discovery, synthesis, substitutions and basket conversion.
- [Commerce tools](../original-runtime/apps/vitrina/product/backend/mcp/tools.py): product search, stock, carts and order operations.
- [Instagram tools](../original-runtime/apps/vitrina/product/backend/mcp/ig_channel_tools.py): channel-specific context and location handoff.
- [Background jobs](../original-runtime/apps/vitrina/product/backend/mcp/chat_jobs.py): progress/results attached to a conversation.
- [Original video pipeline](../original-runtime/apps/vitrina/product/backend/recipe_videos/extract_social_recipe.py): extraction, storage and local-shop matching used by the original product.
- Shared [core](../original-runtime/packages/core), [LLM](../original-runtime/packages/llm), [storage](../original-runtime/packages/storage) and [voice](../original-runtime/packages/voice) implementation needed to understand those paths.

These are source for continuing the broader Vitrina work. Its stock, location, payment, voice and memory integrations need their own configuration, schema and acceptance work. The public recipe launcher binds only the extraction tool. [Vitrina Local](https://github.com/latintzar/vitrina-local) is the separate recipe-to-local-basket product.

## Public portability changes

The original module structure and turn flow are retained. The changes are recorded per file in the lineage manifest:

- An explicit own-configuration launcher selects a pinned Hermes checkout and isolated project state.
- HMAC verification fails closed; webhook size and configured-account admission are bounded.
- Recipe mode skips location, profile-memory and basket operations and selects its own skill.
- The factory uses current SDK arguments and the single recipe MCP toolset. Its child environment contains the vision settings needed by that tool.
- Session access failures propagate in the public runtime. Its Supabase client uses the dedicated `social_recipes` schema.
- Automatic ancestor dotenv loading is disabled in this runtime. Trace output keeps operational metadata.

Original source hashes, historical records and unchanged tools make those adaptations distinguishable from the work being recovered.
