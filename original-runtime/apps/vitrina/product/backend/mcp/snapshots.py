"""Two-track live-stock ledger.

Port of the Node prototype's ``src/pos_snapshots.js``. The model is the
thesis of the whole vitrina project:

* **Track 1 — ``shop_stock``** — a read-only mirror of the shop's own POS
  database (Microinvest WHP today, ~100 vendors over time — see
  ``apps/vitrina/product/integrations/``). A vendor-specific
  connector polls SELECT-only at the cadence appropriate to that vendor
  (~60 s for on-prem readers like ``whp-reader``; webhook-driven for SaaS
  vendors like Shopify) and POSTs to the ``report_pos_snapshot`` tool.
  Leo *never* writes back.
* **Track 2 — ``leo_sold_delta``** — what Leo has sold since the shop last
  reconciled (typically via Stripe checkout). Incremented by
  ``record_leo_sale``; zeroed by ``shop_acknowledge_sales`` when the
  shopkeeper taps "fulfilled".

The effective stock a customer should be quoted is
``max(0, shop_stock - leo_sold_delta)`` — implemented in
:func:`effective_stock_view`.

**Current backing store: in-memory module-level state.** Fine for the live
Claude demo (vendor connectors re-report every ~60 s so state recovers
automatically after a restart). On multi-process deployments, migrate to
Supabase via ``core.Supabase`` (new tables ``shop_stock_snapshots`` and
``leo_sale_ledger``) — that's the next graduation step after the Python
port is verified. The interface below is designed so that swap touches
only this file.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque

_MAX_NOTIFICATIONS = 500
_MAX_AUDIT = 1000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class _Snapshot:
    shop_id: str
    source: str
    host: str
    as_of: str
    received_at: str
    products: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def product_count(self) -> int:
        return len(self.products)


@dataclass
class _Ledger:
    """Module-level singleton holding the two-track state + notifications.

    Kept as a dataclass instead of bare globals so tests can instantiate a
    fresh ledger without touching module state.
    """

    shop_stock: dict[str, _Snapshot] = field(default_factory=dict)
    leo_delta: dict[str, dict[str, int]] = field(default_factory=dict)
    notifications: Deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_MAX_NOTIFICATIONS)
    )
    audit: Deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_MAX_AUDIT)
    )

    def _audit(self, event: str, detail: dict[str, Any]) -> None:
        self.audit.append({"at": _utcnow(), "event": event, "detail": detail})


_LEDGER = _Ledger()


def _ledger() -> _Ledger:
    """Test hook: swap the module-level ledger during pytest if needed."""
    return _LEDGER


def reset_for_tests() -> None:
    """Wipe the ledger. Only call from tests."""
    global _LEDGER
    _LEDGER = _Ledger()


def record_shop_snapshot(
    *,
    shop_id: str,
    products: list[dict[str, Any]],
    source: str | None = None,
    host: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    if not shop_id:
        raise ValueError("shop_id required")
    if not isinstance(products, list):
        raise ValueError("products must be an array")

    indexed: dict[str, dict[str, Any]] = {}
    for p in products:
        pid = str(p.get("product_id") or p.get("id") or p.get("code") or "").strip()
        if not pid:
            continue
        indexed[pid] = {
            "product_id": pid,
            "name_bg": p.get("name_bg") or p.get("name") or "",
            "name_en": p.get("name_en") or "",
            "barcode": p.get("barcode") or p.get("ean") or "",
            "price_eur": float(p.get("price_eur") or p.get("price") or 0) or 0.0,
            "unit": p.get("unit") or p.get("measure") or "",
            "stock": float(p.get("stock") or p.get("qty") or p.get("quantity") or 0) or 0.0,
        }

    snap = _Snapshot(
        shop_id=str(shop_id),
        source=source or "unknown",
        host=host or "",
        as_of=as_of or _utcnow(),
        received_at=_utcnow(),
        products=indexed,
    )
    _ledger().shop_stock[str(shop_id)] = snap
    _ledger()._audit(
        "snapshot.recorded",
        {"shop_id": shop_id, "product_count": len(indexed), "source": source},
    )
    return {"ok": True, "shop_id": str(shop_id), "product_count": len(indexed)}


def get_shop_snapshot(shop_id: str) -> _Snapshot | None:
    return _ledger().shop_stock.get(str(shop_id))


def all_shop_snapshots() -> list[_Snapshot]:
    return list(_ledger().shop_stock.values())


def record_leo_sale(
    *, shop_id: str, product_id: str, quantity: int, order_ref: str | None = None
) -> dict[str, Any]:
    if not shop_id:
        raise ValueError("shop_id required")
    if not product_id:
        raise ValueError("product_id required")
    qty = max(1, int(quantity or 1))
    shop_deltas = _ledger().leo_delta.setdefault(str(shop_id), {})
    shop_deltas[product_id] = shop_deltas.get(product_id, 0) + qty
    _ledger()._audit(
        "sale.recorded",
        {"shop_id": shop_id, "product_id": product_id, "quantity": qty, "order_ref": order_ref},
    )
    _ledger().notifications.append(
        {
            "at": _utcnow(),
            "shop_id": str(shop_id),
            "kind": "leo_sale",
            "product_id": product_id,
            "quantity": qty,
            "order_ref": order_ref,
            "read": False,
        }
    )
    return {
        "ok": True,
        "shop_id": str(shop_id),
        "product_id": product_id,
        "new_leo_sold_total": shop_deltas[product_id],
    }


def get_leo_delta(shop_id: str, product_id: str) -> int:
    return _ledger().leo_delta.get(str(shop_id), {}).get(product_id, 0)


def shop_acknowledged_sales(
    *,
    shop_id: str,
    product_ids: list[str] | None = None,
    writeback_to_pos: bool = False,
) -> dict[str, Any]:
    shop_deltas = _ledger().leo_delta.get(str(shop_id), {})
    cleared: list[dict[str, Any]] = []
    targets = product_ids if product_ids else list(shop_deltas.keys())
    for pid in targets:
        qty = shop_deltas.get(pid, 0)
        if qty:
            cleared.append({"product_id": pid, "qty": qty})
            shop_deltas.pop(pid, None)
    _ledger()._audit(
        "sales.acknowledged",
        {"shop_id": shop_id, "cleared": cleared, "writeback_to_pos": bool(writeback_to_pos)},
    )
    return {
        "ok": True,
        "shop_id": str(shop_id),
        "cleared": cleared,
        "writeback_performed": False,
    }


def push_notification(*, shop_id: str, kind: str, body: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "at": _utcnow(),
        "shop_id": str(shop_id),
        "kind": kind or "info",
        "body": body or {},
        "read": False,
    }
    _ledger().notifications.append(entry)
    _ledger()._audit("notification.pushed", {"shop_id": shop_id, "kind": kind})
    return entry


def list_notifications(*, shop_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    buf = _ledger().notifications
    if shop_id:
        filtered = [n for n in buf if n["shop_id"] == str(shop_id)]
    else:
        filtered = list(buf)
    return list(reversed(filtered[-limit:]))


def effective_stock_view(shop_id: str) -> dict[str, Any] | None:
    """The merged two-track view: shop_stock − leo_sold_delta.

    Returns ``None`` when no snapshot has been reported yet for this shop
    (the Sofia demo data can still render with static stock from the seed
    JSON in that case — handled by the caller).
    """
    snap = _ledger().shop_stock.get(str(shop_id))
    if snap is None:
        return None
    delta = _ledger().leo_delta.get(str(shop_id), {})
    rows: list[dict[str, Any]] = []
    for p in snap.products.values():
        sold = delta.get(p["product_id"], 0)
        rows.append(
            {
                **p,
                "shop_stock": p["stock"],
                "leo_sold_since_reconcile": sold,
                "effective_stock": max(0.0, p["stock"] - sold),
            }
        )
    return {
        "shop_id": snap.shop_id,
        "source": snap.source,
        "host": snap.host,
        "as_of": snap.as_of,
        "received_at": snap.received_at,
        "product_count": len(rows),
        "products": rows,
        "total_leo_sold_since_reconcile": sum(delta.values()),
    }


def get_audit(limit: int = 100) -> list[dict[str, Any]]:
    buf = list(_ledger().audit)
    return list(reversed(buf[-limit:]))
