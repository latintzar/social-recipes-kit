"""Tool handlers for the ACP / UCP checkout surface.

Plain Python functions implementing the ACP checkout verbs (plus a
multi-shop ``handle_complete_checkout_group`` for the Phase 1 UCP
extension):

- ``handle_create_checkout_session`` — single-shop (legacy
  ``{shop_id, items}``) AND multi-shop (``{cart_ids: [...]}``).
- ``handle_get_checkout_session``
- ``handle_update_checkout_session``
- ``handle_complete_checkout_session``  (routes to group complete if
  the session has a ``group_id``)
- ``handle_cancel_checkout_session``
- ``handle_complete_checkout_group`` (thin wrapper over
  :func:`checkout.complete_checkout_group`)
- ``handle_reset_demo_stock`` (dev helper)

All business logic — state machine, DB writes, Stripe — lives in
:mod:`checkout`; these wrappers handle shape validation (ACP
``meta.api_version`` gate, ``-32602`` / ``-32000`` JSON-RPC codes) and
ownership checks. See :mod:`checkout` for design notes.

The legacy back-compat path (``{shop_id, items}``) is preserved: we
mint a cart row + line items on the fly and then go through the new
DB-backed flow. This keeps existing agent system prompts working after
the A3 rewrite.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

import os

import chat_threads

logger = logging.getLogger(__name__)
import checkout
import checkout_tokens
import data
import snapshots
import supabase_client


def _public_base_url() -> str:
    raw = (
        os.getenv("VITRINA_PUBLIC_URL")
        or os.getenv("VITRINA_PUBLIC_HOST")
        or os.getenv("VITRINA_PUBLIC_BASE_URL")
        or "https://vitrina.bg"
    )
    raw = raw.strip().rstrip("/")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    # Env often sets VITRINA_PUBLIC_HOST=vitrina.bg — without a scheme the URL is unusable in chat.
    if raw and "://" not in raw:
        return f"https://{raw.lstrip('/')}"
    return raw


def _buyer_email_from_thread(thread_id: str | None, cart_key: str | None) -> str | None:
    """Use ``chat_threads.metadata.email`` when the shopper shared it in DM/chat."""
    if not thread_id or not cart_key:
        return None
    try:
        row = chat_threads.get_thread(thread_id, cart_key=cart_key)
    except Exception:
        return None
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    em = meta.get("email")
    if isinstance(em, str) and "@" in em:
        return em.strip().lower()[:320]
    return None


class ACPError(Exception):
    """Carries an MCP/JSON-RPC error code + message.

    - ``-32602`` — invalid params (bad API version, missing fields,
      unknown SKU). The agent can fix and retry.
    - ``-32000`` — application error (stock race, invalid state
      transition, shop closed, session already terminal). The agent
      should surface and stop.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _check_api_version(meta: dict[str, Any] | None) -> None:
    if not isinstance(meta, dict) or meta.get("api_version") != checkout.ACP_API_VERSION:
        raise ACPError(
            -32602,
            f"meta.api_version must be '{checkout.ACP_API_VERSION}'",
        )


def _validate_shop_and_items(shop_id: str, items: Any) -> list[dict[str, Any]]:
    if not shop_id:
        raise ACPError(-32602, "payload.shop_id required")
    if data.get_shop(shop_id) is None:
        raise ACPError(-32602, f"unknown shop_id: {shop_id}")
    if not isinstance(items, list) or not items:
        raise ACPError(-32602, "payload.items[] required and non-empty")
    normalized: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or "id" not in it:
            raise ACPError(-32602, "each item must be {id, quantity}")
        prod = data.get_product(it["id"])
        if prod is None:
            raise ACPError(-32602, f"unknown sku: {it['id']}")
        if str(prod.get("shop_id")) != str(shop_id):
            raise ACPError(
                -32602,
                f"sku {it['id']} belongs to shop {prod.get('shop_id')}, not {shop_id}",
            )
        qty = int(it.get("quantity") or 1)
        if qty < 1:
            raise ACPError(-32602, f"quantity for {it['id']} must be >= 1")
        normalized.append({"id": it["id"], "quantity": qty})
    return normalized


