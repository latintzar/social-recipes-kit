# Vitrina — product/backend/mcp

**Audience:** `customer` · **Python FastMCP server** — the single source
of truth for all Vitrina commerce tools.

> The audience is `customer` even though an AI (Claude/Cursor/ChatGPT App,
> or the hosted Hermes agent) holds the client — the AI acts on behalf of a
> shopper whose data is customer-scoped.

## External clients — how to connect

### Hosted service (Hetzner)

The MCP server runs on the Vitrina API on the Hetzner box, mounted at:

```
https://5-161-178-146-api.sslip.io/mcp/
```

> **Trailing slash is required.** The server accepts both `/mcp` and
> `/mcp/`, but the official MCP Python SDK's streamable-http client (and
> therefore Hermes) does not replay the request body across the
> `/mcp` → `/mcp/` 307 redirect — every initialize ends up looking like an
> empty POST and FastMCP returns 404. Configure clients with the slash
> form. `scripts/ops/vitrina-debug/mcp_probe.py` and `health.py` enforce
> this so it never silently regresses.

All requests require a bearer token:

```
Authorization: Bearer <VITRINA_MCP_BEARER_TOKEN>
```

#### Claude Desktop / Cursor (`~/.claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "vitrina": {
      "transport": "http",
      "url": "https://5-161-178-146-api.sslip.io/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

#### ChatGPT custom GPT

Add an Action with Schema URL `https://5-161-178-146-api.sslip.io/mcp`
and auth type **Bearer Token**.

#### OpenClaw / any MCP client

```
VITRINA_MCP_URL=https://5-161-178-146-api.sslip.io/mcp
VITRINA_MCP_BEARER_TOKEN=<token>
```

### Local development

```bash
cd apps/vitrina/product/backend/mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# stdio transport (Claude Desktop subprocess, no auth)
python server.py

# streamable-http transport (hosted, with optional bearer auth)
VITRINA_MCP_BEARER_TOKEN=dev python server.py --http --port 8787
```

Claude Desktop local config (stdio, no auth):

```json
{
  "mcpServers": {
    "vitrina-local": {
      "command": "python",
      "args": ["/abs/path/to/leo/apps/vitrina/product/backend/mcp/server.py"],
      "env": {}
    }
  }
}
```

## Layout

```
product/backend/mcp/
├── __init__.py
├── server.py        FastMCP entrypoint (stdio + streamable-http)
├── tools.py         the 9 tool handlers + descriptors
├── data.py          seed-JSON loader (shops, products, cards)
├── snapshots.py     two-track live-stock ledger (in-memory for now)
├── seed/
│   ├── shops.json       6 Sofia demo shops
│   └── products.json    their product catalogues
├── tests/
│   └── test_tools.py    smoke test for the 9 handlers
├── requirements.txt
└── README.md        (this file)
```

## Tool surface

Two sets:

**Static Sofia demo catalogue** (falls back to seed JSON when no POS
agent has reported yet):

| Tool | Purpose |
|---|---|
| `list_demo_shops(trade?)` | Six Bulgarian shops — 3 food, 3 hardware. |
| `list_products_in_shop(shop, in_stock_only=true)` | One shop's catalogue, live-stock overlay when a snapshot exists. |
| `search_across_shops(query, trade?, max_results=20, in_stock_only=true)` | Cross-shop search, BG or EN. |

**Live POS ingestion + Leo ledger** — the two-track model
(`shop_stock − leo_sold_delta = effective_stock`):

| Tool | Called by | Purpose |
|---|---|---|
| `report_pos_snapshot(shop_id, products, source?, host?, as_of?)` | A vendor-specific connector (e.g. `whp-reader` for Microinvest, `shopify-pull` for Shopify, …) — see `product/integrations/`. Cadence varies by vendor; ~60 s for on-prem readers. | Ingest read-only POS mirror (SELECT-only; never write back). |
| `get_live_pos_snapshot(shop_id?)` | Ops / debug / the web UI | Read merged two-track view. Omit `shop_id` for summary of all reporting shops. |
| `record_leo_sale(shop_id, product_id, quantity, order_ref?)` | Stripe-checkout success webhook | Bump `leo_sold_delta`; push notification to shop. |
| `shop_acknowledge_sales(shop_id, product_ids?, writeback_to_pos=false)` | Shopkeeper app's "fulfilled" tap | Zero `leo_sold_delta`. Writeback opt-in only. |
| `list_shop_notifications(shop_id?, limit=50)` | Shopkeeper app inbox | List pending Leo sales etc. |
| `get_mcp_audit_trail(limit=100)` | Ops / debug | Last N events (snapshots / sales / acks / notifications). |

## The two-track model (the thesis)

```
        POS database                  Leo
 (Microinvest WHP, Square, …)     (this server)
            │                          │
            │ vendor-specific          │
            │ connector pushes         │
            │ snapshots                │
            │ (whp-reader,             │
            │  shopify-pull, …)        │
            ▼                          ▼
     shop_stock track         leo_sold_delta track
            │                          │
            └────────── max ───────────┘
                        (0, a-b)
                           │
                           ▼
                   effective_stock
              (what we quote a customer)
```

Leo never writes to the customer's POS. The shopkeeper reconciles manually
(same as for any in-person cash sale) by tapping "fulfilled" in their
shopkeeper app — that zeroes our delta. A future opt-in "grant write
access" per-tenant would flip `writeback_to_pos=true` so Leo also pushes
the quantity adjustment into the POS directly.

