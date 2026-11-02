---
name: social-recipes-setup
description: Set up the Social Recipes Instagram agent, standalone video extractor or player with your own provider and Meta accounts.
---

# Social Recipes setup

For a complete Reel-to-recipe conversation, read [the runtime guide](../../docs/agent-runtime.md) and [runtime boundaries](../../docs/runtime-security.md). Use Python 3.14, the combined dependency lock, the explicit pinned Hermes checkout and the project launcher. It requires your own `SOCIAL_RECIPES_*` configuration and isolated state; its recipe profile preserves the original queue, session history and reply adapter.

For extraction, React playback or a custom inbox callback, read [the component setup guide](../../docs/AGENT-SETUP.md). Vitrina Local is the separate local-basket product; [the original broader skill and tools](../../docs/original-runtime.md) remain available for continuing that work.

Use the repository's Python package and existing entrypoints. Configure a supported vision endpoint/model through process environment before importing the package. The package reads `OPENROUTER_API_KEY` or `RECIPE_LLM_API_KEY`; a dotenv template alone does not load values. Keep secrets in your environment/secret manager or hidden local input and report names/presence only.

Start by checking Python, FFmpeg, package installation and output storage. Run one selected video only when the user asks to use the provider, then inspect extraction status, recipe and saved media.

For Instagram, read `INSTAGRAM_SETUP.md` and the actual `instagram_webhook.py`. The default HTTP service serves extraction/video routes; your application mounts `make_router` for `/instagram/webhook`. Configure their Meta account, access token, verify token, app secret and reachable HTTPS callback. The default `auto_reply` confirms the recipe title; their `on_reel` callback supplies recipe formatting and persistence. Guide account permission and test-message steps through their own Meta account.

Keep original data and dependency licences. Distinguish local checks, provider extraction, signed webhook acceptance and observed DM delivery when reporting the result.
