> Retained April–May 2026 implementation record. [Current public setup](../../../../../../docs/agent-runtime.md).

# backend/agent — Vitrina shopper agent legacy app shell

**Audience:** `customer` (shoppers). Runs the chat tab on web today;
Instagram DM is wired through the same legacy shell; TikTok DM remains a
scaffold. This surface is being migrated to the official self-hosted Hermes
runtime installed on the Hetzner box.

## Architecture

```
External AI clients (optional, future hosted layer):
  Claude Desktop / ChatGPT / Cursor / OpenClaw
        │
        │  uvx vitrina-mcp  (stdio)   OR   https://mcp.vitrina.bg/mcp  (hosted, future)
        ▼
                              ┌─────────────────────────────────────┐
vitrina-agent (Hetzner)       │ FastMCP server.py                   │
  ├─ uvicorn server:app       │   stdio transport (default)         │
  └─ spawns ──stdio──────────►│   --http opt-in for hosted layer    │
                              │ tools.HANDLERS (UCP / ACP / ICP)    │
                              └─────────────┬───────────────────────┘
                                            ▼
                                         Supabase
```

The Vitrina MCP runs as an **in-process stdio subprocess** of `vitrina-agent`
— the same packaging pattern Stripe (`@stripe/mcp`,
`StdioServerTransport`) and Supabase (`@supabase/mcp-server-supabase`,
`bin: dist/transports/stdio.js`) ship in their published packages. Hermes
spawns it via the standard `mcp_servers.<name>.command/args/env` config.

Why stdio instead of HTTP between agent and MCP? See
apps/vitrina/DEPLOY.md (`../../../../DEPLOY.md#mcp-transport`, historical internal reference) — short
version: streamable-HTTP is *stateful* (sessions, idle timeouts, 307
quirks, no transport-level reconnect in Hermes), and we don't use any
of its features. stdio is bulletproof and what every popular OSS MCP
ships first.

External AI clients connect either by spawning the same stdio subprocess
themselves (`uvx vitrina-mcp` / Claude Desktop config) or, in future,
through a hosted streamable-http endpoint we expose for partners. Both
paths reuse `apps/vitrina/product/backend/mcp/server.py` unchanged.

## What this service owns

- The Vercel AI-SDK SSE wire contract (`/v1/chat/stream`) — translated
  from Hermes' callbacks in `sse_adapter.py`.
- Channel adapters (`platforms/instagram_dm.py`, `tiktok_dm.py`) — same
  `channel.run_turn` as web; IG/TikTok layers are webhook + outbound
  transport only.
- Session-shape glue (`session_bridge.py`) — maps the frontend's
  `{thread_id, cart_key, cart_ids, user_location}` payload onto Hermes'
  `run_conversation` inputs.

## File map

```
agent/
├── server.py            FastAPI: /v1/chat/stream + DM webhooks + /health
├── repl.py              `python -m agent.repl` — terminal test harness
├── sse_adapter.py       Hermes callbacks → AI-SDK SSE frames + silent-turn safety net
├── agent_factory.py     legacy app-shell bridge (provider routing + MCP config)
├── session_bridge.py    Frontend payload → Hermes inputs (cache-stable system + ephemeral)
├── session_source.py    Supabase chat_threads load/persist — every channel uses this
├── tracing.py           Per-turn structured trace events (stdout JSON + opt-in Supabase)
├── vitrina_tools/       Namespace placeholder — tools are served by vitrina-mcp
├── platforms/           Channel adapters (one entry point: channel.run_turn)
│   ├── channel.py       Channel-agnostic turn runner — server.py + DMs + repl call this
│   ├── instagram_dm.py  Meta Graph webhook + DM delivery (same run_turn)
│   └── tiktok_dm.py     TikTok Business Messaging adapter (same shape)
└── tests/
    ├── test_sse_adapter.py     Wire-frame contract + silent-turn safety net + status events
    └── test_session_bridge.py  Cache-stable system message invariant
```

## Channel parity

Instagram DM and the web chat tab are not two agents — they both call
`platforms/channel.py::run_turn` with the same SKILL, tools, compatibility
Hermes bridge, and Supabase transcript. The only differences are **delivery**:
SSE + widgets on web vs Meta Graph send API + plain text on IG.

## How a turn flows

