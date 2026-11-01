---
name: social-recipes-setup
description: Set up the Social Recipes video extractor, player or Instagram webhook with the operator's own vision and Meta accounts.
---

# Social Recipes setup

Read [the agent setup guide](../../docs/AGENT-SETUP.md) and choose the module the operator needs: video extraction, React playback or Instagram intake. The published package supplies those components; the separately described original agent runtime is under reconciliation.

Use the repository's Python package and existing entrypoints. Configure a supported vision endpoint/model through process environment before importing the package. The package reads `OPENROUTER_API_KEY` or `RECIPE_LLM_API_KEY`; a dotenv template alone does not load values. Keep secrets in the operator's environment/secret manager or hidden local input and report names/presence only.

Start by checking Python, FFmpeg, package installation and output storage. Run one selected video only when the operator asks to use the provider, then inspect extraction status, recipe and saved media.

For Instagram, read `INSTAGRAM_SETUP.md` and the actual `instagram_webhook.py`. The default HTTP service serves extraction/video routes; the operator's application mounts `make_router` for `/instagram/webhook`. Configure their Meta account, access token, verify token, app secret and reachable HTTPS callback. The default `auto_reply` confirms the recipe title; their `on_reel` callback supplies recipe formatting and persistence. Guide account permission and test-message steps through their own Meta account.

Keep original data and dependency licences. Distinguish local checks, provider extraction, signed webhook acceptance and observed DM delivery when reporting the result.