## Imports

Shared capabilities come from `packages/` — never from another app and
never from another surface's internals:

```python
from core import Supabase, NotFound, CurrentUser   # planned: tenant scoping
from storage import ...                             # planned: snapshot persistence
```

Currently imports only the stdlib + `fastmcp`. The `core`/`storage`
hookups land in the next graduation step, once a second tenant exists
and the in-memory ledger needs to outlive a process restart.

## Why this folder has no `__init__.py` (deliberate)

**TL;DR — the name `mcp` is taken by an upstream pip package, so we
can't expose our folder as a Python package without breaking
`fastmcp`. We keep the folder named `mcp/` (the right name for an MCP
server, matches the surface registry) and load its modules at the top
level via `sys.path`.**

Long version:

- There is a real `mcp` PyPI package (the official MCP Python SDK,
  installed at `/usr/local/lib/python3.12/dist-packages/mcp/`).
- `fastmcp` — our only hard dependency — imports from it
  internally: `from mcp.types import Tool`, `from mcp.server import
  Server`, etc.
- If our `apps/vitrina/product/backend/mcp/` had an
  `__init__.py`, Python would treat it as an importable package named
  `mcp`. When Python resolves `from mcp.types import …` inside
  `fastmcp`, it walks `sys.path` in order and takes the first hit. If
  our folder is on `sys.path` as `mcp`, ours wins — and `fastmcp`
  crashes because we don't have a `types` submodule.
- **Removing `__init__.py` means Python does *not* treat our folder as
  the `mcp` package**, so `fastmcp`'s `from mcp.types import …`
  continues to resolve to the real SDK every time, regardless of
  sys.path order. The folder name is purely a filesystem label.
- Our sibling modules (`tools.py`, `data.py`, `snapshots.py`,
  `server.py`) are loaded as *top-level* modules by inserting this
  folder onto `sys.path` at the three entry points that need them:

  - `server.py` (the MCP entry point)
  - `../api/routes/stock.py` (the REST mirror that calls the same
    handlers)
  - `tests/conftest.py` (the pytest setup)

  Each of those files has 3–4 lines that look like:

  ```python
  if _HERE not in sys.path:
      sys.path.insert(0, _HERE)
  import tools  # noqa: E402
  ```

  The `noqa: E402` exists so linters don't "helpfully" delete the
  `sys.path` line and reorder the import above it.

- **Do not add an `__init__.py` back.** If a future refactor needs a
  proper package namespace for these modules, the right move is either
  (a) rename the folder to something that doesn't collide (e.g.
  `mcp_server/`) — at which point the surface registry in `AGENTS.md`
  also needs updating — or (b) make our package accessible only via
  a longer path like `backend.mcp` (requires `product/backend/` on
  PYTHONPATH and never `product/backend/mcp/` on PYTHONPATH). Both are
  more disruptive than the three `sys.path` lines we have today.

The upside of the current shape: the folder name is semantic (people
opening `backend/mcp/` immediately know what lives there), it matches
the surface registry, and the escape hatch is contained.

## What's still in-memory (and why)

`snapshots.py` stores `shop_stock`, `leo_sold_delta`, notifications, and
the audit trail as module-level dataclasses. This is deliberate while
the demo is single-process:

- Customer-side connectors (e.g. `whp-reader` for Microinvest) re-report
  every 60 s, so a process restart recovers within a minute. SaaS-pull
  connectors run on a similar cadence.
- `leo_sold_delta` is short-lived (hours at most) — a stripe-checkout
  retry can replay the sale on restart.
- Multi-process or multi-region deployments require Supabase-backed
  persistence; that swap touches only `snapshots.py`.

When we migrate: add `shop_stock_snapshots` and `leo_sale_ledger` tables
to the `packages/storage` schema catalogue and replace the in-memory
dataclasses with `core.Supabase` queries. No handler signature changes.

## See also

- Company decision: `apps/vitrina/company/decisions/architecture-2026-04.md`
- Connector protocol: `apps/vitrina/product/integrations/microinvest-pos/README.md`
- Old Node prototype: `apps/vitrina/product/scratch/experiments/mcp-server/`

## Magic links: location + checkout (for external clients)

Any MCP client — Hermes today; Claude Desktop / ChatGPT tomorrow — calls the same
two magic-link tools and receives channel-agnostic `vitrina.bg` short URLs:

```
location.create_link()           → { url: "https://vitrina.bg/location?s=ab12cd", expires_in: 600 }
checkout.create_link(cart_ids=[…]) → { url: "https://vitrina.bg/checkout?s=xy99zz", expires_in: 86400 }
```

Slugs are stored in `public.location_tokens` / `public.checkout_tokens` (migration `012_magic_link_tokens.sql`).

The web pages resolve slugs via the agent service:
- `GET  /agent/v1/magic/location/lookup?s=…`
- `POST /agent/v1/magic/location/confirm`
- `GET  /agent/v1/magic/checkout/lookup?s=…`
- `POST /agent/v1/magic/checkout/auth`
- `POST /agent/v1/magic/checkout/session`  ← creates Stripe session here, not in MCP

Stripe session creation is deferred to the checkout page so the user has reviewed
the basket and chosen auth before Stripe is involved. On `checkout.session.completed`
the webhook appends the order to `chat_threads.metadata.orders[]` so "where's my
order?" works from the same chat thread.