Every channel — web SSE, IG DM, TikTok DM, terminal repl — funnels
through `platforms/channel.run_turn`:

1. **Load a bounded recent history window** from Supabase via
   `session_source.load_history`. DM channels intentionally use a small
   window so old failed deploys, stale signed media URLs, or "MCP down"
   debug replies do not steer the current turn.
2. **Build TurnInputs** with `session_bridge.build_turn`. The static
   `system_message` is `SKILL.md` plus optional `SOUL.md`. The per-turn
   `ephemeral_system_prompt` carries the `<channel>` block + constraints.
3. **Persist the user turn** pre-flight so a crash mid-loop leaves a trail.
4. **Construct an `AIAgent`** via the compatibility `agent_factory.build_agent`.
   Hermes auto-discovers the `vitrina-mcp` toolset from the app-local
   `mcp_servers.vitrina` block in `$HERMES_HOME/config.yaml`.
5. **Run** `agent.run_conversation`. Web SSE runs this in a background
   thread; DMs and the repl wait for completion.
6. **Persist the assistant turn** (text + widgets) for rehydration.

## Long-term memory vs chat session

- **Persistent chat (e.g. Instagram DM):** One `thread_id` per sender (stable
  UUID from `instagram_platform.ig_thread_id`). Supabase stores the rolling
  transcript; each inbound DM is a new **turn**, not a new account. DM
  channels load a **short** history window (`_DM_HISTORY_LIMIT`, currently 8
  messages) so stale signed CDN URLs and old debug noise do not dominate the
  model context.
- **Hermes `session_id`:** Set to the same `thread_id` in the compatibility
  bridge so Hermes can correlate runs for that thread.
- **Memory partition (`user_id` on the agent):** `channel.run_turn` passes
  `memory_user_id` when it differs from the logged-in Supabase user — on IG
  this is `ig:{sender_psid}` while `user_id` stays `None` for anonymous carts.
- **What actually gets written:** The old app shell can still enable mem0 with
  `MEM0_API_KEY`, but the native-Hermes direction is profile-scoped memory on
  the official runtime. Vitrina still does **one explicit** `storage.remember`
  write when IG profile syncs (`instagram_identity`: @username / display name)
  so the handle survives across sessions.

## MCP connection

The legacy app shell writes an app-local `$HERMES_HOME/config.yaml` at boot.
On Hetzner that home is `/opt/vitrina/runtime/hermes-app-home`, deliberately
separate from the official runtime home at `/root/.hermes`. Stdio transport,
no network, no auth (subprocess is local):

```yaml
memory:
  provider: mem0

mcp_servers:
  vitrina:
    command: python
    args:
      - /app/mcp/server.py
    env:
      PYTHONPATH: /app/mcp:/app/recipe_videos:/app:/app/packages/...
      SUPABASE_URL: ${SUPABASE_URL}
      SUPABASE_SERVICE_ROLE_KEY: ${SUPABASE_SERVICE_ROLE_KEY}
      WEBSHARE_PROXY_USERNAME: ${WEBSHARE_PROXY_USERNAME}
      WEBSHARE_PROXY_PASSWORD: ${WEBSHARE_PROXY_PASSWORD}
      STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY}
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}
      # … environment forwarded explicitly by agent_factory.py
    timeout: 120
    connect_timeout: 30
```

Hermes' stdio launcher filters subprocess env to a tiny safe-by-default
allowlist (`PATH`, `HOME`, `USER`, `LANG`, `XDG_*`), so secrets the MCP needs
must be forwarded explicitly in the MCP server config.

Local-dev: nothing to set — `_resolve_mcp_command` falls back to the
in-repo `apps/vitrina/product/backend/mcp/server.py` when the
container path doesn't exist.

## Provider routing

`agent_factory.py` picks a provider:

1. `ANTHROPIC_API_KEY` present → direct Anthropic. **Preferred** — Hermes
   prompt caching only fires on direct Anthropic. Model ids are normalised
   for the API (e.g. `anthropic/claude-sonnet-4.6` → `claude-sonnet-4-6`).
   Console marketing names (e.g. newer Sonnet generations) must still resolve
   to an **id Anthropic’s API returns** — a bad slug causes silent stream
   failures, not a fallback model. Override with `VITRINA_AGENT_MODEL`.
