"""Tests for ``mcp.orders.sweep_24h_awaiting``.

Seeds three orders: two paid >24h ago (should sweep), one paid <24h ago
(should be left alone). Asserts the return shape and the on-row side
effects (``collected_via='cron_24h'``, ``collected_at`` stamped).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import orders as orders_mod
from fixtures import supabase_mock


@pytest.fixture(autouse=True)
def _fake_supabase(monkeypatch: pytest.MonkeyPatch) -> supabase_mock.FakeSupabase:
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(orders_mod, "_get_supabase", lambda: fake)
    yield fake
    supabase_mock.reset()


def _seed(order_id: str, paid_at: datetime, status: str = "awaiting_collection") -> None:
    supabase_mock.seed(
        "orders",
        [
            {
                "id": order_id,
                "checkout_session_id": f"sess-{order_id}",
                "cart_id": f"cart-{order_id}",
                "user_id": "user-1",
                "shop_id": "shop-1",
                "currency": "EUR",
                "line_items": [],
                "totals": {"total_minor": 100},
                "collection_code": "ABCDEF",
                "collect_key": f"key-{order_id}",
                "fulfillment_status": status,
                "paid_at": paid_at.isoformat(),
                "permalink_url": f"https://vitrina.bg/orders/{order_id}",
            }
        ],
    )


def test_sweep_flips_orders_older_than_24h() -> None:
    now = datetime.now(timezone.utc)
    _seed("old-1", now - timedelta(hours=30))
    _seed("old-2", now - timedelta(days=3))
    _seed("fresh", now - timedelta(hours=2))

    result = orders_mod.sweep_24h_awaiting()

    assert result["swept"] == 2
    assert set(result["order_ids"]) == {"old-1", "old-2"}

    rows = {r["id"]: r for r in supabase_mock.dump("orders")}
    assert rows["old-1"]["fulfillment_status"] == "collected"
    assert rows["old-1"]["collected_via"] == "cron_24h"
    assert rows["old-1"]["collected_at"] is not None
    assert rows["old-2"]["fulfillment_status"] == "collected"
    assert rows["old-2"]["collected_via"] == "cron_24h"
    assert rows["fresh"]["fulfillment_status"] == "awaiting_collection"
    assert rows["fresh"].get("collected_via") is None


def test_sweep_ignores_already_collected() -> None:
    now = datetime.now(timezone.utc)
    _seed("old-collected", now - timedelta(days=5), status="collected")

    result = orders_mod.sweep_24h_awaiting()

    assert result["swept"] == 0
    assert result["order_ids"] == []


def test_sweep_empty_table_returns_zero() -> None:
    result = orders_mod.sweep_24h_awaiting()
    assert result == {"swept": 0, "order_ids": []}
