"""Tests for ``mcp.orders.collect_order`` — the QR + authed-owner path.

Covers the five auth branches documented in the handler docstring:
QR (valid key), invalid key, authed owner w/o key, anonymous w/o key,
and the idempotent already-collected re-run.
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


def _seed_order(**overrides) -> dict:
    row = {
        "id": "order-1",
        "checkout_session_id": "sess-1",
        "cart_id": "cart-1",
        "user_id": "user-alice",
        "shop_id": "shop-1",
        "currency": "EUR",
        "line_items": [],
        "totals": {"total_minor": 500},
        "collection_code": "ABC123",
        "collect_key": "secret-key-20chars-xx",
        "fulfillment_status": "awaiting_collection",
        "paid_at": datetime.now(timezone.utc).isoformat(),
        "permalink_url": "https://vitrina.bg/orders/order-1",
    }
    row.update(overrides)
    supabase_mock.seed("orders", [row])
    return row


def test_collect_with_valid_key_flips_via_qr() -> None:
    _seed_order()
    out = orders_mod.collect_order("order-1", collect_key="secret-key-20chars-xx")
    assert out["fulfillment_status"] == "collected"
    assert out["collected_via"] == "qr"
    assert out["collected_at"] is not None


def test_collect_with_wrong_key_raises_invalid_collect_key() -> None:
    _seed_order()
    with pytest.raises(orders_mod.InvalidCollectKey):
        orders_mod.collect_order("order-1", collect_key="nope")
    # row is unchanged
    stored = supabase_mock.dump("orders")[0]
    assert stored["fulfillment_status"] == "awaiting_collection"


def test_collect_as_authed_owner_without_key_flips_via_manual() -> None:
    _seed_order()
    out = orders_mod.collect_order("order-1", user_id="user-alice")
    assert out["fulfillment_status"] == "collected"
    assert out["collected_via"] == "manual"


def test_collect_anonymous_without_key_raises_access_denied() -> None:
    _seed_order()
    with pytest.raises(orders_mod.OrderAccessDenied):
        orders_mod.collect_order("order-1")
    stored = supabase_mock.dump("orders")[0]
    assert stored["fulfillment_status"] == "awaiting_collection"


def test_collect_already_collected_is_idempotent() -> None:
    collected_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _seed_order(
        fulfillment_status="collected",
        collected_at=collected_at,
        collected_via="qr",
    )
    out = orders_mod.collect_order("order-1", collect_key="secret-key-20chars-xx")
    assert out["fulfillment_status"] == "collected"
    # Still the ORIGINAL collected_at — no mutation on the no-op branch.
    assert out["collected_at"] == collected_at
    assert out["collected_via"] == "qr"


def test_operator_role_flips_via_staff() -> None:
    _seed_order()
    out = orders_mod.collect_order("order-1", operator_role=True)
    assert out["fulfillment_status"] == "collected"
    assert out["collected_via"] == "staff"
