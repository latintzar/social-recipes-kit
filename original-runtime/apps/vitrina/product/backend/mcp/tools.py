"""Tool handlers — the public surface that Claude / Cursor / ChatGPT call.

Port of the Node prototype's ``src/tools.js``. All handlers return plain
Python dicts — :mod:`server` wraps them into the MCP ``content`` envelope
expected by the JSON-RPC transport and :mod:`product.backend.api` exposes
the same functions as REST endpoints so the future web/mobile surface can
speak the exact same contract.

Two surfaces of tools:

1. **Sofia-demo static catalogue** — ``list_demo_shops``,
   ``list_products_in_shop``, ``search_across_shops``. Reads from
   :mod:`.data` with a live overlay from :mod:`.snapshots` when a POS
   agent has reported recently.
2. **Live POS ingestion + Leo ledger** — ``report_pos_snapshot``,
   ``get_live_pos_snapshot``, ``record_leo_sale``, ``shop_acknowledge_sales``,
   ``list_shop_notifications``, ``get_mcp_audit_trail``. All operate through
   :mod:`.snapshots`.

The server instructions below match the Node prototype verbatim so the
Claude connector's existing system prompt keeps working after cutover.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from pathlib import Path
import importlib.util
import sys
from typing import Any, Callable

import data
import snapshots

SERVER_NAME = "stock-project-mcp"
SERVER_VERSION = "0.3.0"
PROTOCOL_VERSION = "2025-11-25"

SERVER_INSTRUCTIONS = """\
You are connected to Leo's Sofia demo — six real-feeling Bulgarian shops plus
a LIVE inventory layer fed by per-vendor connectors (one per POS / ERP
system the shops run on).

The live layer has two tracks:
  * shop_stock        ← read-only mirror of the shop's POS (Microinvest WHP
                        in this demo, but every vendor has its own connector
                        — see product/integrations/). The connector polls
                        SELECT-only at ~60s and POSTs report_pos_snapshot.
                        Leo never writes back.
  * leo_sold_delta    ← what Leo has sold since the shop last reconciled,
                        via Stripe checkout. Tracked here, not in the POS.
  * effective_stock    = max(0, shop_stock - leo_sold_delta)
                        → this is what we should quote a customer.

Flow:
  1. list_demo_shops — orient.
  2. list_products_in_shop — catalogue + live effective_stock when a snapshot
     has been reported for that shop. Falls back to static demo stock when
     the sync agent hasn't reported yet.
  3. search_across_shops — cross-shop ingredient/material search.
  4. record_leo_sale — call after a Stripe checkout succeeds. The shop sees
     a new notification (list_shop_notifications) and confirms fulfilment
     in the Leo shopkeeper app, which calls shop_acknowledge_sales to zero
     the delta.
  5. get_live_pos_snapshot — verify the agent is alive (as_of < 2 min) and
     see per-product shop_stock / leo_sold / effective_stock."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize(text: str | None) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", str(text))
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return stripped.lower()


_VALID_TRADES = (
    "dairy",
    "butcher",
    "grocery-deli",
    "ironmonger-diy",
    "ironmonger-pro",
    "ironmonger-bath",
)


# ----------------------------- static catalogue ------------------------------


def handle_list_demo_shops(*, trade: str | None = None) -> dict[str, Any]:
    """Tool: ``list_demo_shops`` — the six Sofia demo shops."""
    shops = [s for s in data.all_shops() if not trade or s.get("trade") == trade]
    cards: list[dict[str, Any]] = []
    for s in shops:
        card = data.shop_card(s)
        card["tagline_en"] = s.get("tagline_en")
        card["product_count"] = len(data.products_for_shop(s["id"]))
        cards.append(card)
    return {
        "total_shops": len(cards),
        "city": "Sofia, Bulgaria",
        "currency": "EUR",
        "shops": cards,
        "hint": (
            'Call search_across_shops("<ingredient>") or '
            'list_products_in_shop("<shop-id>") next.'
            if cards
            else "No shops matched that trade."
        ),
    }


