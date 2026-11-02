"""Order row lifecycle handlers for Vitrina Phase 1.

Every paid Stripe checkout becomes an ``orders`` row in Supabase. That row
carries the canonical ACP-shaped ``line_items`` + ``totals`` JSON (frozen at
payment time), a ``collection_code`` for humans to read at the counter, a
``collect_key`` for the QR workflow (shopper scans -> our app POSTs the
``collect`` endpoint on their phone -> order flips to ``collected``), and the
``fulfillment_status`` 3-state machine:

    awaiting_collection  -> collected      (QR / staff / authed-owner)
    awaiting_collection  -> flushed        (future — write-off path)
    collected            -> collected      (idempotent no-op)

Two callers write orders rows:

1. :mod:`api.routes.webhooks` on ``checkout.session.completed`` (Stripe told
   us the card cleared). Calls :func:`create_order_from_session`.
2. Nothing else in Phase 1 — the MCP agent-native complete path still runs
   through :mod:`checkout_handlers` which owns the ACP wire response. When
   A3 rewrites ``checkout.py`` to persist sessions it will also call
   :func:`create_order_from_session` once the DB session flips to ``paid``.

Reads come from:

- :mod:`api.routes.orders` (GET + list + POST collect for shoppers).
- :mod:`api.routes.cron` (24h sweep — auto-collect anything older than a day
  so pretend-real Phase 1 doesn't leak ``awaiting_collection`` forever).

Mock-friendly: every DB call goes through :func:`_get_supabase` which tests
monkeypatch with :class:`fixtures.supabase_mock.FakeSupabase`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import supabase_client

logger = logging.getLogger(__name__)


# ── exceptions ──────────────────────────────────────────────────────
class OrderNotFound(Exception):
    """No order row matches the supplied id."""


class OrderAccessDenied(Exception):
    """Caller is not the owner and did not supply a valid collect key."""


class InvalidCollectKey(Exception):
    """The supplied collect_key does not match the order's stored key."""


# ── DB indirection ─────────────────────────────────────────────────
def _get_supabase() -> Any:
    return supabase_client.get_supabase()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ── key / code helpers ──────────────────────────────────────────────
def _mint_collect_key() -> str:
    """~20-char URL-safe secret. Paired with the order id in the QR url."""
    return secrets.token_urlsafe(15)


def _collection_code(session_id: str) -> str:
    """6-char uppercase hex, deterministic per session id.

    Intentionally duplicated from ``checkout._pickup_collection_code`` rather
    than imported: A3 is rewriting ``checkout.py`` and we do not want to
    couple orders creation to that file's transient API. The formula is
    trivially two lines; keep them in sync by convention.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:6].upper()


def _permalink(order_id: str) -> str:
    host = os.getenv("VITRINA_PUBLIC_HOST", "https://vitrina.bg").rstrip("/")
    return f"{host}/orders/{order_id}"


# ── line-item / totals shaping ──────────────────────────────────────
def _acp_line_items(cart_lines: list[dict]) -> list[dict]:
    """Flatten the ACP-shaped line_items snapshot the UI / receipt renders.

    Preserves :mod:`cart.add_cart_item`'s ``product_snapshot`` at payment
    time so later catalogue churn can't retroactively change what a
    receipt says.
    """
    out: list[dict] = []
    for line in cart_lines:
        snap = line.get("product_snapshot") or {}
        out.append(
            {
                "id": line.get("id"),
                "product_id": line.get("product_id"),
                "quantity": int(line.get("quantity") or 0),
                "name": snap.get("name") or snap.get("name_en") or snap.get("name_bg"),
                "unit_price_minor": int(line.get("unit_price_minor") or snap.get("price_minor") or 0),
                "subtotal_minor": int(line.get("subtotal_minor") or 0),
                "tax_minor": int(line.get("tax_minor") or 0),
                "total_minor": int(line.get("total_minor") or 0),
                "product_snapshot": snap,
                "group_id": line.get("group_id"),
                "group_metadata": line.get("group_metadata"),
            }
        )
    return out


def _acp_totals(lines: list[dict]) -> dict:
    subtotal = sum(int(l.get("subtotal_minor") or 0) for l in lines)
    tax = sum(int(l.get("tax_minor") or 0) for l in lines)
    total = sum(int(l.get("total_minor") or 0) for l in lines)
    return {
        "subtotal_minor": subtotal,
        "tax_minor": tax,
        "total_minor": total,
        "fulfillment_minor": 0,
        "discount_minor": 0,
    }


def _shop_id_of_cart(cart: dict, lines: list[dict]) -> str:
    """Best-effort shop_id — prefer the cart's column, fall back to the
    first line's snapshot (older seeded carts predate the column)."""
    sid = cart.get("shop_id")
    if sid:
        return sid
    for line in lines:
        snap = line.get("product_snapshot") or {}
        if snap.get("shop_id"):
            return snap["shop_id"]
    return ""