def _assert_session_access(
    session_row: dict[str, Any], *, user_id: str | None, cart_key: str | None
) -> None:
    """If caller supplied user_id / cart_key, verify it matches the cart.

    Callers without either token are allowed (agent-native path with a
    shared secret at the transport layer). This mirrors the cart module's
    approach; the gate is defence-in-depth on top of RLS.
    """
    if not user_id and not cart_key:
        return
    try:
        sb = checkout._get_supabase()
        cart = (
            sb.table("carts")
            .select("user_id,cart_key")
            .eq("id", session_row.get("cart_id"))
            .single()
            .execute()
        )
    except Exception:
        return
    if not cart:
        return
    if user_id and cart.get("user_id") == user_id:
        return
    if cart_key and cart.get("cart_key") == cart_key:
        return
    raise ACPError(-32602, "session_access_denied")


# ── legacy back-compat: on-the-fly cart ─────────────────────────────────


def _mint_legacy_cart(
    shop_id: str, items: list[dict[str, Any]], buyer: dict[str, Any] | None
) -> str:
    """Create a transient cart + line items for legacy single-shop callers.

    The old ACP surface took ``{shop_id, items}`` directly — no UCP cart
    involved. We keep that contract alive by minting a cart row on the
    fly so downstream machinery (sessions, orders, webhook fan-out) all
    sees the same shape regardless of how the agent created the checkout.
    """
    sb = checkout._get_supabase()
    cart_key = f"legacy_{secrets.token_hex(8)}"
    cart_row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "shop_id": shop_id,
        "cart_key": cart_key,
        "user_id": None,
        "currency": (data.get_shop(shop_id) or {}).get("currency") or "EUR",
        "status": "open",
    }
    # NB: the `carts` table does NOT have a `buyer` column. The buyer
    # email is forwarded to Stripe directly via `customer_email` on the
    # Checkout Session (see ``checkout.create_stripe_checkout_link``);
    # we don't need it on the cart row itself for any downstream step.
    inserted = sb.table("carts").insert(cart_row).execute()
    row = inserted[0] if isinstance(inserted, list) else inserted
    cart_id = (row or cart_row)["id"]

    line_rows: list[dict[str, Any]] = []
    for it in items:
        p = data.get_product(it["id"]) or {}
        unit_minor = checkout.price_minor(p)
        qty = int(it["quantity"])
        subtotal = unit_minor * qty
        line_rows.append(
            {
                "cart_id": cart_id,
                "product_id": it["id"],
                "quantity": qty,
                "base_amount_minor": subtotal,
                "subtotal_minor": subtotal,
                "tax_minor": 0,
                "total_minor": subtotal,
                "product_snapshot": {
                    "shop_id": shop_id,
                    "price_eur": p.get("price_eur"),
                    "price_minor": unit_minor,
                    "name": p.get("name_en") or p.get("name_bg") or it["id"],
                    "name_en": p.get("name_en"),
                    "name_bg": p.get("name_bg"),
                },
            }
        )
    if line_rows:
        sb.table("cart_line_items").insert(line_rows).execute()
    return cart_id


# ── create ─────────────────────────────────────────────────────────────