def handle_list_products_in_shop(
    *, shop: str, in_stock_only: bool = True
) -> dict[str, Any]:
    """Tool: ``list_products_in_shop`` — a shop's catalogue with live overlay."""
    resolved = data.resolve_shop(shop)
    if resolved is None:
        raise ValueError(f"unknown shop: {shop}")

    live_view = snapshots.effective_stock_view(resolved["id"])
    live_by_product: dict[str, dict[str, Any]] = {}
    if live_view:
        live_by_product = {p["product_id"]: p for p in live_view["products"]}

    rows: list[dict[str, Any]] = []
    for p in data.products_for_shop(resolved["id"]):
        live = live_by_product.get(p["id"])
        if in_stock_only:
            effective = live["effective_stock"] if live else (p.get("stock") or 0)
            if effective <= 0:
                continue
        card = data.product_card(p)
        if live:
            card.update(
                {
                    "shop_stock": live["shop_stock"],
                    "leo_sold_since_reconcile": live["leo_sold_since_reconcile"],
                    "effective_stock": live["effective_stock"],
                    "stock_source": live_view["source"] if live_view else None,
                    "stock_as_of": live_view["as_of"] if live_view else None,
                }
            )
        rows.append(card)

    out: dict[str, Any] = {
        "shop": data.shop_card(resolved),
        "products": rows,
        "currency": "EUR",
        "server_time": _now(),
    }
    if live_view:
        received = datetime.fromisoformat(live_view["received_at"].replace("Z", "+00:00"))
        age = int((datetime.now(timezone.utc) - received).total_seconds())
        out["live_snapshot"] = {
            "source": live_view["source"],
            "host": live_view["host"],
            "as_of": live_view["as_of"],
            "age_seconds": age,
            "leo_sold_pending": live_view["total_leo_sold_since_reconcile"],
        }
    else:
        out["live_snapshot"] = None
    return out


def handle_search_across_shops(
    *,
    query: str,
    trade: str | None = None,
    max_results: int = 20,
    in_stock_only: bool = True,
) -> dict[str, Any]:
    """Tool: ``search_across_shops`` — flat + grouped product matches."""
    q = _normalize(query)
    if not q:
        raise ValueError("query required")

    matches: list[dict[str, Any]] = []
    for s in data.all_shops():
        if trade and s.get("trade") != trade:
            continue
        live_view = snapshots.effective_stock_view(s["id"])
        live_by_product: dict[str, dict[str, Any]] = {}
        if live_view:
            live_by_product = {p["product_id"]: p for p in live_view["products"]}
        for p in data.products_for_shop(s["id"]):
            haystack = _normalize(
                " ".join(
                    [
                        p.get("name_bg") or "",
                        p.get("name_en") or "",
                        p.get("category") or "",
                        " ".join(p.get("tags") or []),
                    ]
                )
            )
            if q not in haystack:
                continue
            live = live_by_product.get(p["id"])
            stock = live["effective_stock"] if live else (p.get("stock") or 0)
            if in_stock_only and stock <= 0:
                continue
            matches.append(
                {
                    "product_id": p["id"],
                    "shop_id": s["id"],
                    "shop_name_bg": s["name_bg"],
                    "name_bg": p.get("name_bg"),
                    "name_en": p.get("name_en"),
                    "price_eur": p.get("price_eur"),
                    "stock": stock,
                    "is_live": bool(live),
                    "image": p.get("image"),
                }
            )

    matches.sort(key=lambda m: m.get("price_eur") or 0)
    trimmed = matches[:max_results]
    by_shop: dict[str, list[dict[str, Any]]] = {}
    for m in trimmed:
        by_shop.setdefault(m["shop_id"], []).append(m)
    return {
        "query": query,
        "matches": trimmed,
        "by_shop": [{"shop_id": sid, "items": items} for sid, items in by_shop.items()],
        "currency": "EUR",
        "server_time": _now(),
    }


# ----------------------- live POS ingestion / Leo ledger ----------------------