# ── core writer ─────────────────────────────────────────────────────
def create_order_from_session(
    session_id: str,
    *,
    override_paid_at: datetime | None = None,
) -> dict:
    """Materialise an ``orders`` row from a ``checkout_sessions`` id.

    Reads the session + its cart + cart_line_items, builds the ACP-shaped
    line_items/totals JSON, mints a ``collect_key`` + ``collection_code``,
    inserts the order, and flips the cart's status to ``completed``. Idempotent
    on the session id: if an order already exists for this session it is
    returned as-is (no duplicate insert).

    :param session_id: ``checkout_sessions.id`` uuid.
    :param override_paid_at: optional — used by the cron-sweep test to seed
        rows in the past. Defaults to now().
    """
    sb = _get_supabase()

    existing = (
        sb.table("orders")
        .select("*")
        .eq("checkout_session_id", session_id)
        .execute()
    )
    if existing:
        return existing[0]

    session = (
        sb.table("checkout_sessions")
        .select("*")
        .eq("id", session_id)
        .single()
        .execute()
    )
    if not session:
        raise OrderNotFound(f"checkout_session:{session_id}")

    cart_id = session["cart_id"]
    cart = (
        sb.table("carts").select("*").eq("id", cart_id).single().execute()
    )
    if not cart:
        raise OrderNotFound(f"cart:{cart_id}")

    lines = (
        sb.table("cart_line_items")
        .select("*")
        .eq("cart_id", cart_id)
        .order("created_at")
        .execute()
    ) or []

    acp_items = _acp_line_items(lines)
    totals = _acp_totals(lines)
    paid_at = (override_paid_at or _now()).isoformat()

    # Deterministic code per session (stable across retries); fresh key per
    # order row so lost QRs can't be replayed from an old paid session.
    collection_code = _collection_code(session_id)
    collect_key = _mint_collect_key()

    order_row: dict[str, Any] = {
        "checkout_session_id": session_id,
        "group_id": session.get("group_id"),
        "cart_id": cart_id,
        "user_id": cart.get("user_id"),
        "shop_id": _shop_id_of_cart(cart, lines),
        "currency": cart.get("currency") or "EUR",
        "line_items": acp_items,
        "totals": totals,
        "collection_code": collection_code,
        "collect_key": collect_key,
        "fulfillment_status": "awaiting_collection",
        "paid_at": paid_at,
        "permalink_url": "",  # filled below once we know the id
    }

    inserted = sb.table("orders").insert(order_row).execute()
    row = inserted[0] if isinstance(inserted, list) else inserted
    if not row:
        raise RuntimeError("order_insert_failed")

    permalink = _permalink(row["id"])
    updated = (
        sb.table("orders")
        .update({"permalink_url": permalink})
        .eq("id", row["id"])
        .execute()
    )
    row = (updated[0] if isinstance(updated, list) and updated else row)
    row["permalink_url"] = permalink

    sb.table("carts").update({"status": "completed"}).eq("id", cart_id).execute()

    logger.info(
        "order.created id=%s session=%s shop=%s total=%s",
        row["id"],
        session_id,
        order_row["shop_id"],
        totals["total_minor"],
    )
    return row


def update_order_payment_details(order_id: str, patch: dict[str, Any]) -> dict:
    """Attach Stripe payment / payout bookkeeping to an existing order row."""
    allowed = {
        "payment_intent_id",
        "stripe_charge_id",
        "stripe_transfer_group",
        "stripe_connect_account_id",
        "gross_amount_minor",
        "platform_fee_minor",
        "payout_amount_minor",
        "payout_currency",
        "stripe_transfer_id",
        "payout_status",
        "payout_error",
    }
    payload = {k: v for k, v in patch.items() if k in allowed}
    if not payload:
        return _load_order(order_id)
    sb = _get_supabase()
    try:
        updated = sb.table("orders").update(payload).eq("id", order_id).execute()
        row = updated[0] if isinstance(updated, list) and updated else updated
        return row or _load_order(order_id)
    except Exception as exc:  # pragma: no cover - older schema tolerance
        logger.warning("order payment detail update failed id=%s err=%s", order_id, exc)
        return _load_order(order_id)


# ── readers ─────────────────────────────────────────────────────────
def _load_order(order_id: str) -> dict:
    sb = _get_supabase()
    row = sb.table("orders").select("*").eq("id", order_id).single().execute()
    if not row:
        raise OrderNotFound(order_id)
    return row


def get_order(
    order_id: str,
    *,
    user_id: str | None = None,
    collect_key: str | None = None,
) -> dict:
    """Fetch a single order row.

    Auth: either ``user_id`` matches ``orders.user_id`` OR ``collect_key``
    matches ``orders.collect_key``. Raises :class:`OrderAccessDenied` if
    neither proves ownership. (The collect_key is intentionally
    readable-from-QR so a non-logged-in shopper can see their order.)
    """
    row = _load_order(order_id)
    if user_id and row.get("user_id") == user_id:
        return row
    if collect_key and row.get("collect_key") == collect_key:
        return row
    raise OrderAccessDenied("order_access_denied")