def handle_create_checkout_session(
    *,
    meta: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an ACP checkout session.

    Two input shapes are accepted:

    * Legacy single-shop::

          {"shop_id": "...", "items": [{id, quantity}], "buyer": {...}?}

      We mint a cart on the fly, create ONE ``checkout_sessions`` row,
      and hand back the ACP body with a Stripe link attached.

    * Multi-shop (Phase 1 UCP)::

          {"cart_ids": ["uuid1", "uuid2", ...], "buyer": {...}?}

      Every cart must belong to a shop that's currently open (per
      :func:`shop_hours.validate_cart_shops_open`). We create N
      sibling ``checkout_sessions`` rows sharing a ``group_id`` and
      one Embedded Stripe Checkout Session. Returns
      ``{group_id, sessions: [...], client_secret, ...}``.
    """
    _check_api_version(meta)
    payload = payload or {}
    buyer = payload.get("buyer") if isinstance(payload.get("buyer"), dict) else None
    buyer_email = (buyer or {}).get("email") if isinstance(buyer, dict) else None
    # Frontend passes window.location.origin so Stripe's return_url points
    # back to the same host the user started on (production, custom domain,
    # or preview deploy). Validation lives in ``checkout._resolve_return_url``.
    return_origin_raw = payload.get("return_origin")
    return_origin = (
        return_origin_raw if isinstance(return_origin_raw, str) else None
    )

    # ── multi-shop path ─────────────────────────────────────────────
    cart_ids = payload.get("cart_ids")
    if isinstance(cart_ids, list) and cart_ids:
        # Slot drives the pre-flight: priority/Now requires shops open
        # now; standard windows just need the shop open during the
        # booked window. See :func:`checkout.create_multishop_group`.
        slot_payload = payload.get("slot")
        slot_arg: dict[str, Any] | None = (
            slot_payload if isinstance(slot_payload, dict) else None
        )
        try:
            group = checkout.create_multishop_group(
                [str(c) for c in cart_ids], slot=slot_arg
            )
        except checkout.ShopClosedError as exc:
            raise ACPError(
                -32000,
                "shop_closed",
                data={"closed_carts": exc.closed_carts},
            ) from exc
        stripe_result = checkout.create_single_stripe_session_for_group(
            group["group_id"],
            buyer_email=buyer_email,
            return_origin=return_origin,
        )
        sessions_view = [
            checkout.build_session_view(sid) for sid in group["session_ids"]
        ]
        body: dict[str, Any] = {
            "group_id": group["group_id"],
            "sessions": sessions_view,
            "_acp_spec_version": checkout.ACP_API_VERSION,
        }
        if not stripe_result.get("ok"):
            # Multi-shop Stripe creation failed. Raise a typed ACPError
            # with the Stripe message in `data` so the frontend stops
            # showing the generic "missing client_secret" toast and
            # surfaces the actual cause (key mismatch, currency
            # mismatch, line-items overflow, etc.). The carts already
            # exist; the agent / user can retry.
            raise ACPError(
                -32000,
                "stripe_checkout_failed",
                data={
                    "error": stripe_result.get("error"),
                    "group_id": group["group_id"],
                },
            )
        body["client_secret"] = stripe_result["client_secret"]
        body["payment_link"] = {
            "provider": "stripe",
            "client_secret": stripe_result["client_secret"],
            "stripe_session_id": stripe_result["stripe_session_id"],
            "mode": checkout.STRIPE_UI_MODE_EMBEDDED_PAGE,
        }
        return body

    # ── legacy single-shop path ─────────────────────────────────────
    shop_id = str(payload.get("shop_id") or "")
    items = _validate_shop_and_items(shop_id, payload.get("items"))

    cart_id = _mint_legacy_cart(shop_id, items, buyer)
    row = checkout.db_create_checkout_session(
        cart_id, initial_status=checkout._STATUS_READY
    )
    session_id = row["id"]

    # Kick a Stripe link immediately (matches legacy behaviour where
    # the ACP body carried ``payment_link.url``).
    stripe_session_view = {
        "id": session_id,
        "shop_id": shop_id,
        "items": items,
    }
    stripe_result = checkout.create_stripe_checkout_link(
        stripe_session_view,
        buyer_email=buyer_email,
        return_origin=return_origin,
    )
    if stripe_result.get("ok"):
        try:
            checkout.db_update_checkout_session(
                session_id,
                {
                    "status": checkout._STATUS_IN_PROGRESS,
                    "stripe_session_id": stripe_result["id"],
                    "stripe_transfer_group": stripe_result.get("transfer_group"),
                },
            )
        except checkout.InvalidStatusTransition as e:  # pragma: no cover
            raise ACPError(-32000, str(e)) from e

    body = checkout.build_session_view(session_id)
    if not stripe_result.get("ok"):
        # Same rationale as the multi-shop branch above — bubble the
        # Stripe error out as ACPError so the frontend's catch surfaces
        # the actual cause to Peter instead of the generic "missing
        # client_secret" toast.
        raise ACPError(
            -32000,
            "stripe_checkout_failed",
            data={"error": stripe_result.get("error"), "session_id": session_id},
        )
    body["client_secret"] = stripe_result["client_secret"]
    body["payment_link"] = {
        "provider": "stripe",
        "client_secret": stripe_result["client_secret"],
        "stripe_session_id": stripe_result["id"],
        "mode": checkout.STRIPE_UI_MODE_EMBEDDED_PAGE,
    }
    return body


# ── get ────────────────────────────────────────────────────────────────


def handle_get_checkout_session(
    *,
    meta: dict[str, Any] | None = None,
    id: str | None = None,  # noqa: A002 — ACP spec names the field `id`
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    _check_api_version(meta)
    if not id:
        raise ACPError(-32602, "id required")
    row = checkout.db_get_checkout_session(id)
    if row is None:
        raise ACPError(-32602, "session not found")
    _assert_session_access(row, user_id=user_id, cart_key=cart_key)
    return checkout.build_session_view(id)


# ── update ─────────────────────────────────────────────────────────────


_UPDATE_ALLOWED_KEYS = {"fulfillment_option_id", "buyer", "status"}


def handle_update_checkout_session(
    *,
    meta: dict[str, Any] | None = None,
    id: str | None = None,  # noqa: A002
    payload: dict[str, Any] | None = None,
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    """Mutate a session. Allowed patches: ``fulfillment_option_id``,
    ``buyer``, and explicit ``status`` transitions per the 6-state machine.
    """
    _check_api_version(meta)
    if not id:
        raise ACPError(-32602, "id required")
    row = checkout.db_get_checkout_session(id)
    if row is None:
        raise ACPError(-32602, "session not found")
    _assert_session_access(row, user_id=user_id, cart_key=cart_key)
    if row.get("status") in checkout._TERMINAL:
        raise ACPError(-32000, f"session is {row['status']}, cannot update")

    p = payload or {}
    patch: dict[str, Any] = {}
    for k, v in p.items():
        if k in _UPDATE_ALLOWED_KEYS:
            patch[k] = v
        elif k == "requires_escalation_url":
            patch[k] = v
    if not patch:
        return checkout.build_session_view(id)

    try:
        checkout.db_update_checkout_session(id, patch)
    except checkout.InvalidStatusTransition as e:
        raise ACPError(-32000, "invalid_status_transition", data={"detail": str(e)}) from e
    return checkout.build_session_view(id)


# ── complete (single-shop) ─────────────────────────────────────────────


def _complete_single_session(session_id: str) -> dict[str, Any]:
    """Single-shop completion: stock-race check + transition to completed.

    Mirrors A8's ``create_order_from_session`` contract: we only flip
    the session's status + decrement stock here; the order row is
    written by :mod:`orders.create_order_from_session` which the Stripe
    webhook calls. When the MCP tool is invoked directly (agent-native
    path, no Stripe redirect) we also call it inline so the ACP
    response carries an ``order`` block.
    """
    row = checkout.db_get_checkout_session(session_id)
    if row is None:
        raise ACPError(-32602, "session not found")
    status = row.get("status")
    if status == checkout._STATUS_COMPLETED:
        body = checkout.build_session_view(session_id)
        body["_idempotent"] = True
        return body
    if status == checkout._STATUS_CANCELED:
        raise ACPError(-32000, "session is canceled, cannot complete")

    cart = checkout._load_cart_row(row["cart_id"]) if row.get("cart_id") else {}
    shop_id = cart.get("shop_id") or ""
    items = checkout.load_session_items(row)

    stock_before: dict[str, int] = {}
    for it in items:
        avail = checkout.live_stock(shop_id, it["id"])
        stock_before[it["id"]] = avail
        if avail < int(it.get("quantity") or 1):
            raise ACPError(
                -32000,
                (
                    f"Stock changed — {it['id']} now has only {avail} on shelf "
                    f"at {shop_id}, cannot fulfill {it['quantity']}. "
                    "Create a new session with a smaller qty."
                ),
                data={"sku": it["id"], "available": avail},
            )

    stock_after: dict[str, int] = {}
    for it in items:
        snapshots.record_leo_sale(
            shop_id=shop_id,
            product_id=it["id"],
            quantity=int(it.get("quantity") or 1),
            order_ref=f"vitrina_session:{session_id[:12]}…",
        )
        stock_after[it["id"]] = checkout.live_stock(shop_id, it["id"])

    try:
        checkout.db_update_checkout_session(
            session_id, {"status": checkout._STATUS_COMPLETED}
        )
    except checkout.InvalidStatusTransition as e:
        raise ACPError(-32000, "invalid_status_transition", data={"detail": str(e)}) from e

    # Attempt to mint an order (idempotent). Orders module may fail in
    # environments that don't have the orders table seeded (e.g. unit
    # tests that focus on state transitions only); swallow quietly and
    # let the webhook retry.
    order: dict[str, Any] | None = None
    try:
        import orders as orders_mod  # local import to avoid test cycles

        order = orders_mod.create_order_from_session(session_id)
    except Exception:  # pragma: no cover — defensive
        order = None

    body = checkout.build_session_view(session_id)
    if order:
        body["order"] = order
    body["_stock_before"] = stock_before
    body["_stock_after"] = stock_after

    # Receipt email — stays a stub unless RESEND_API_KEY is set.
    buyer = cart.get("buyer") if isinstance(cart.get("buyer"), dict) else None
    buyer_email = (buyer or {}).get("email") if isinstance(buyer, dict) else None
    if buyer_email:
        total_minor = sum(
            checkout.price_minor(data.get_product(it["id"]) or {"price_eur": 0})
            * int(it.get("quantity") or 1)
            for it in items
        )
        collection_code = checkout._pickup_collection_code(session_id)
        email_result = checkout.send_order_email(
            to=buyer_email,
            order_id=(order or {}).get("id", f"ord_{secrets.token_hex(6)}"),
            items=items,
            total_minor=total_minor,
            shop_id=shop_id,
            collection_code=collection_code,
        )
        if email_result.get("ok"):
            body["email_delivery"] = {"status": "sent", "to": buyer_email,
                                      "provider": "resend", "id": email_result.get("id")}
        elif email_result.get("skipped"):
            body["email_delivery"] = {"status": "skipped", "to": buyer_email,
                                      "reason": email_result.get("error")}
        else:
            body["email_delivery"] = {"status": "failed", "to": buyer_email,
                                      "error": email_result.get("error")}
    else:
        body["email_delivery"] = {"status": "skipped", "reason": "no_buyer_email"}
    return body


def _complete_checkout_internal(session_id: str) -> dict[str, Any]:
    """Back-compat shim for the Stripe webhook (``api/routes/webhooks.py``).

    The webhook still calls this name; it now delegates to the new
    DB-backed flow. Returns an ACP body; raises :class:`ACPError` on
    stock race / state issues.
    """
    row = checkout.db_get_checkout_session(session_id)
    if row is None:
        raise ACPError(-32602, "session not found")
    if row.get("group_id"):
        checkout.complete_checkout_group(row["group_id"])
        return checkout.build_session_view(session_id)
    return _complete_single_session(session_id)


def handle_complete_checkout_session(
    *,
    meta: dict[str, Any] | None = None,
    id: str | None = None,  # noqa: A002
    payload: dict[str, Any] | None = None,  # noqa: ARG001
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    _check_api_version(meta)
    if not id:
        raise ACPError(-32602, "id required")
    row = checkout.db_get_checkout_session(id)
    if row is None:
        raise ACPError(-32602, "session not found")
    _assert_session_access(row, user_id=user_id, cart_key=cart_key)

    if row.get("group_id"):
        return handle_complete_checkout_group(
            meta=meta, group_id=row["group_id"]
        )
    return _complete_single_session(id)


# ── complete group ─────────────────────────────────────────────────────


def handle_complete_checkout_group(
    *,
    meta: dict[str, Any] | None = None,
    group_id: str | None = None,
) -> dict[str, Any]:
    """Thin wrapper over :func:`checkout.complete_checkout_group`.

    Returns ``{group_id, results: [...], rolled_back: bool, ...}`` with
    per-session outcomes. Raises ``ACPError(-32000, 'stock_race')`` when
    the atomic rollback fires, with the shortfalls in ``data``.
    """
    _check_api_version(meta)
    if not group_id:
        raise ACPError(-32602, "group_id required")
    results = checkout.complete_checkout_group(group_id)
    if not results:
        raise ACPError(-32602, "group not found or empty")
    header = results[0] if isinstance(results[0], dict) and results[0].get("rolled_back") else None
    if header is not None:
        raise ACPError(
            -32000,
            "stock_race_rolled_back",
            data={
                "group_id": group_id,
                "shortfalls": header.get("shortfalls"),
                "results": results[1:],
            },
        )
    return {
        "group_id": group_id,
        "results": results,
        "sessions": [checkout.build_session_view(r["session_id"]) for r in results],
        "_acp_spec_version": checkout.ACP_API_VERSION,
    }


# ── cancel ─────────────────────────────────────────────────────────────


def handle_cancel_checkout_session(
    *,
    meta: dict[str, Any] | None = None,
    id: str | None = None,  # noqa: A002
    user_id: str | None = None,
    cart_key: str | None = None,
) -> dict[str, Any]:
    _check_api_version(meta)
    if not id:
        raise ACPError(-32602, "id required")
    row = checkout.db_get_checkout_session(id)
    if row is None:
        raise ACPError(-32602, "session not found")
    _assert_session_access(row, user_id=user_id, cart_key=cart_key)
    if row.get("status") == checkout._STATUS_COMPLETED:
        raise ACPError(-32000, "session already completed, cannot cancel")
    if row.get("status") == checkout._STATUS_CANCELED:
        return checkout.build_session_view(id)
    try:
        checkout.db_update_checkout_session(
            id, {"status": checkout._STATUS_CANCELED}
        )
    except checkout.InvalidStatusTransition as e:
        raise ACPError(-32000, "invalid_status_transition", data={"detail": str(e)}) from e
    return checkout.build_session_view(id)


def handle_checkout_create_link(
    meta: dict[str, Any],
    payload: dict[str, Any] | None = None,
    *,
    cart_ids: list[str] | None = None,
    user_id: str | None = None,  # noqa: ARG001
    cart_key: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Mint a short vitrina.bg/checkout?s=… magic link for any chat channel.

    Stripe session creation is deferred to ``POST /v1/magic/checkout/session``
    which runs after the shopper has reviewed the basket and optionally
    authenticated on the Vitrina checkout page.  This keeps raw Stripe URLs
    out of chat and gives shoppers a proper basket-review + auth step.
    """
    _check_api_version(meta)
    payload = payload if isinstance(payload, dict) else {}
    raw_ids = payload.get("cart_ids")
    ids: list[str] | None = None
    if isinstance(raw_ids, list) and raw_ids:
        ids = [str(x) for x in raw_ids]
    elif cart_ids:
        ids = [str(x) for x in cart_ids]
    if not ids:
        raise ACPError(
            -32602,
            "cart_ids required — pass payload.cart_ids or session carts",
        )
    if not thread_id or not cart_key:
        raise ACPError(
            -32602,
            "thread_id and cart_key are required (injected from session)",
        )
    slot_payload = payload.get("slot")
    slot_arg: dict[str, Any] | None = (
        slot_payload if isinstance(slot_payload, dict) else None
    )
    try:
        slug = checkout_tokens.mint(
            cart_ids=ids,
            cart_key=cart_key,
            thread_id=thread_id,
            slot=slot_arg,
        )
    except Exception as exc:  # noqa: BLE001
        raise ACPError(
            -32000,
            f"checkout_link_mint_failed: {exc}",
        ) from exc
    url = f"{_public_base_url()}/checkout?s={slug}"
    # #region debug instrumentation — magic-link URL audit (session 1d70ef)
    logger.info(
        "MAGIC_LINK_AUDIT-1d70ef checkout url=%s thread=%s "
        "VITRINA_PUBLIC_HOST=%r VITRINA_PUBLIC_URL=%r VITRINA_PUBLIC_BASE_URL=%r",
        url, thread_id,
        os.environ.get("VITRINA_PUBLIC_HOST"),
        os.environ.get("VITRINA_PUBLIC_URL"),
        os.environ.get("VITRINA_PUBLIC_BASE_URL"),
    )
    # #endregion
    return {
        "ok": True,
        "url": url,
        "expires_in": int(checkout_tokens.CHECKOUT_TOKEN_TTL.total_seconds()),
        "_acp_spec_version": checkout.ACP_API_VERSION,
    }


# ── dev helper: reset_demo_stock ───────────────────────────────────────


def handle_reset_demo_stock() -> dict[str, Any]:
    """Zero all ``leo_sold_delta`` entries across all demo shops."""
    ledger = snapshots._ledger()  # noqa: SLF001
    wiped = {shop: dict(d) for shop, d in ledger.leo_delta.items()}
    ledger.leo_delta.clear()
    ledger._audit(  # noqa: SLF001
        "demo_stock.reset", {"wiped_shops": list(wiped.keys())}
    )
    return {
        "ok": True,
        "message": "Demo stock reset — all leo_sold_delta entries cleared.",
        "wiped": wiped,
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