def handle_report_pos_snapshot(
    *,
    shop_id: str,
    products: list[dict[str, Any]],
    source: str | None = None,
    host: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Tool: ``report_pos_snapshot`` — ingest a read-only POS mirror."""
    result = snapshots.record_shop_snapshot(
        shop_id=shop_id,
        products=products,
        source=source,
        host=host,
        as_of=as_of,
    )
    snap = snapshots.get_shop_snapshot(shop_id)
    return {
        **result,
        "as_of": snap.as_of if snap else None,
        "received_at": snap.received_at if snap else None,
        "source": snap.source if snap else None,
        "host": snap.host if snap else None,
        "server_time": _now(),
        "hint": (
            "Snapshot stored read-only. Leo will not write back to your POS. "
            "Call get_live_pos_snapshot to verify."
        ),
    }


def handle_get_live_pos_snapshot(*, shop_id: str | None = None) -> dict[str, Any]:
    """Tool: ``get_live_pos_snapshot`` — read merged two-track view."""
    if not shop_id:
        summaries = []
        now = datetime.now(timezone.utc)
        for s in snapshots.all_shop_snapshots():
            received = datetime.fromisoformat(s.received_at.replace("Z", "+00:00"))
            summaries.append(
                {
                    "shop_id": s.shop_id,
                    "source": s.source,
                    "host": s.host,
                    "product_count": s.product_count,
                    "as_of": s.as_of,
                    "received_at": s.received_at,
                    "age_seconds": int((now - received).total_seconds()),
                }
            )
        return {
            "shops_with_snapshots": len(summaries),
            "shops": summaries,
            "server_time": _now(),
            "hint": (
                "Pass a shop_id to see merged shop_stock / leo_sold_delta / effective_stock."
                if summaries
                else "No snapshots yet. The sync agent on the shop PC should POST report_pos_snapshot every ~60s."
            ),
        }

    view = snapshots.effective_stock_view(shop_id)
    if view is None:
        return {
            "ok": False,
            "error": f'no snapshot reported yet for shop_id "{shop_id}"',
            "server_time": _now(),
        }
    received = datetime.fromisoformat(view["received_at"].replace("Z", "+00:00"))
    age = int((datetime.now(timezone.utc) - received).total_seconds())
    leo_sold = view["total_leo_sold_since_reconcile"]
    return {
        **view,
        "age_seconds": age,
        "server_time": _now(),
        "hint": (
            f"{leo_sold} unit(s) sold via Leo since last reconcile. effective_stock reflects this."
            if leo_sold > 0
            else "No pending Leo sales. effective_stock == shop_stock."
        ),
    }


def handle_record_leo_sale(
    *,
    shop_id: str,
    product_id: str,
    quantity: int,
    order_ref: str | None = None,
) -> dict[str, Any]:
    """Tool: ``record_leo_sale`` — bump ``leo_sold_delta`` + notify shop."""
    result = snapshots.record_leo_sale(
        shop_id=shop_id, product_id=product_id, quantity=quantity, order_ref=order_ref
    )
    snapshots.push_notification(
        shop_id=shop_id,
        kind="leo_sale",
        body={
            "product_id": product_id,
            "quantity": quantity,
            "order_ref": order_ref,
            "message": (
                f'Leo just sold {quantity} × {product_id}. '
                'Confirm "fulfilled" when the customer picks up.'
            ),
        },
    )
    return {
        **result,
        "server_time": _now(),
        "hint": (
            "Recorded in Leo's ledger. Your POS data was NOT touched. "
            "The shop sees a notification on next app open."
        ),
    }


def handle_shop_acknowledge_sales(
    *,
    shop_id: str,
    product_ids: list[str] | None = None,
    writeback_to_pos: bool = False,
) -> dict[str, Any]:
    """Tool: ``shop_acknowledge_sales`` — shopkeeper confirms fulfilment."""
    result = snapshots.shop_acknowledged_sales(
        shop_id=shop_id, product_ids=product_ids, writeback_to_pos=writeback_to_pos
    )
    return {
        **result,
        "server_time": _now(),
        "hint": (
            "Leo delta zeroed AND writeback to your POS was attempted."
            if result.get("writeback_performed")
            else "Leo delta zeroed. No changes were made to your POS (write access not granted)."
        ),
    }


def handle_list_shop_notifications(
    *, shop_id: str | None = None, limit: int = 50
) -> dict[str, Any]:
    """Tool: ``list_shop_notifications`` — Leo shopkeeper app inbox."""
    items = snapshots.list_notifications(shop_id=shop_id, limit=limit)
    return {"count": len(items), "notifications": items, "server_time": _now()}


def handle_get_mcp_audit_trail(*, limit: int = 100) -> dict[str, Any]:
    """Tool: ``get_mcp_audit_trail`` — last N audit events for ops/debug."""
    items = snapshots.get_audit(limit=limit)
    return {"count": len(items), "events": items, "server_time": _now()}


# ------------- descriptors (kept in lock-step with the Node prototype) --------


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_demo_shops",
        "description": (
            "List the six Bulgarian demo shops (3 food + 3 hardware) with addresses, hours today, "
            "phone, rating and hero photos. Call this first to orient yourself. Optional `trade` filter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"trade": {"type": "string", "enum": list(_VALID_TRADES)}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_products_in_shop",
        "description": (
            "Return the catalogue of one demo shop. If a live POS snapshot exists for this shop, "
            "the response is overlayed with `shop_stock`, `leo_sold_since_reconcile`, and `effective_stock`. "
            'Accepts the shop id (e.g. "mandra-rodopa") or a free-text name match.'
        ),
        "inputSchema": {
            "type": "object",
            "required": ["shop"],
            "properties": {
                "shop": {"type": "string"},
                "in_stock_only": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "search_across_shops",
        "description": (
            "Search all demo shops for products matching a query (BG or EN). "
            "Returns a flat list + by_shop grouping."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "trade": {"type": "string", "enum": list(_VALID_TRADES)},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                "in_stock_only": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "report_pos_snapshot",
        "description": (
            "Ingest a READ-ONLY snapshot of a shop's POS inventory. Called by the per-vendor "
            "connector that owns this shop's POS (e.g. `whp-reader` for Microinvest WHP, "
            "`shopify-pull` for Shopify, etc. — see product/integrations/). On-prem readers "
            "typically poll every ~60s with SELECT-only queries; Leo NEVER writes back. The "
            "snapshot replaces (not merges) the shop_stock track."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["shop_id", "products"],
            "properties": {
                "shop_id": {"type": "string"},
                "source": {"type": "string"},
                "host": {"type": "string"},
                "as_of": {"type": "string"},
                "products": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["product_id"],
                        "properties": {
                            "product_id": {"type": "string"},
                            "name_bg": {"type": "string"},
                            "name_en": {"type": "string"},
                            "barcode": {"type": "string"},
                            "price_eur": {"type": "number"},
                            "unit": {"type": "string"},
                            "stock": {"type": "number"},
                        },
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_live_pos_snapshot",
        "description": (
            "Read back the most recent POS snapshot for a shop, merged with Leo's sales delta. "
            "Omit shop_id to list all shops that have reported."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"shop_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "record_leo_sale",
        "description": (
            "Record that Leo just sold `quantity` units of `product_id` at `shop_id` "
            "(e.g. via Stripe checkout). Increments our leo_sold_delta ledger. The shop's POS is NOT touched."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["shop_id", "product_id", "quantity"],
            "properties": {
                "shop_id": {"type": "string"},
                "product_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "order_ref": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "shop_acknowledge_sales",
        "description": (
            "Shopkeeper confirms they fulfilled the Leo sales. Zeros leo_sold_delta for the given "
            "product_ids (or all if omitted). `writeback_to_pos` is OFF by default."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["shop_id"],
            "properties": {
                "shop_id": {"type": "string"},
                "product_ids": {"type": "array", "items": {"type": "string"}},
                "writeback_to_pos": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_shop_notifications",
        "description": (
            "List recent notifications for a shop (unread Leo sales, etc.). "
            "What the Leo shopkeeper app would render."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "shop_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_mcp_audit_trail",
        "description": (
            "Last N audit events (snapshots received, sales recorded, acknowledgements, notifications). "
            "For demos + ops."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100}
            },
            "additionalProperties": False,
        },
    },
    # ── ACP checkout surface (spec 2026-04-17) ──────────────────────────
    # These five tools implement the Agentic Commerce Protocol checkout
    # flow; click-and-collect only in v1 (single `fulfillment_options`
    # entry of type `pickup`). Every call requires
    # `meta.api_version == "2026-04-17"` per the ACP binding spec —
    # invalid version returns JSON-RPC -32602. Stock-race on complete
    # returns -32000. See checkout.py + checkout_handlers.py.
    {
        "name": "create_checkout_session",
        "description": (
            "Create an ACP checkout session for a cart of items at a single shop. Returns the "
            "full CheckoutSession body with fulfillment_options (pickup only, with live stock "
            "check), totals, and a Stripe payment_link.url the shopper can tap to pay. "
            "For multi-shop baskets pass payload.cart_ids; Vitrina creates one Stripe Checkout "
            "link for the group and later distributes store payouts through Stripe Connect. "
            "Pass payload.buyer.email if known — we send a branded receipt via Resend on "
            "completion. This is the first step of any transaction."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["meta", "payload"],
            "properties": {
                "meta": {
                    "type": "object",
                    "required": ["api_version"],
                    "properties": {"api_version": {"type": "string"}},
                },
                "payload": {
                    "type": "object",
                    "required": ["shop_id", "items"],
                    "properties": {
                        "shop_id": {"type": "string"},
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["id", "quantity"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "quantity": {"type": "integer", "minimum": 1},
                                },
                            },
                        },
                        "buyer": {"type": "object"},
                        "fulfillment_address": {"type": "object"},
                    },
                },
            },
        },
    },
    {
        "name": "get_checkout_session",
        "description": (
            "Retrieve the current state of a checkout session by id, including totals and live "
            "fulfillment options. Session id is the opaque HMAC-signed token returned from "
            "create_checkout_session."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["meta", "id"],
            "properties": {
                "meta": {"type": "object", "required": ["api_version"]},
                "id": {"type": "string"},
            },
        },
    },
    {
        "name": "update_checkout_session",
        "description": (
            "Update items, buyer, fulfillment_address, or selected fulfillment_option_id on an "
            "existing in_progress session. Cannot update a completed or canceled session. Note: "
            "updating reissues a new session id (the old one is invalidated)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["meta", "id", "payload"],
            "properties": {
                "meta": {"type": "object", "required": ["api_version"]},
                "id": {"type": "string"},
                "payload": {"type": "object"},
            },
        },
    },
    {
        "name": "complete_checkout_session",
        "description": (
            "Finalize the order. Decrements leo_sold_delta for each item (gated on current shelf "
            "stock — refuses with -32000 if another purchase raced ahead), mints an order_id + "
            "pickup collection_code, and fires a Resend receipt if buyer.email was set. "
            "Idempotent: calling on an already-completed session returns the same order payload."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["meta", "id"],
            "properties": {
                "meta": {"type": "object", "required": ["api_version"]},
                "id": {"type": "string"},
                "payload": {"type": "object"},
            },
        },
    },
    {
        "name": "cancel_checkout_session",
        "description": "Cancel an in-progress session. No-op on an already-canceled session.",
        "inputSchema": {
            "type": "object",
            "required": ["meta", "id"],
            "properties": {
                "meta": {"type": "object", "required": ["api_version"]},
                "id": {"type": "string"},
            },
        },
    },
    {
        "name": "reset_demo_stock",
        "description": (
            "DEV ONLY — zero all leo_sold_delta ledgers across all shops, so a demo can be rerun "
            "without a shopkeeper acknowledge. Does NOT modify POS snapshots."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "checkout.create_link",
        "description": (
            "Returns a short vitrina.bg/checkout?s=… magic link for any chat channel "
            "(Instagram, ChatGPT, Claude Desktop, etc.). "
            "Vitrina renders the basket + per-shop summary, runs auth-or-guest, "
            "and embeds Stripe Checkout. Never send raw Stripe URLs to the user. "
            "Requires meta.api_version and payload.cart_ids. Optional payload.slot for slot selection."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["meta", "payload"],
            "properties": {
                "meta": {
                    "type": "object",
                    "required": ["api_version"],
                    "properties": {"api_version": {"type": "string"}},
                },
                "payload": {
                    "type": "object",
                    "properties": {
                        "cart_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Open cart UUIDs to merge into one Stripe checkout.",
                        },
                        "slot": {"type": "object"},
                        "buyer": {"type": "object"},
                        "return_origin": {"type": "string"},
                    },
                },
            },
        },
    },
    {
        "name": "location.create_link",
        "description": (
            "Returns a short vitrina.bg/location?s=… URL where the shopper can pin their pickup area. "
            "Works for any chat channel (Instagram, ChatGPT, Claude Desktop, etc.). "
            "Requires thread_id and cart_key (injected from session)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "account.attach_email",
        "description": (
            "Store the shopper's email on the thread for cart transfer when they sign up on the web with the same email."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email"],
            "properties": {"email": {"type": "string", "format": "email"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "cart.transfer_by_email",
        "description": (
            "After web signup, reassign every chat thread + anonymous cart that stored this email to the new user. "
            "Call from the auth flow with the verified account email and target user_id (UUID)."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["email", "target_user_id"],
            "properties": {
                "email": {"type": "string"},
                "target_user_id": {"type": "string", "format": "uuid"},
            },
            "additionalProperties": False,
        },
    },
]


import checkout_handlers  # noqa: E402 — handlers module ported from Node ACP prototype


def _load_sibling_module(name: str) -> Any:
    """Load a sibling MCP module by file path.

    API route modules have overlapping names (for example
    ``api/routes/agent_errors.py``). Loading by path keeps the MCP tool
    dispatcher from accidentally binding to a REST adapter module when
    tests put both directories on ``sys.path``.
    """
    path = Path(__file__).resolve().with_name(f"{name}.py")
    module_name = f"_vitrina_mcp_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling MCP module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # If exec_module raises mid-import (missing dep, bad data file,
        # …), the partially-initialised module is still cached in
        # sys.modules. Without this cleanup, the *next* call to
        # `_load_sibling_module` returns the half-loaded shell — every
        # subsequent request then fails with confusing AttributeError
        # ("module … has no attribute RECIPE_TOOLS") instead of the real
        # underlying ImportError. Drop the cache so the next call retries
        # the load and surfaces the real error.
        sys.modules.pop(module_name, None)
        raise
    return module


agent_errors = _load_sibling_module("agent_errors")
cart = _load_sibling_module("cart")
chat_threads = _load_sibling_module("chat_threads")
replan = _load_sibling_module("replan")
shop_hours = _load_sibling_module("shop_hours")
recipes_tools = _load_sibling_module("recipes_tools")
ig_channel_tools = _load_sibling_module("ig_channel_tools")


UCP_CART_TOOLS: list[dict[str, Any]] = [
    {
        "name": "dev.ucp.shopping.cart.create",
        "description": "Create an open UCP cart for one shop. Anonymous callers pass cart_key; authed callers pass user_id.",
        "inputSchema": {
            "type": "object",
            "required": ["shop_id"],
            "properties": {
                "shop_id": {"type": "string"},
                "user_id": {"type": "string"},
                "cart_key": {"type": "string"},
                "currency": {"type": "string", "default": "EUR"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dev.ucp.shopping.cart.get",
        "description": "Fetch one cart with line items and totals. Requires matching user_id or cart_key when supplied.",
        "inputSchema": {
            "type": "object",
            "required": ["cart_id"],
            "properties": {
                "cart_id": {"type": "string"},
                "user_id": {"type": "string"},
                "cart_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dev.ucp.shopping.cart.add_item",
        "description": "Add or increment a product line. Supports recipe group_id + group_metadata for chat-created recipes.",
        "inputSchema": {
            "type": "object",
            "required": ["cart_id", "product_id", "quantity"],
            "properties": {
                "cart_id": {"type": "string"},
                "product_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
                "group_id": {"type": "string"},
                "group_metadata": {"type": "object"},
                "replace": {"type": "boolean", "default": False},
                "user_id": {"type": "string"},
                "cart_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dev.ucp.shopping.cart.update_item",
        "description": "Update a cart line quantity. Quantity 0 deletes the line.",
        "inputSchema": {
            "type": "object",
            "required": ["cart_id", "line_id", "quantity"],
            "properties": {
                "cart_id": {"type": "string"},
                "line_id": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 0},
                "user_id": {"type": "string"},
                "cart_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dev.ucp.shopping.cart.substitute_in_recipe",
        "description": "Replace one product inside a recipe group and append a substitution trail to group_metadata.",
        "inputSchema": {
            "type": "object",
            "required": ["cart_id", "group_id", "original_product_id", "replacement_product_id", "reason"],
            "properties": {
                "cart_id": {"type": "string"},
                "group_id": {"type": "string"},
                "original_product_id": {"type": "string"},
                "replacement_product_id": {"type": "string"},
                "reason": {"type": "string"},
                "user_id": {"type": "string"},
                "cart_key": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dev.ucp.shopping.cart.transfer",
        "description": "Move anonymous carts and matching chat_threads from cart_keys to a logged-in user_id.",
        "inputSchema": {
            "type": "object",
            "required": ["cart_keys", "user_id"],
            "properties": {
                "cart_keys": {"type": "array", "items": {"type": "string"}},
                "user_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "dev.ucp.shopping.cart.list_user_carts",
        "description": "List open carts belonging to a logged-in user.",
        "inputSchema": {
            "type": "object",
            "required": ["user_id"],
            "properties": {"user_id": {"type": "string"}},
            "additionalProperties": False,
        },
    },
]

SHOP_HOURS_TOOLS: list[dict[str, Any]] = [
    {
        "name": "shop_hours.is_open_now",
        "description": "Return open/closed status and next_open_at for a shop using its timezone-aware opening_hours.",
        "inputSchema": {
            "type": "object",
            "required": ["shop_id"],
            "properties": {"shop_id": {"type": "string"}, "at_ts": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "shop_hours.validate_cart_shops_open",
        "description": "Validate every cart's shop is open before checkout; returns closed carts with next_open_at.",
        "inputSchema": {
            "type": "object",
            "required": ["cart_ids"],
            "properties": {"cart_ids": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False,
        },
    },
]

REPLAN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "replan.find_alternatives",
        "description": "Find nearby open shops carrying some/all items from a closed shop cart.",
        "inputSchema": {
            "type": "object",
            "required": ["closed_shop_id", "items", "location"],
            "properties": {
                "closed_shop_id": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}},
                "location": {"type": "object"},
                "radius_km": {"type": "number", "default": 5.0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "replan.propose_consolidation",
        "description": "Score replace-one vs consolidate-all options from candidate shops.",
        "inputSchema": {
            "type": "object",
            "required": ["active_cart_ids", "candidate_shops"],
            "properties": {
                "active_cart_ids": {"type": "array", "items": {"type": "string"}},
                "candidate_shops": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "replan.ask_user_constraints",
        "description": "Return a clarify_question widget payload for missing distance/time/guests/budget/dietary constraints.",
        "inputSchema": {
            "type": "object",
            "required": ["missing"],
            "properties": {"missing": {"type": "array", "items": {"type": "string"}}},
            "additionalProperties": False,
        },
    },
    {
        "name": "replan.propose_recipe_swap",
        "description": "Return 2-3 recipe alternatives for a recipe_group_id under constrained inventory.",
        "inputSchema": {
            "type": "object",
            "required": ["recipe_group_id", "reason"],
            "properties": {
                "recipe_group_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]

CHECKOUT_GROUP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "complete_checkout_group",
        "description": "Complete every checkout session in a multi-shop checkout group atomically after Stripe payment clears.",
        "inputSchema": {
            "type": "object",
            "required": ["meta", "group_id"],
            "properties": {
                "meta": {"type": "object", "required": ["api_version"]},
                "group_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]


TOOLS.extend(
    [
        *UCP_CART_TOOLS,
        *SHOP_HOURS_TOOLS,
        *REPLAN_TOOLS,
        *chat_threads.CHAT_TOOLS,
        *agent_errors.AGENT_ERROR_TOOLS,
        *CHECKOUT_GROUP_TOOLS,
        *recipes_tools.RECIPE_TOOLS,
    ]
)

HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_demo_shops": handle_list_demo_shops,
    "list_products_in_shop": handle_list_products_in_shop,
    "search_across_shops": handle_search_across_shops,
    "report_pos_snapshot": handle_report_pos_snapshot,
    "get_live_pos_snapshot": handle_get_live_pos_snapshot,
    "record_leo_sale": handle_record_leo_sale,
    "shop_acknowledge_sales": handle_shop_acknowledge_sales,
    "list_shop_notifications": handle_list_shop_notifications,
    "get_mcp_audit_trail": handle_get_mcp_audit_trail,
    "create_checkout_session": checkout_handlers.handle_create_checkout_session,
    "get_checkout_session": checkout_handlers.handle_get_checkout_session,
    "update_checkout_session": checkout_handlers.handle_update_checkout_session,
    "complete_checkout_session": checkout_handlers.handle_complete_checkout_session,
    "cancel_checkout_session": checkout_handlers.handle_cancel_checkout_session,
    "complete_checkout_group": checkout_handlers.handle_complete_checkout_group,
    "checkout.create_link": checkout_handlers.handle_checkout_create_link,
    "reset_demo_stock": checkout_handlers.handle_reset_demo_stock,
    "dev.ucp.shopping.cart.create": cart.create_cart,
    "dev.ucp.shopping.cart.get": cart.get_cart,
    "dev.ucp.shopping.cart.add_item": cart.add_cart_item,
    "dev.ucp.shopping.cart.update_item": cart.update_cart_item,
    "dev.ucp.shopping.cart.substitute_in_recipe": cart.substitute_in_recipe,
    "dev.ucp.shopping.cart.transfer": cart.transfer_cart,
    "cart.transfer_by_email": cart.transfer_by_email,
    "dev.ucp.shopping.cart.list_user_carts": cart.list_user_carts,
    "shop_hours.is_open_now": shop_hours.is_shop_open_now,
    "shop_hours.validate_cart_shops_open": shop_hours.validate_cart_shops_open,
    "replan.find_alternatives": replan.find_alternatives,
    "replan.propose_consolidation": replan.propose_consolidation,
    "replan.ask_user_constraints": replan.ask_user_constraints,
    "replan.propose_recipe_swap": replan.propose_recipe_swap,
    "chat.create_thread": chat_threads.create_thread,
    "chat.get_thread": chat_threads.get_thread,
    "chat.list_user_threads": chat_threads.list_user_threads,
    "chat.list_threads_by_key": chat_threads.list_threads_by_key,
    "chat.pin_thread": chat_threads.pin_thread,
    "chat.update_thread_title": chat_threads.update_thread_title,
    "chat.delete_thread": chat_threads.delete_thread,
    "chat.append_message": chat_threads.append_message,
    "chat.list_messages": chat_threads.list_messages,
    "chat.seed_thread_from_order": chat_threads.seed_thread_from_order,
    "agent_errors.create": agent_errors.create_error,
    "agent_errors.get": agent_errors.get_error,
    "agent_errors.list_user": agent_errors.list_user_errors,
    "agent_errors.list_unresolved": agent_errors.list_unresolved,
    "agent_errors.resolve": agent_errors.mark_resolved,
    **ig_channel_tools.HANDLERS,
    **recipes_tools.RECIPE_HANDLERS,
}


def dispatch(tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call a tool by name; used by both transports + by tests."""
    handler = HANDLERS.get(tool_name)
    if handler is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    return handler(**(arguments or {}))
