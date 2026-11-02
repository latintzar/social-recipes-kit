"""ACP-shape checkout helpers for Vitrina (Phase 1 — DB-backed).

Ports the ACP (Agentic Commerce Protocol, spec 2026-04-17) checkout
surface from the Node prototype, plus UCP Phase 1 extensions:

* Sessions are **real Supabase rows** (``checkout_sessions`` table, see
  ``api/migrations/001_commerce_tables.sql``). No more HMAC-signed
  stateless tokens — the id IS a UUID, the row carries the state.
* **Multi-shop** group checkout — one Stripe Checkout Session can carry
  line items from N independent ``checkout_sessions`` rows sharing a
  ``group_id``. On the Stripe webhook we fan out and materialise N
  orders (see ``mcp/orders.py::create_order_from_session`` + the
  fan-out in ``api/routes/webhooks.py``).
* **UCP 6-state lifecycle**::

      not_ready_for_payment → ready_for_payment → in_progress
                                                    ├→ requires_escalation
                                                    │     ├→ in_progress
                                                    │     └→ ready_for_payment
                                                    ├→ completed (terminal)
                                                    └→ ready_for_payment (retry)
      (any non-terminal)    → canceled      (terminal)

  ``db_update_checkout_session`` enforces the transition graph; bad
  jumps raise ``ACPError(-32000, 'invalid_status_transition')`` in
  :mod:`checkout_handlers`.

## Click-and-collect only

v1 ships only ``fulfillment_options: [{type: "pickup"}]``. No delivery.
Collection codes are generated at `complete` time and surfaced in the
order response; they're deterministic per session (first 6 hex of the
session id's sha256) so a re-fetch returns the same code.

## Stripe wiring

``create_stripe_checkout_link`` (single session) and
``create_single_stripe_session_for_group`` (multi-shop) call
``stripe.checkout.Session.create`` with ``ui_mode=embedded_page`` so the
browser mounts Embedded Checkout via ``client_secret``. Hosted tap-to-pay
links use ``create_stripe_hosted_session_for_group`` with ``ui_mode=hosted_page``.
Vitrina adds Connect transfer metadata so one shopper charge can fan out to
store payouts after the webhook confirms payment.

The Stripe webhook (``api/routes/webhooks.py``) reads
``metadata.vitrina_session_id`` (UUID) and optional ``metadata.group_id``
to fan out order creation. For multi-shop, every sub-session carries
the same Stripe ``stripe_session_id`` (they share one Stripe Checkout
Session) and every row gets an order minted on completion.

## ACP wire format

Response shape matches ACP spec ``2026-04-17`` (CheckoutSession). See
``apps/vitrina/company/notes/local-stock-landscape/acp/rfc.agentic_checkout.md``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import data
import snapshots
import supabase_client

logger = logging.getLogger(__name__)

ACP_API_VERSION = "2026-04-17"

# Stripe API 2026-03-25.dahlia+ — ``ui_mode`` ``embedded`` / ``hosted`` were
# removed; using them returns HTTP 409. Use embedded_page / hosted_page.
STRIPE_UI_MODE_EMBEDDED_PAGE = "embedded_page"
STRIPE_UI_MODE_HOSTED_PAGE = "hosted_page"

_STRIPE_KEY_ENV = "STRIPE_SECRET_KEY_TEST"
_STRIPE_KEY_FALLBACK_ENV = "STRIPE_SECRET_KEY"
_SUCCESS_URL_ENV = "VITRINA_STRIPE_SUCCESS_URL"
_CANCEL_URL_ENV = "VITRINA_STRIPE_CANCEL_URL"
_PLATFORM_FEE_BPS_ENV = "VITRINA_PLATFORM_FEE_BPS"
_RESEND_KEY_ENV = "RESEND_API_KEY"

# Default return_url for Embedded Checkout. Stripe substitutes
# {CHECKOUT_SESSION_ID} server-side in the URL.
_RETURN_URL_DEFAULT = "https://vitrina.app/checkout/success?session_id={CHECKOUT_SESSION_ID}"

# Hosts the frontend may pass as `return_origin` so the return_url
# points back to the same origin the user started from (production,
# custom domain vitrina.bg, or a Vercel preview deploy).
_ALLOWED_RETURN_HOST_SUFFIXES: tuple[str, ...] = (
    "vitrina.app",
    "vitrina.bg",
    ".vercel.app",
)


def _resolve_return_url(
    *,
    return_origin: Optional[str] = None,
) -> str:
    """Return the ``return_url`` for an Embedded Stripe Checkout Session.

    Embedded Checkout uses a single ``return_url`` (no separate
    ``cancel_url``) — Stripe substitutes ``{CHECKOUT_SESSION_ID}`` in
    the URL before redirecting.

    Order of precedence:
        1. ``return_origin`` (allowlisted https:// host) — mints a same-
           origin URL so the success page loads on the deploy the user
           started from (makes preview deploys work without per-env config).
        2. ``VITRINA_STRIPE_SUCCESS_URL`` env override (kept for operator
           flexibility; the value should contain ``{CHECKOUT_SESSION_ID}``).
        3. Built-in default (vitrina.app).
    """
    tpl = os.environ.get(_SUCCESS_URL_ENV) or _RETURN_URL_DEFAULT
    if return_origin:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(return_origin)
            host = (parsed.hostname or "").lower()
            if (
                parsed.scheme in {"http", "https"}
                and host
                and any(
                    host == suffix.lstrip(".") or host.endswith(suffix)
                    for suffix in _ALLOWED_RETURN_HOST_SUFFIXES
                )
            ):
                base = f"{parsed.scheme}://{parsed.netloc}"
                tpl = f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}"
        except Exception:  # pragma: no cover — defensive
            pass
    return tpl


# ── UCP 6-state lifecycle ──────────────────────────────────────────────

_STATUS_NOT_READY = "not_ready_for_payment"
_STATUS_READY = "ready_for_payment"
_STATUS_IN_PROGRESS = "in_progress"
_STATUS_REQUIRES_ESCALATION = "requires_escalation"
_STATUS_COMPLETED = "completed"
_STATUS_CANCELED = "canceled"
_TERMINAL = {_STATUS_COMPLETED, _STATUS_CANCELED}
_VALID_STATUSES = {
    _STATUS_NOT_READY,
    _STATUS_READY,
    _STATUS_IN_PROGRESS,
    _STATUS_REQUIRES_ESCALATION,
    _STATUS_COMPLETED,
    _STATUS_CANCELED,
}

# Allowed status transitions. Terminal states deliberately have no
# outgoing edges. The "re-try after failure" edges
# (in_progress → ready_for_payment, requires_escalation → ready_for_payment)
# match the Stripe webhook handler in ``api/routes/webhooks.py``.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    _STATUS_NOT_READY: {_STATUS_READY, _STATUS_CANCELED},
    _STATUS_READY: {_STATUS_IN_PROGRESS, _STATUS_CANCELED},
    _STATUS_IN_PROGRESS: {
        _STATUS_REQUIRES_ESCALATION,
        _STATUS_COMPLETED,
        _STATUS_READY,
        _STATUS_CANCELED,
    },
    _STATUS_REQUIRES_ESCALATION: {
        _STATUS_IN_PROGRESS,
        _STATUS_READY,
        _STATUS_CANCELED,
    },
    _STATUS_COMPLETED: set(),
    _STATUS_CANCELED: set(),
}


class InvalidStatusTransition(ValueError):
    """Raised by ``db_update_checkout_session`` on a bad state transition.

    Handlers translate this into an ``ACPError(-32000,
    'invalid_status_transition')``.
    """


# ── DB indirection (tests monkeypatch this) ────────────────────────────


def _get_supabase() -> Any:
    return supabase_client.get_supabase()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── stock helpers ──────────────────────────────────────────────────────


def live_stock(shop_id: str, product_id: str) -> int:
    """Effective shelf stock = seed base − leo_sold_delta, floored at 0.

    Reads the static demo catalogue via :mod:`data` and overlays
    :mod:`snapshots`' Leo-sold delta. Products not present in the demo
    catalogue return 0 — the DB-backed multi-shop flow treats those as
    "cannot complete without a fresh live-stock feed" which is the
    safe default.
    """
    product = data.get_product(product_id)
    if not product or str(product.get("shop_id")) != str(shop_id):
        return 0
    base = int(product.get("stock") or 0)
    sold = snapshots.get_leo_delta(str(shop_id), str(product_id))
    return max(0, base - sold)


def price_minor(product: dict[str, Any]) -> int:
    """EUR major units (``price_eur``) → minor units (cents)."""
    return int(round(float(product.get("price_eur") or 0) * 100))


def _pickup_collection_code(session_id: str) -> str:
    """6-char hex code for in-store pickup. Deterministic per session."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:6].upper()


# ── ACP response shaping ───────────────────────────────────────────────


def _totals_for(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ACP Total[] array — subtotal + free pickup + zero tax (EU VAT incl.)."""
    subtotal = 0
    for it in items:
        p = data.get_product(it["id"])
        if p is None:
            continue
        subtotal += price_minor(p) * int(it.get("quantity") or 1)
    return [
        {"type": "items_base_amount", "display_text": "Subtotal", "amount": subtotal},
        {"type": "subtotal", "display_text": "Subtotal", "amount": subtotal},
        {"type": "fulfillment", "display_text": "Pickup", "amount": 0},
        {"type": "tax", "display_text": "VAT (incl.)", "amount": 0},
        {"type": "total", "display_text": "Total", "amount": subtotal},
    ]


def _shop_location_for(shop_id: str) -> dict[str, Any]:
    shop = data.get_shop(shop_id) or {}
    addr = shop.get("address") or {}
    return {
        "name": shop.get("name_en") or shop.get("name_bg") or shop_id,
        "address": {
            "line_one": addr.get("street_en") or addr.get("street_bg") or "",
            "city": addr.get("city_en") or addr.get("city_bg") or "",
            "state": "",
            "country": addr.get("country") or "BG",
            "postal_code": addr.get("postal_code") or "",
        },
        "phone": shop.get("phone"),
        "lat": addr.get("lat"),
        "lng": addr.get("lng"),
    }


def build_checkout_session(session: dict[str, Any]) -> dict[str, Any]:
    """Render a session dict into an ACP CheckoutSession response body.

    ``session`` is the shape produced by :func:`build_session_view` —
    ``id``, ``shop_id``, ``status``, ``items: [{id, quantity}]``, plus
    the usual optional ACP bits (buyer, fulfillment_address, etc.).
    """
    session_id: str = session["id"]
    shop_id = str(session.get("shop_id") or "")
    status: str = session.get("status") or _STATUS_IN_PROGRESS
    is_terminal = status in _TERMINAL

    items_out: list[dict[str, Any]] = []
    stock_by_item: dict[str, int] = {}
    for it in session.get("items") or []:
        p = data.get_product(it["id"])
        qty = int(it.get("quantity") or 1)
        unit = price_minor(p) if p else 0
        items_out.append(
            {
                "id": it["id"],
                "quantity": qty,
                "base_amount": unit,
                "discount": 0,
                "subtotal": unit * qty,
                "tax": 0,
                "total": unit * qty,
                "line_item_id": f"li_{it['id']}",
            }
        )
        stock_by_item[it["id"]] = live_stock(shop_id, it["id"])

    loc = _shop_location_for(shop_id)
    all_available = is_terminal or all(
        stock_by_item.get(it["id"], 0) >= int(it.get("quantity") or 1)
        for it in session.get("items") or []
    )

    now = datetime.now(timezone.utc)
    ready_by = (now + timedelta(minutes=15)).isoformat(timespec="seconds").replace("+00:00", "Z")
    pickup_by = (now + timedelta(hours=24)).isoformat(timespec="seconds").replace("+00:00", "Z")

    fulfillment_options: list[dict[str, Any]] = []
    if all_available:
        fulfillment_options.append(
            {
                "type": "pickup",
                "id": "pickup_in_store",
                "title": f"Pickup at {loc['name']}",
                "description": (
                    "Order confirmed. Ready in ~15 min."
                    if is_terminal and status == _STATUS_COMPLETED
                    else "Live stock confirmed. Ready in ~15 min."
                ),
                "location": loc,
                "pickup_type": "in_store",
                "collection_code": _pickup_collection_code(session_id),
                "ready_by": ready_by,
                "pickup_by": pickup_by,
                "totals": [{"type": "fulfillment", "display_text": "Pickup", "amount": 0}],
            }
        )

    messages: list[dict[str, Any]] = []
    stock_summary = ", ".join(f"{k}×{v}" for k, v in stock_by_item.items()) or "(empty cart)"
    if is_terminal and status == _STATUS_COMPLETED:
        messages.append(
            {
                "type": "info",
                "content_type": "plain",
                "content": (
                    f"Purchase confirmed. Shelf stock after this order: {stock_summary}. "
                    f"Reserved for pickup at {loc['name']} — collection code "
                    f"{_pickup_collection_code(session_id)}."
                ),
            }
        )
    elif is_terminal and status == _STATUS_CANCELED:
        messages.append(
            {
                "type": "info",
                "content_type": "plain",
                "content": "Session canceled. No stock was decremented.",
            }
        )
    elif status == _STATUS_REQUIRES_ESCALATION:
        messages.append(
            {
                "type": "info",
                "content_type": "plain",
                "content": (
                    "Payment requires additional authentication (3-D Secure). "
                    + (
                        f"Complete it here: {session.get('requires_escalation_url')}."
                        if session.get("requires_escalation_url")
                        else "Stripe will supply a challenge URL shortly."
                    )
                ),
            }
        )
    elif all_available:
        messages.append(
            {
                "type": "info",
                "content_type": "plain",
                "content": (
                    f"Live pickup stock on shelf: {stock_summary}. "
                    "Cart is within available stock — safe to complete."
                ),
            }
        )
    else:
        shortfalls = [
            f"{it['id']}: requested {it.get('quantity', 1)}, "
            f"only {stock_by_item.get(it['id'], 0)} on shelf"
            for it in session.get("items") or []
            if stock_by_item.get(it["id"], 0) < int(it.get("quantity") or 1)
        ]
        messages.append(
            {
                "type": "error",
                "content_type": "plain",
                "content": (
                    f"Insufficient pickup stock. {'; '.join(shortfalls)}. "
                    "Reduce the quantity or choose another item."
                ),
            }
        )

    currency = (data.get_shop(shop_id) or {}).get("currency") or "EUR"

    body: dict[str, Any] = {
        "id": session_id,
        "buyer": session.get("buyer"),
        "payment_provider": {
            "provider": "stripe",
            "supported_payment_methods": ["card"],
        },
        "status": status,
        "currency": currency,
        "line_items": items_out,
        "fulfillment_address": session.get("fulfillment_address"),
        "fulfillment_options": fulfillment_options,
        "fulfillment_option_id": session.get("fulfillment_option_id") or "pickup_in_store",
        "totals": _totals_for(session.get("items") or []),
        "messages": messages,
        "links": [
            {
                "type": "terms_of_use",
                "url": "https://vitrina.app/terms",
            }
        ],
        "_live_stock": stock_by_item,
        "_acp_spec_version": ACP_API_VERSION,
    }
    if session.get("group_id"):
        body["group_id"] = session["group_id"]
    if session.get("requires_escalation_url"):
        body["requires_escalation_url"] = session["requires_escalation_url"]
    if session.get("stripe_checkout_url"):
        body["payment_link"] = {
            "provider": "stripe",
            "url": session["stripe_checkout_url"],
            "stripe_session_id": session.get("stripe_session_id"),
            "mode": "checkout",
        }
    return body


# ── Supabase persistence for checkout_sessions ────────────────────────


def db_create_checkout_session(
    cart_id: str,
    *,
    group_id: Optional[str] = None,
    initial_status: str = _STATUS_NOT_READY,
    fulfillment_option_id: str = "pickup_in_store",
) -> dict[str, Any]:
    """INSERT a ``checkout_sessions`` row and return it.

    ``initial_status`` defaults to ``not_ready_for_payment`` per the UCP
    lifecycle — callers flip to ``ready_for_payment`` once cart totals /
    shop hours / buyer info have all cleared pre-flight, then to
    ``in_progress`` when a Stripe session is minted.
    """
    if initial_status not in _VALID_STATUSES:
        raise ValueError(f"unknown_status:{initial_status}")
    sb = _get_supabase()
    row_in: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "cart_id": cart_id,
        "status": initial_status,
        "fulfillment_option_id": fulfillment_option_id,
    }
    if group_id:
        row_in["group_id"] = group_id
    inserted = sb.table("checkout_sessions").insert(row_in).execute()
    row = inserted[0] if isinstance(inserted, list) else inserted
    return dict(row or row_in)


def db_get_checkout_session(session_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single ``checkout_sessions`` row by id. ``None`` if missing."""
    sb = _get_supabase()
    row = (
        sb.table("checkout_sessions")
        .select("*")
        .eq("id", session_id)
        .single()
        .execute()
    )
    return row or None


def db_list_sessions_in_group(group_id: str) -> list[dict[str, Any]]:
    sb = _get_supabase()
    rows = (
        sb.table("checkout_sessions")
        .select("*")
        .eq("group_id", group_id)
        .execute()
    )
    return list(rows or [])


def db_update_checkout_session(
    session_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """UPDATE a ``checkout_sessions`` row; enforces the state machine.

    If ``patch`` carries a ``status``, the current row's status must have
    the target in its outgoing-edge set, otherwise
    :class:`InvalidStatusTransition` is raised and no write happens.
    """
    current = db_get_checkout_session(session_id)
    if not current:
        raise ValueError(f"session_not_found:{session_id}")
    new_status = patch.get("status")
    if new_status is not None:
        if new_status not in _VALID_STATUSES:
            raise InvalidStatusTransition(
                f"unknown status {new_status!r}"
            )
        cur = current.get("status") or _STATUS_NOT_READY
        if new_status != cur and new_status not in _ALLOWED_TRANSITIONS.get(cur, set()):
            raise InvalidStatusTransition(
                f"invalid_status_transition: {cur} -> {new_status}"
            )
        # Stamp terminal timestamps.
        if new_status == _STATUS_COMPLETED and "completed_at" not in patch:
            patch = {**patch, "completed_at": _now_iso()}
        elif new_status == _STATUS_CANCELED and "canceled_at" not in patch:
            patch = {**patch, "canceled_at": _now_iso()}
    sb = _get_supabase()
    updated = (
        sb.table("checkout_sessions")
        .update(patch)
        .eq("id", session_id)
        .execute()
    )
    row = updated[0] if isinstance(updated, list) and updated else updated
    return dict(row or {**current, **patch})


# ── view assembly (DB row → ACP-shape session dict) ───────────────────


def load_session_items(session_row: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve cart_line_items for the session's cart_id.

    Returns ``[{id, quantity}]`` in the legacy shape the ACP renderer
    expects. ``id`` is the line's ``product_id`` (so the static
    :mod:`data` catalogue lookups in :func:`build_checkout_session`
    keep working); quantities come straight off the line row.
    """
    cart_id = session_row.get("cart_id")
    if not cart_id:
        return []
    sb = _get_supabase()
    lines = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .order("created_at")
        .execute()
    ) or []
    out: list[dict[str, Any]] = []
    for line in lines:
        pid = line.get("product_id")
        if not pid:
            continue
        out.append({"id": pid, "quantity": int(line.get("quantity") or 1)})
    return out


def _load_cart_row(cart_id: str) -> dict[str, Any]:
    sb = _get_supabase()
    row = (
        sb.table("carts").select("*").eq("id", cart_id).single().execute()
    )
    return dict(row or {})


def build_session_view(session_id: str) -> dict[str, Any]:
    """Full round-trip: DB row → ACP CheckoutSession body.

    Stitches ``checkout_sessions`` + ``carts`` + ``cart_line_items``
    into the session dict :func:`build_checkout_session` renders from,
    then stamps the DB-native fields (``status``,
    ``stripe_checkout_url``, ``requires_escalation_url``, ``group_id``)
    onto the output.
    """
    row = db_get_checkout_session(session_id)
    if not row:
        raise LookupError(f"session_not_found:{session_id}")
    cart = _load_cart_row(row["cart_id"]) if row.get("cart_id") else {}
    items = load_session_items(row)
    session_view = {
        "id": row["id"],
        "shop_id": cart.get("shop_id") or "",
        "status": row.get("status") or _STATUS_NOT_READY,
        "items": items,
        "buyer": cart.get("buyer") or row.get("buyer"),
        "fulfillment_address": row.get("fulfillment_address"),
        "fulfillment_option_id": row.get("fulfillment_option_id")
        or "pickup_in_store",
        "group_id": row.get("group_id"),
        "stripe_checkout_url": row.get("stripe_checkout_url"),
        "stripe_session_id": row.get("stripe_session_id"),
        "requires_escalation_url": row.get("requires_escalation_url"),
    }
    return build_checkout_session(session_view)


# ── Stripe Checkout Session wrapper ────────────────────────────────────


def _get_stripe_client() -> Any:
    """Build a per-call Stripe client. Test seam — patched via monkeypatch."""
    key = os.environ.get(_STRIPE_KEY_ENV) or os.environ.get(_STRIPE_KEY_FALLBACK_ENV)
    if not key:
        raise RuntimeError("stripe_key_missing")
    import stripe  # type: ignore[import-not-found]

    return stripe.StripeClient(api_key=key)


def _convert_to_stripe_currency(shop_currency: str, amount_minor: int) -> tuple[str, int]:
    """Pass shop currency straight through to Stripe.

    Bulgaria adopted the euro 2026-01-01 (this codebase's local timeline)
    and Vitrina re-priced every shop in EUR at the same time. There's no
    longer a BGN-display / EUR-charge split — Stripe gets the same
    currency the user sees on the card.

    Stripe requires unit_amount >= 50 cents (EUR minimum). Zero-price
    items (e.g. pantry spices that are technically free) floor to 50 so
    the session create never returns an InvalidRequestError 409.
    """
    currency = (shop_currency or "EUR").lower()
    # Stripe's minimum chargeable amount is 50 cents for EUR/GBP/etc.
    safe_amount = max(50, int(amount_minor)) if int(amount_minor) > 0 else 50
    return currency, safe_amount


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _stripe_transfer_group(*, session_id: str | None = None, group_id: str | None = None) -> str:
    if group_id:
        return f"vitrina_group_{group_id}"
    return f"vitrina_session_{session_id or uuid.uuid4()}"


def _load_shop_row(shop_id: str) -> dict[str, Any]:
    if not shop_id:
        return {}
    try:
        sb = _get_supabase()
        row = sb.table("shops").select("*").eq("id", shop_id).single().execute()
        return dict(row or {})
    except Exception as exc:  # pragma: no cover - ops/schema fallback
        logger.warning("shop load failed shop=%s err=%s", shop_id, exc)
        return {}


def _shop_display_name(shop_id: str, shop_row: dict[str, Any] | None = None) -> str:
    row = shop_row or {}
    return (
        row.get("name")
        or row.get("name_en")
        or row.get("name_bg")
        or _shop_location_for(shop_id)["name"]
        or shop_id
    )


def _platform_fee_bps(shop_row: dict[str, Any] | None = None) -> int:
    row_value = (shop_row or {}).get("platform_fee_bps")
    raw = row_value if row_value is not None else os.environ.get(_PLATFORM_FEE_BPS_ENV)
    bps = _safe_int(raw, 0)
    return min(10_000, max(0, bps))


def _cart_lines(cart_id: str) -> list[dict[str, Any]]:
    sb = _get_supabase()
    return (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .order("created_at")
        .execute()
    ) or []


def _line_unit_minor(line: dict[str, Any]) -> int:
    qty = max(1, _safe_int(line.get("quantity"), 1))
    snap = line.get("product_snapshot") or {}
    if line.get("total_minor") is not None:
        return max(0, int(round(_safe_int(line.get("total_minor")) / qty)))
    if line.get("subtotal_minor") is not None:
        return max(0, int(round(_safe_int(line.get("subtotal_minor")) / qty)))
    return _safe_int(line.get("unit_price_minor") or snap.get("price_minor"))


def _cart_stripe_summary(cart_id: str) -> dict[str, Any]:
    cart = _load_cart_row(cart_id)
    shop_id = str(cart.get("shop_id") or "")
    shop_row = _load_shop_row(shop_id)
    display_currency = (
        cart.get("currency")
        or shop_row.get("currency")
        or (data.get_shop(shop_id) or {}).get("currency")
        or "EUR"
    )
    lines = _cart_lines(cart_id)
    stripe_currency = "eur" if str(display_currency).upper() == "EUR" else str(display_currency).lower()
    gross_amount_minor = 0
    line_items: list[dict[str, Any]] = []
    for line in lines:
        qty = max(1, _safe_int(line.get("quantity"), 1))
        product_id = str(line.get("product_id") or "")
        snap = line.get("product_snapshot") or {}
        unit_minor_display = _line_unit_minor(line)
        currency, unit_amount = _convert_to_stripe_currency(display_currency, unit_minor_display)
        stripe_currency = currency
        gross_amount_minor += unit_amount * qty
        line_items.append(
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": snap.get("name")
                        or snap.get("name_en")
                        or snap.get("name_bg")
                        or product_id,
                        "description": (
                            f"SKU {product_id} — pickup at {_shop_display_name(shop_id, shop_row)}"
                        ),
                    },
                },
                "quantity": qty,
            }
        )
    return {
        "cart": cart,
        "shop": shop_row,
        "shop_id": shop_id,
        "currency": stripe_currency,
        "gross_amount_minor": gross_amount_minor,
        "line_items": line_items,
    }


def _items_to_stripe_line_items(
    shop_id: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert ACP ``{id, quantity}`` items into Stripe ``price_data`` rows."""
    shop_currency = (data.get_shop(shop_id) or {}).get("currency") or "EUR"
    out: list[dict[str, Any]] = []
    for it in items:
        p = data.get_product(it["id"])
        if p is None:
            continue
        currency, unit_amount = _convert_to_stripe_currency(shop_currency, price_minor(p))
        out.append(
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": p.get("name_en") or p.get("name_bg") or it["id"],
                        "description": (
                            f"SKU {p['id']} — pickup at "
                            f"{_shop_location_for(shop_id)['name']}"
                        ),
                    },
                },
                "quantity": int(it.get("quantity") or 1),
            }
        )
    return out


def create_stripe_checkout_link(
    session: dict[str, Any],
    *,
    buyer_email: Optional[str] = None,
    return_origin: Optional[str] = None,
) -> dict[str, Any]:
    """Single-shop path: create an Embedded Stripe Checkout Session.

    ``session`` is a dict with at least ``id`` (uuid), ``shop_id``, and
    ``items``. Returns ``{ok, client_secret, id}``; on failure
    ``{ok: False, error}``.  The webhook keys off
    ``metadata.vitrina_session_id`` = the uuid.

    Uses ``ui_mode='embedded_page'`` — the frontend mounts Embedded
    Checkout via ``@stripe/react-stripe-js`` using the returned ``client_secret``.
    No ``cancel_url``; the user cancels by closing the basket sheet.
    """
    shop_id = str(session.get("shop_id") or "")
    line_items = _items_to_stripe_line_items(shop_id, session.get("items") or [])
    if not line_items:
        return {"ok": False, "error": "empty_cart"}

    session_id = session["id"]
    return_url = _resolve_return_url(return_origin=return_origin)
    transfer_group = _stripe_transfer_group(session_id=session_id)

    params: dict[str, Any] = {
        "ui_mode": STRIPE_UI_MODE_EMBEDDED_PAGE,
        "mode": "payment",
        "return_url": return_url,
        "line_items": line_items,
        "client_reference_id": f"acp_{session_id}",
        "metadata": {
            "vitrina_session_id": session_id,
            "vitrina_shop_id": shop_id,
            "transfer_group": transfer_group,
        },
        "payment_intent_data": {
            "transfer_group": transfer_group,
            "metadata": {
                "vitrina_session_id": session_id,
                "vitrina_shop_id": shop_id,
                "transfer_group": transfer_group,
            },
        },
    }
    if buyer_email:
        params["customer_email"] = buyer_email

    try:
        client = _get_stripe_client()
        stripe_session = client.checkout.sessions.create(params=params)
        sid = getattr(stripe_session, "id", None) or stripe_session["id"]  # type: ignore[index]
        client_secret = (
            getattr(stripe_session, "client_secret", None)
            or stripe_session.get("client_secret")  # type: ignore[union-attr]
        )
        return {"ok": True, "client_secret": client_secret, "id": sid, "transfer_group": transfer_group}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # pragma: no cover — defensive
        # Surface the real Stripe error so the agent can react / report it.
        # `user_message` exists on stripe.error.* exceptions; fall back to str(e).
        reason = getattr(e, "user_message", None) or str(e) or type(e).__name__
        logger.warning("Stripe embedded checkout creation failed: %s", reason)
        return {"ok": False, "error": f"{type(e).__name__}: {reason}"[:500]}


# ── Multi-shop group orchestration ────────────────────────────────────


class ShopClosedError(Exception):
    """Raised by :func:`create_multishop_group` when any cart's shop is
    not available for the chosen pickup slot.

    Carries the per-cart detail so handlers can surface it in the ACP
    error envelope. Translated in :mod:`checkout_handlers` into an
    ``ACPError('-32000', 'shop_closed', data={'closed_carts': [...]})``.
    """

    def __init__(self, closed_carts: list[dict[str, Any]]) -> None:
        super().__init__("shop_closed")
        self.closed_carts = closed_carts


def create_multishop_group(
    cart_ids: list[str],
    *,
    slot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create N ``checkout_sessions`` rows sharing a ``group_id``.

    Pre-flight depends on the chosen pickup slot:

    * **Priority "Now" slot** (``slot.kind == 'priority'``) — shops must
      be open *right now*; if any closed we raise :class:`ShopClosedError`.
    * **Standard / scheduled slot** (``slot.kind == 'standard'`` with
      ``start`` / ``end`` ISO timestamps) — shops only need to be open
      during the booked pickup window. A shop closed at the moment of
      checkout but open during the window the next morning still passes.
    * **No slot supplied** — fall back to the "open now" check (legacy
      callers / tests). The frontend always sends a slot.

    On success returns ``{group_id, session_ids: [...]}``; no rows are
    written if validation fails.
    """
    if not cart_ids:
        raise ValueError("cart_ids_required")
    # Guard: strip any optimistic/non-UUID cart IDs that the frontend may
    # emit before the server cart is persisted.  A real cart UUID never
    # starts with "__opt-cart__:".
    cart_ids = [c for c in cart_ids if not str(c).startswith("__opt-cart__:")]
    if not cart_ids:
        raise ValueError("no_real_cart_ids — all provided IDs were optimistic placeholders")
    import shop_hours  # local import — avoid test-time cycles

    slot_kind = (slot or {}).get("kind") if isinstance(slot, dict) else None
    window_start = (slot or {}).get("start") if isinstance(slot, dict) else None
    window_end = (slot or {}).get("end") if isinstance(slot, dict) else None

    use_window = (
        slot_kind == "standard"
        and isinstance(window_start, str)
        and isinstance(window_end, str)
    )
    if use_window:
        statuses = shop_hours.validate_cart_shops_open_during_window(
            list(cart_ids), window_start, window_end
        )
    else:
        statuses = shop_hours.validate_cart_shops_open(list(cart_ids))
    closed = [s for s in statuses if not s.get("open")]
    if closed:
        raise ShopClosedError(closed)

    group_id = str(uuid.uuid4())
    session_ids: list[str] = []
    for cart_id in cart_ids:
        row = db_create_checkout_session(
            cart_id,
            group_id=group_id,
            initial_status=_STATUS_READY,
        )
        session_ids.append(row["id"])
    return {"group_id": group_id, "session_ids": session_ids}


def _group_line_items_for_stripe(group_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flatten every session in a group into one Stripe ``line_items`` list.

    Returns ``(stripe_line_items, sessions)``. Each session's rows are
    still keyed to that session via the per-session ``metadata`` carried
    separately — for Stripe's session-level metadata we only stash the
    group id. Webhook fan-out in ``api/routes/webhooks.py`` iterates
    every sibling session.
    """
    sessions = db_list_sessions_in_group(group_id)
    stripe_items: list[dict[str, Any]] = []
    for s in sessions:
        cart_id = s.get("cart_id")
        if cart_id:
            summary = _cart_stripe_summary(str(cart_id))
            stripe_items.extend(summary["line_items"])
            continue
        cart = _load_cart_row(s["cart_id"]) if s.get("cart_id") else {}
        stripe_items.extend(_items_to_stripe_line_items(cart.get("shop_id") or "", load_session_items(s)))
    return stripe_items, sessions


def create_single_stripe_session_for_group(
    group_id: str,
    *,
    buyer_email: Optional[str] = None,
    return_origin: Optional[str] = None,
    extra_metadata: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Create ONE Embedded Stripe Checkout Session covering every cart in a group.

    Stamps ``stripe_session_id`` on every sub-session and flips them to
    ``in_progress``. Returns ``{client_secret, stripe_session_id}`` or
    ``{ok: False, error}`` on failure.

    Uses ``ui_mode='embedded_page'`` — the frontend mounts Embedded
    Checkout via ``@stripe/react-stripe-js`` using the returned ``client_secret``.
    """
    stripe_items, sessions = _group_line_items_for_stripe(group_id)
    if not stripe_items:
        return {"ok": False, "error": "empty_group"}

    primary_session_id = sessions[0]["id"] if sessions else group_id
    return_url = _resolve_return_url(return_origin=return_origin)
    transfer_group = _stripe_transfer_group(group_id=group_id)

    params: dict[str, Any] = {
        "ui_mode": STRIPE_UI_MODE_EMBEDDED_PAGE,
        "mode": "payment",
        "return_url": return_url,
        "line_items": stripe_items,
        "client_reference_id": f"acp_group_{group_id}",
        "metadata": {
            # The webhook fan-out (webhooks.py::_iter_session_ids) keys
            # off group_id and the primary vitrina_session_id together.
            "vitrina_session_id": primary_session_id,
            "group_id": group_id,
            "transfer_group": transfer_group,
            **(extra_metadata or {}),
        },
        "payment_intent_data": {
            "transfer_group": transfer_group,
            "metadata": {
                "vitrina_session_id": primary_session_id,
                "group_id": group_id,
                "transfer_group": transfer_group,
            },
        },
    }
    if buyer_email:
        params["customer_email"] = buyer_email

    try:
        client = _get_stripe_client()
        stripe_session = client.checkout.sessions.create(params=params)
        sid = getattr(stripe_session, "id", None) or stripe_session["id"]  # type: ignore[index]
        client_secret = (
            getattr(stripe_session, "client_secret", None)
            or stripe_session.get("client_secret")  # type: ignore[union-attr]
        )
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # pragma: no cover — defensive
        reason = getattr(e, "user_message", None) or str(e) or type(e).__name__
        logger.warning("Stripe group checkout creation failed: %s", reason)
        return {"ok": False, "error": f"{type(e).__name__}: {reason}"[:500]}

    for s in sessions:
        try:
            db_update_checkout_session(
                s["id"],
                {
                    "status": _STATUS_IN_PROGRESS,
                    "stripe_session_id": sid,
                    "stripe_transfer_group": transfer_group,
                },
            )
        except InvalidStatusTransition as e:
            logger.warning(
                "group stripe stamp: bad transition on %s: %s", s["id"], e
            )

    return {
        "ok": True,
        "client_secret": client_secret,
        "stripe_session_id": sid,
        "stripe_transfer_group": transfer_group,
    }


def _resolve_cancel_url(*, return_origin: Optional[str] = None) -> str:
    if return_origin:
        try:
            from urllib.parse import urlparse

            p = urlparse(return_origin)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}/?checkout=cancelled"
        except Exception:
            pass
    return os.environ.get("VITRINA_STRIPE_CANCEL_URL") or "https://vitrina.app/?checkout=cancelled"


def create_stripe_hosted_session_for_group(
    group_id: str,
    *,
    buyer_email: Optional[str] = None,
    return_origin: Optional[str] = None,
) -> dict[str, Any]:
    """Hosted Stripe Checkout — returns a tappable ``url`` (Instagram, SMS, etc.)."""
    stripe_items, sessions = _group_line_items_for_stripe(group_id)
    if not stripe_items:
        return {"ok": False, "error": "empty_group"}

    primary_session_id = sessions[0]["id"] if sessions else group_id
    success_url = _resolve_return_url(return_origin=return_origin)
    cancel_url = _resolve_cancel_url(return_origin=return_origin)
    transfer_group = _stripe_transfer_group(group_id=group_id)

    params: dict[str, Any] = {
        "ui_mode": STRIPE_UI_MODE_HOSTED_PAGE,
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": stripe_items,
        "client_reference_id": f"acp_group_{group_id}",
        "metadata": {
            "vitrina_session_id": primary_session_id,
            "group_id": group_id,
            "transfer_group": transfer_group,
        },
        "payment_intent_data": {
            "transfer_group": transfer_group,
            "metadata": {
                "vitrina_session_id": primary_session_id,
                "group_id": group_id,
                "transfer_group": transfer_group,
            },
        },
    }
    if buyer_email:
        params["customer_email"] = buyer_email

    try:
        client = _get_stripe_client()
        stripe_session = client.checkout.sessions.create(params=params)
        sid = getattr(stripe_session, "id", None) or stripe_session["id"]  # type: ignore[index]
        pay_url = getattr(stripe_session, "url", None) or stripe_session.get("url")  # type: ignore[union-attr]
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # pragma: no cover — defensive
        reason = getattr(e, "user_message", None) or str(e) or type(e).__name__
        logger.warning("Stripe hosted checkout creation failed: %s", reason)
        return {"ok": False, "error": f"{type(e).__name__}: {reason}"[:500]}

    for s in sessions:
        try:
            db_update_checkout_session(
                s["id"],
                {
                    "status": _STATUS_IN_PROGRESS,
                    "stripe_session_id": sid,
                    "stripe_transfer_group": transfer_group,
                },
            )
        except InvalidStatusTransition as e:
            logger.warning(
                "hosted stripe stamp: bad transition on %s: %s", s["id"], e
            )

    return {
        "ok": True,
        "url": pay_url,
        "stripe_session_id": sid,
        "stripe_transfer_group": transfer_group,
    }


def _stripe_obj_get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _latest_charge_id(client: Any, payment_intent_id: str | None) -> str | None:
    if not payment_intent_id:
        return None
    try:
        intent = client.payment_intents.retrieve(payment_intent_id)
    except Exception as exc:  # pragma: no cover - Stripe API surface drift
        logger.warning("stripe payment_intent retrieve failed id=%s err=%s", payment_intent_id, exc)
        return None
    latest = _stripe_obj_get(intent, "latest_charge")
    if isinstance(latest, str):
        return latest
    return _stripe_obj_get(latest, "id")


def create_store_transfer_for_order(
    order: dict[str, Any],
    *,
    payment_intent_id: str | None,
    transfer_group: str | None,
) -> dict[str, Any]:
    """Send one store's post-fee share to its Stripe Connect account.

    A multi-shop checkout is one platform charge. After Stripe confirms the
    charge, this helper creates one Connect transfer per materialised order.
    If a store has no ``shops.stripe_connect_account_id`` yet, we record the
    intended payout numbers but do not block order creation.
    """
    cart_id = str(order.get("cart_id") or "")
    summary = _cart_stripe_summary(cart_id) if cart_id else {}
    shop_id = str(order.get("shop_id") or summary.get("shop_id") or "")
    shop_row = summary.get("shop") or _load_shop_row(shop_id)
    account_id = shop_row.get("stripe_connect_account_id")
    gross = _safe_int(summary.get("gross_amount_minor"))
    currency = str(summary.get("currency") or order.get("currency") or "eur").lower()
    fee_bps = _platform_fee_bps(shop_row)
    platform_fee = int(round(gross * fee_bps / 10_000))
    payout = max(0, gross - platform_fee)
    transfer_group = transfer_group or _stripe_transfer_group(
        session_id=order.get("checkout_session_id"),
        group_id=order.get("group_id"),
    )

    result: dict[str, Any] = {
        "payment_intent_id": payment_intent_id,
        "stripe_transfer_group": transfer_group,
        "stripe_connect_account_id": account_id,
        "gross_amount_minor": gross,
        "platform_fee_minor": platform_fee,
        "payout_amount_minor": payout,
        "payout_currency": currency,
        "payout_status": "pending",
    }
    if not account_id:
        result["payout_status"] = "connect_account_missing"
        return result
    if not payment_intent_id:
        result["payout_status"] = "payment_intent_missing"
        return result
    if payout <= 0:
        result["payout_status"] = "nothing_to_transfer"
        return result

    try:
        client = _get_stripe_client()
        charge_id = _latest_charge_id(client, payment_intent_id)
        params: dict[str, Any] = {
            "amount": payout,
            "currency": currency,
            "destination": account_id,
            "transfer_group": transfer_group,
            "metadata": {
                "vitrina_order_id": str(order.get("id") or ""),
                "vitrina_session_id": str(order.get("checkout_session_id") or ""),
                "vitrina_shop_id": shop_id,
                "platform_fee_bps": str(fee_bps),
                "platform_fee_minor": str(platform_fee),
            },
        }
        if charge_id:
            params["source_transaction"] = charge_id
        transfer = client.transfers.create(params=params)
        result["stripe_transfer_id"] = _stripe_obj_get(transfer, "id")
        result["stripe_charge_id"] = charge_id
        result["payout_status"] = "transferred"
    except Exception as exc:  # pragma: no cover - keep Stripe retries calm
        logger.exception(
            "stripe connect transfer failed order=%s shop=%s err=%s",
            order.get("id"),
            shop_id,
            exc,
        )
        result["payout_status"] = "transfer_failed"
        result["payout_error"] = str(exc)
    return result


def _stock_race_check(session_row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return ``None`` if every item is still in stock, else a shortfall dict."""
    cart = _load_cart_row(session_row["cart_id"]) if session_row.get("cart_id") else {}
    shop_id = cart.get("shop_id") or ""
    items = load_session_items(session_row)
    for it in items:
        avail = live_stock(shop_id, it["id"])
        if avail < int(it.get("quantity") or 1):
            return {
                "session_id": session_row["id"],
                "sku": it["id"],
                "available": avail,
                "requested": int(it.get("quantity") or 1),
            }
    return None


def complete_checkout_group(group_id: str) -> list[dict[str, Any]]:
    """Webhook / manual target for multi-shop group completion.

    Atomic: every session must pass the stock-race check BEFORE any
    stock is decremented. If any fails, every sibling session is
    flipped to ``canceled`` and no orders are minted. On success each
    session's stock is decremented via :mod:`snapshots` and the row
    flips to ``completed`` — the actual ``orders`` row insert is done
    by the Stripe webhook via :mod:`orders.create_order_from_session`
    (which is idempotent). Returns one ``{session_id, result, ...}``
    entry per session.
    """
    sessions = db_list_sessions_in_group(group_id)
    if not sessions:
        return []

    shortfalls: list[dict[str, Any]] = []
    for s in sessions:
        short = _stock_race_check(s)
        if short is not None:
            shortfalls.append(short)

    if shortfalls:
        out: list[dict[str, Any]] = []
        for s in sessions:
            if s.get("status") in _TERMINAL:
                out.append({"session_id": s["id"], "result": "skipped_terminal"})
                continue
            try:
                db_update_checkout_session(s["id"], {"status": _STATUS_CANCELED})
                out.append(
                    {
                        "session_id": s["id"],
                        "result": "canceled",
                        "reason": "group_stock_race_rollback",
                    }
                )
            except InvalidStatusTransition:
                out.append({"session_id": s["id"], "result": "skipped_terminal"})
        return [
            {"group_id": group_id, "rolled_back": True, "shortfalls": shortfalls},
            *out,
        ]

    results: list[dict[str, Any]] = []
    for s in sessions:
        cart = _load_cart_row(s["cart_id"]) if s.get("cart_id") else {}
        shop_id = cart.get("shop_id") or ""
        items = load_session_items(s)
        if s.get("status") == _STATUS_COMPLETED:
            results.append({"session_id": s["id"], "result": "already_completed"})
            continue
        for it in items:
            snapshots.record_leo_sale(
                shop_id=shop_id,
                product_id=it["id"],
                quantity=int(it.get("quantity") or 1),
                order_ref=f"vitrina_group:{group_id[:12]}…",
            )
        try:
            db_update_checkout_session(s["id"], {"status": _STATUS_COMPLETED})
            results.append(
                {
                    "session_id": s["id"],
                    "result": "completed",
                    "collection_code": _pickup_collection_code(s["id"]),
                }
            )
        except InvalidStatusTransition as e:
            results.append({"session_id": s["id"], "result": "skipped", "reason": str(e)})

    return results


# ── Resend receipt email (stub) ────────────────────────────────────────


def send_order_email(
    *,
    to: str,
    order_id: str,
    items: list[dict[str, Any]],
    total_minor: int,
    shop_id: str,
    collection_code: str,
) -> dict[str, Any]:
    """Fire-and-forget branded receipt via Resend. Stub if no API key."""
    key = os.environ.get(_RESEND_KEY_ENV)
    if not key:
        return {"ok": False, "skipped": True, "error": "resend_api_key_missing"}
    if not to:
        return {"ok": False, "error": "no_recipient"}

    currency = (data.get_shop(shop_id) or {}).get("currency") or "EUR"
    fmt = lambda m: f"{m / 100:.2f} {currency}"  # noqa: E731
    loc = _shop_location_for(shop_id)

    rows = []
    for it in items:
        p = data.get_product(it["id"])
        name = (p or {}).get("name_en") or (p or {}).get("name_bg") or it["id"]
        rows.append(
            f"<tr><td>{name} (SKU {it['id']}) ×{it['quantity']}</td>"
            f"<td>{fmt(price_minor(p or {'price_eur': 0}) * int(it.get('quantity') or 1))}</td></tr>"
        )
    html = (
        "<!doctype html><html><body>"
        f"<h1>Vitrina — order {order_id} confirmed</h1>"
        f"<p>Collection code: <strong>{collection_code}</strong></p>"
        f"<table>{''.join(rows)}"
        f"<tr><td>Total</td><td>{fmt(total_minor)}</td></tr></table>"
        f"<p>Pickup at {loc['name']}, {loc['address']['line_one']}, {loc['address']['city']}.</p>"
        "<p>This is a Vitrina demo order — no real charge was made.</p>"
        "</body></html>"
    )

    try:
        import urllib.request

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            method="POST",
            data=json.dumps(
                {
                    "from": "Vitrina <orders@vitrina.app>",
                    "to": [to],
                    "subject": f"Order {order_id} confirmed — collection code {collection_code}",
                    "html": html,
                }
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            data_out = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "id": data_out.get("id")}
    except Exception as e:  # pragma: no cover — network-level failures
        logger.warning("Resend receipt failed: %s", e)
        return {"ok": False, "error": type(e).__name__}


__all__ = [
    "ACP_API_VERSION",
    "InvalidStatusTransition",
    "ShopClosedError",
    "live_stock",
    "price_minor",
    "build_checkout_session",
    "build_session_view",
    "create_stripe_checkout_link",
    "create_multishop_group",
    "create_single_stripe_session_for_group",
    "create_store_transfer_for_order",
    "complete_checkout_group",
    "db_create_checkout_session",
    "db_get_checkout_session",
    "db_list_sessions_in_group",
    "db_update_checkout_session",
    "load_session_items",
    "send_order_email",
    "_get_stripe_client",
    "_pickup_collection_code",
    "_STATUS_NOT_READY",
    "_STATUS_READY",
    "_STATUS_IN_PROGRESS",
    "_STATUS_REQUIRES_ESCALATION",
    "_STATUS_COMPLETED",
    "_STATUS_CANCELED",
    "_TERMINAL",
    "_VALID_STATUSES",
]