def list_user_orders(
    user_id: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List orders belonging to ``user_id``, newest first."""
    sb = _get_supabase()
    q = sb.table("orders").select("*").eq("user_id", user_id)
    if status:
        q = q.eq("fulfillment_status", status)
    return q.order("paid_at", desc=True).limit(limit).execute() or []


def list_orders_for_cart_key(
    cart_key: str,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List orders tied to anonymous carts owned by ``cart_key``."""
    if not cart_key:
        return []
    sb = _get_supabase()
    carts = (
        sb.table("carts")
        .select("id")
        .eq("cart_key", cart_key)
        .execute()
    ) or []
    cart_ids = [str(row.get("id")) for row in carts if row.get("id")]
    if not cart_ids:
        return []
    q = sb.table("orders").select("*").in_("cart_id", cart_ids)
    if status:
        q = q.eq("fulfillment_status", status)
    return q.order("paid_at", desc=True).limit(limit).execute() or []


# ── collect (QR + staff + authed owner) ─────────────────────────────
def collect_order(
    order_id: str,
    *,
    collect_key: str | None = None,
    user_id: str | None = None,
    operator_role: bool = False,
) -> dict:
    """Flip an order to ``collected``. Idempotent.

    Auth precedence:

    1. ``collect_key`` present AND matches -> ``collected_via='qr'``.
    2. ``operator_role=True`` -> ``collected_via='staff'`` (shop counter app).
    3. ``user_id`` matches owner -> ``collected_via='manual'``.

    If none of those hold we raise :class:`InvalidCollectKey` (when a key was
    supplied but wrong) or :class:`OrderAccessDenied` (no credentials at all).
    Already-collected rows are returned unmodified.
    """
    row = _load_order(order_id)

    if row.get("fulfillment_status") == "collected":
        return row

    via: str | None = None
    if collect_key is not None:
        if row.get("collect_key") == collect_key:
            via = "qr"
        else:
            raise InvalidCollectKey("invalid_collect_key")
    elif operator_role:
        via = "staff"
    elif user_id and row.get("user_id") == user_id:
        via = "manual"
    else:
        raise OrderAccessDenied("order_access_denied")

    sb = _get_supabase()
    updated = (
        sb.table("orders")
        .update(
            {
                "fulfillment_status": "collected",
                "collected_at": _now_iso(),
                "collected_via": via,
            }
        )
        .eq("id", order_id)
        .execute()
    )
    out = (updated[0] if isinstance(updated, list) and updated else row)
    logger.info("order.collected id=%s via=%s", order_id, via)
    return out


# ── cron sweep (24h auto-collect) ───────────────────────────────────
def sweep_24h_awaiting() -> dict:
    """Auto-collect every ``awaiting_collection`` order older than 24h.

    Phase 1 runs a pretend-real pickup flow; rather than let rows linger
    forever when a shopper never shows, a daily cron flushes them to
    ``collected`` with ``collected_via='cron_24h'``. Returns the summary
    ``{swept, order_ids}``.
    """
    sb = _get_supabase()
    rows = (
        sb.table("orders")
        .select("*")
        .eq("fulfillment_status", "awaiting_collection")
        .execute()
    ) or []

    cutoff = _now() - timedelta(hours=24)
    swept_ids: list[str] = []
    now_iso = _now_iso()

    for row in rows:
        paid_raw = row.get("paid_at")
        if not paid_raw:
            continue
        try:
            paid_dt = datetime.fromisoformat(str(paid_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if paid_dt.tzinfo is None:
            paid_dt = paid_dt.replace(tzinfo=timezone.utc)
        if paid_dt >= cutoff:
            continue
        sb.table("orders").update(
            {
                "fulfillment_status": "collected",
                "collected_at": now_iso,
                "collected_via": "cron_24h",
            }
        ).eq("id", row["id"]).execute()
        swept_ids.append(row["id"])

    logger.info("order.sweep_24h swept=%d", len(swept_ids))
    return {"swept": len(swept_ids), "order_ids": swept_ids}


# ── tool descriptors (coordinator registers these) ──────────────────
ORDER_TOOLS = [
    {
        "name": "orders.get",
        "description": (
            "Fetch a single order by id. Authed owner or caller with the "
            "matching collect_key (QR flow)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "user_id": {"type": "string"},
                "collect_key": {"type": "string"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "orders.list_mine",
        "description": "List the authed user's orders, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "orders.collect",
        "description": (
            "Mark an order collected. Accepts a shopper collect_key (QR) or "
            "an authed owner; idempotent on already-collected rows."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "collect_key": {"type": "string"},
                "user_id": {"type": "string"},
                "operator_role": {"type": "boolean"},
            },
            "required": ["order_id"],
        },
    },
]