2. `OPENROUTER_API_KEY` present → OpenRouter fallback.
3. Neither → stub-echo mode.

## Env reference

| Var | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | for prod | direct Anthropic, prompt caching |
| `OPENROUTER_API_KEY` | fallback | OpenRouter |
| `VITRINA_AGENT_PROVIDER` | optional | force `anthropic` or `openrouter` |
| `VITRINA_AGENT_MODEL` | optional | default `anthropic/claude-sonnet-4-5` |
| `HERMES_HOME` | legacy app shell | `/opt/vitrina/runtime/hermes-app-home` |
| `WEBSHARE_PROXY_USERNAME` / `_PASSWORD` | prod | forwarded to MCP subprocess for `yt-dlp` |
| `INSTAGRAM_*` | when DMs live | see `platforms/instagram_dm.py` |
| `TIKTOK_*` | when DMs live | see `platforms/tiktok_dm.py` |

## Run locally

The MCP starts itself as a stdio child of the agent — you only run the
agent process:

```bash
cd apps/vitrina/product/backend/agent
pip install -r requirements.txt
pip install -r ../mcp/requirements.txt
pip install -r ../recipe_videos/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
# Supabase / Webshare / Stripe vars from .env.local — these are forwarded
# to the MCP subprocess through the app-local Hermes config.
uvicorn server:app --port 8002 --reload
```

For live local agent turns, Hermes must be importable in the Python environment.
The production direction is the official installer runtime; do not add a
vendored Hermes tree to this repo.

To exercise the MCP standalone (e.g. for an external AI client like
Claude Desktop), run it with `--http`:

```bash
cd apps/vitrina/product/backend/mcp
python server.py --http --port 8787
# point your client at http://localhost:8787/mcp
```

## Tracing — the full conversation trail

`tracing.py` is the single source of truth for what happens inside a turn.
Every channel emits `[TRACE]` JSON lines that share one `turn_id`:

```bash
ssh root@5.161.178.146 'journalctl -u vitrina-agent -n 1000' | rg <turn_id>
```

| Event | Tells you |
|---|---|
| `turn.start` / `turn.finish` | Channel, thread, cart, status, elapsed_ms |
| `run_turn.built` | System + ephemeral prompt sizes, history length |
| `run_conversation.start/.done/.error` | Provider, model, latency, exceptions |
| `ig.webhook.event/.duplicate_mid` | Inbound IG bubble parsed / deduped |
| `ig.run_turn.pre/.post` | Pre-Hermes IG state and outcome |
| `ig.send.bubbles/.bubble` | One outbound DM per bubble |
| `sse.adapter.close/.fail` | Web stream close + fallback flag |

Optional Supabase persistence: apply migration `011_agent_traces.sql` and
set `VITRINA_AGENT_TRACE_SUPABASE=1`.

## Packaging status

Railway packaging for this agent is retired. Hetzner deployment is documented
in `apps/vitrina/deploy/hetzner/README.md`; the official Hermes runtime profile
is tracked in `apps/vitrina/deploy/hermes-profile-template/` and applied to
`/root/.hermes` during deploy.

## Magic links from chat

All short links the agent sends into chat are channel-agnostic. No IG-specific URLs.

| URL | Tool | TTL |
|---|---|---|
| `vitrina.bg/location?s=…` | `location.create_link` | 10 min |
| `vitrina.bg/checkout?s=…` | `checkout.create_link` | 24 h (single-use after successful pay) |

**Location flow** — agent calls `location.create_link` (no args needed, thread/cart
injected) → sends the short URL → shopper pins area in the browser → web page
POSTs to `/v1/magic/location/confirm` → agent fires a proactive DM and can now
run shop searches.

**Checkout flow** — agent calls `checkout.create_link` with `cart_ids` → mints a
`checkout_token` slug → sends `vitrina.bg/checkout?s=…` → shopper reviews basket →
auth gate (Google / email-password / guest+email) → page calls
`POST /v1/magic/checkout/session` → Stripe embedded iframe → on success webhook
appends order to `chat_threads.metadata.orders[]` so "where's my order?"
answers from the same thread.

The agent must **never** send raw `https://checkout.stripe.com/…` URLs or
`/ig/location` URLs — those no longer exist.

