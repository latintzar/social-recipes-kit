# Set up Social Recipes with your agent

Social Recipes supplies a video-to-recipe extractor, a timed React player and Instagram webhook/reply helpers. Start with one video, then connect the Instagram inbox if your project needs it.

For a complete Instagram recipe conversation, use [the recovered agent runtime](agent-runtime.md): the original queue, history, Hermes turn loop and streamed replies, with your own Meta, vision and session-store configuration. This guide covers extraction and component integration. Vitrina Local adds the local-shop shopping workflow.

## Extract one recipe

From the repository root, use Python 3.10+ and install FFmpeg through your operating system:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e './packages/recipe-extractor[service]'
```

`yt-dlp` is installed by the Python package. Supply your own vision API key through the process environment or your secret manager. In Bash, hidden input keeps it out of command history:

```bash
read -r -s -p 'Vision API key: ' RECIPE_LLM_API_KEY
printf '\n'
export RECIPE_LLM_API_KEY
```

The extractor reads process environment values. A saved dotenv file needs to be loaded by your process manager or application before importing the package. The template lists fields; see [extractor configuration](../packages/recipe-extractor/.env.example).

| Field | Purpose |
|---|---|
| `RECIPE_LLM_API_KEY` or `OPENROUTER_API_KEY` | Your vision provider key; `OPENROUTER_API_KEY` takes precedence |
| `RECIPE_LLM_URL` | OpenAI-compatible chat-completions URL; default `https://openrouter.ai/api/v1/chat/completions` |
| `RECIPE_MODEL` | Vision model identifier supported by your account; source default `google/gemini-2.5-flash` |
| `RECIPE_OUTPUT_DIR` | Your recording/artifact directory; default `recipe_output` |

Select the endpoint, model and output directory before importing the package. Keep downloaded videos and recipes in storage appropriate for your application. An agent can check `ffmpeg -version`, package imports and whether required variable names are populated; it should report names and presence only.

When you are ready to send a video to your selected provider:

```sh
.venv/bin/recipe-extractor 'https://www.instagram.com/reel/YOUR_REEL/'
```

Use a video your account can access. Review the returned recipe, extraction status and saved media. Platform access and model accuracy are part of that first real result. [Extractor and player guide](../README.md#runtime-and-modules).

## Connect an Instagram inbox

The existing `make_router` integration supplies `GET` and `POST /instagram/webhook`. Mount it in your FastAPI application using the [Instagram integration guide](../INSTAGRAM_SETUP.md). The bundled `recipe-extractor-serve` command supplies `/extract`, `/synthesize` and `/videos`; Instagram routes are mounted by your application.

| Field | Purpose |
|---|---|
| `INSTAGRAM_PAGE_ACCESS_TOKEN` | Your connected account's Graph access token for resolving shares and sending replies |
| `INSTAGRAM_VERIFY_TOKEN` | Your webhook verification value, matching the Meta callback configuration |
| `INSTAGRAM_APP_SECRET` | Your Meta app secret for `X-Hub-Signature-256` verification |
| `INSTAGRAM_BUSINESS_ID` | Instagram account ID for the Facebook-token Send API branch |

Use your professional Instagram account and public HTTPS callback, subscribe to `messages`, and complete Meta's current permissions/review process for the accounts you intend to serve. The source selects the Instagram or Facebook Graph Send API branch from the token form; its implementation is in [instagram_webhook.py](../packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py).

The retained Graph adapters target `https://graph.instagram.com/v23.0` and `https://graph.facebook.com/v23.0`, as declared in [instagram_cdn.py](../packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py). Account authorization and token creation follow your chosen Meta app setup; the kit consumes that access token. Verify the configured API version and permissions with your account when commissioning the inbox.

`make_router(auto_reply=True)` sends the extracted title as a confirmation. Pass an `on_reel(sender_id, result)` callback to save the result and return your own recipe reply. The callback receives the same recipe and media result as the extractor. Commission intake and replies with a consenting test account, checking the GET challenge, signed POST, saved recipe and observed DM separately.

Keep tokens, downloaded videos and recipient records with the operator. An agent can prepare configuration and review source; the operator supplies secrets through a private input and chooses when to make provider requests or send messages.

## Source and licence

[Extractor](../packages/recipe-extractor/src/recipe_extractor/extract.py) · [HTTP service](../packages/recipe-extractor/src/recipe_extractor/service.py) · [Instagram guide](../INSTAGRAM_SETUP.md) · [Setup skill](../skills/social-recipes-setup/SKILL.md)

Original code and these setup instructions use the repository's [MIT licence](../LICENSE). Connected platforms, dependencies and source media retain their own terms.
