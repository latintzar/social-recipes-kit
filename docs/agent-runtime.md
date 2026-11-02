# Instagram agent runtime

Social Recipes turns a cooking Reel sent to your Instagram account into a recipe in the same conversation. The recovered runtime adds the original burst queue, conversation history, Hermes turn loop, streaming reply adapter and shopper skill/tool source to the extraction kit.

## One conversation

1. A person sends a Reel or follows up with a text message.
2. Meta sends a signed event to `/v1/instagram/webhook`. The adapter verifies your app's HMAC and admits events for your configured Instagram business account.
3. The original quiet-window queue combines nearby messages from that sender. Message IDs suppress repeat webhook deliveries within the process.
4. The server derives a stable thread UUID and `ig:{sender_id}` ownership key, then loads that conversation's previous turns from your session store.
5. Hermes receives the recipe profile and current shared URLs. Its recipe MCP tool calls the existing extractor: media → selected frames → your vision provider → recipe and source video.
6. The original Instagram streaming adapter sends complete text bubbles and saves the assistant turn for the next message.

The recipe profile exposes one extraction tool. Vitrina Local adds stock matching, baskets and checkout above this layer; the preserved original shopper tools and skill are described in [the source history](original-runtime.md).

## Run it

The verified agent setup is Linux x86_64 with Python **3.14**, Git, FFmpeg and `uv`. The standalone extraction package retains its separate Python 3.10+ setup in [AGENT-SETUP.md](AGENT-SETUP.md).

```sh
git clone https://github.com/latintzar/social-recipes-kit.git
cd social-recipes-kit
uv venv --python 3.14
uv pip sync --python .venv/bin/python --require-hashes requirements-agent.lock
uv pip install --python .venv/bin/python --no-deps -e .

git clone https://github.com/NousResearch/hermes-agent.git ../social-recipes-hermes
git -C ../social-recipes-hermes checkout --detach f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab
```

The launcher imports that explicit, unmodified SDK checkout through its own child process. The combined lock includes the SDK's core, MCP and Anthropic dependencies and its [upstream security overrides](../requirements-hermes-overrides.in). Its pinned revision and licence are recorded in [hermes.lock.json](../runtime/hermes.lock.json). Keep the checkout dedicated to this project.

### Your accounts

Copy [the environment template](../.env.agent.example) to a private file and populate the `SOCIAL_RECIPES_*` values. Load it into your shell using your usual private secret manager. These values configure this project only:

| Setting | What you supply |
| --- | --- |
| `PROVIDER`, `MODEL`, `API_KEY` | Your explicit `openrouter` or `anthropic` account and model |
| `VISION_BASE_URL`, `VISION_MODEL`, `VISION_API_KEY` | Your OpenAI-compatible vision service; required together for Anthropic, optional together for OpenRouter |
| `STATE_DIR` | Absolute path to a new empty private directory for this project's Hermes config and downloaded recipe media |
| `HERMES_SOURCE` | Absolute path to the dedicated checkout at the revision above |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | Your project's HTTPS URL and server-only service key |
| `IG_APP_SECRET`, `IG_VERIFY_TOKEN` | Your Meta app secret and a random verification token, each at least 16 characters |
| `IG_ACCESS_TOKEN`, `IG_BUSINESS_ID` | Your Instagram Messaging token and business-account ID |

Every name in that table has the prefix `SOCIAL_RECIPES_`. Ambient provider variables and installed Hermes accounts stay outside the launcher's selected environment. Configuration and SDK checks complete before the server starts. Runtime keys belong on the server; the MCP child receives only the vision credentials it needs.

### Session store

Review and apply [001_sessions.sql](../runtime/sql/001_sessions.sql) in **your own Supabase project**, then add `social_recipes` to its exposed API schemas. The dedicated schema contains `chat_threads` and `chat_messages`. Its grants are server-only, with RLS enabled; sender ownership is checked by the original thread handlers. The public HTTP entry accepts Meta webhooks and a health check; session CRUD is internal.

Use a dedicated project/schema as the migration creates that schema's session tables. Original Vitrina commerce migrations are retained as historical source and belong to the optional commerce implementation.

### Start and connect Meta

```sh
.venv/bin/social-recipes-agent check
.venv/bin/social-recipes-agent serve --host 127.0.0.1 --port 8080
```

Put the local server behind your HTTPS reverse proxy. Configure Meta's callback as `https://your-host/v1/instagram/webhook`, with your verification token, and subscribe your professional account to messaging events. [INSTAGRAM_SETUP.md](../INSTAGRAM_SETUP.md) explains the account, permissions and subscription checks; its extraction callback example is the smaller kit option. Send your own cooking Reel and follow up about its ingredients to verify the complete connection.

## Operation and status

This is a recovered prototype runtime, with a recipe-only public entry and preserved original broader source. Offline checks cover signature admission, the original quiet-window queue, thread ownership/history, tool binding, streamed replies and SDK integration. A current Meta account, reachable social video and your chosen model are separate live acceptance checks with your own credentials.

Run one process/replica: burst buffers and the message-ID cache are process-local. A restart clears queued work. Production delivery that needs durable retries or multiple workers should extend those exact boundaries with a shared queue and idempotency store. Conversation content lives in your session database; runtime traces default to operational metadata. Voice attachments receive a prompt to send a Reel or text in this profile.

[Source map](original-runtime.md) · [Product tour](product-tour.md) · [Security boundary](runtime-security.md)
