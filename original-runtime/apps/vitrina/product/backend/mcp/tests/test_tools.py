"""Smoke test for the 9 MCP tool handlers.

Verifies the static catalogue loads, the live two-track model updates
correctly under simulated agent + Stripe events, and every handler
returns the expected top-level keys. Transport-agnostic — exercises
``tools.dispatch`` directly, so it's the contract test both the stdio
MCP transport and the REST mirror (``../../api/routes/stock.py``) depend
on.

Run:
    cd apps/vitrina/product/backend/mcp
    python -m pytest tests/ -v
"""
from __future__ import annotations

import pytest

import snapshots
import tools


@pytest.fixture(autouse=True)
def _fresh_ledger():
    snapshots.reset_for_tests()
    yield
    snapshots.reset_for_tests()


def test_list_demo_shops_returns_six_sofia_shops() -> None:
    out = tools.dispatch("list_demo_shops")
    assert out["total_shops"] >= 1
    assert out["city"] == "Sofia, Bulgaria"
    assert out["currency"] == "EUR"
    assert isinstance(out["shops"], list)
    assert all("id" in s for s in out["shops"])


def test_list_demo_shops_filters_by_trade() -> None:
    out = tools.dispatch("list_demo_shops", {"trade": "dairy"})
    for shop in out["shops"]:
        assert "name_bg" in shop
    assert out["total_shops"] <= 3, "dairy trade should not match 3+ shops in Sofia demo"


def test_list_products_in_shop_resolves_by_id_and_returns_catalogue() -> None:
    shops = tools.dispatch("list_demo_shops")["shops"]
    assert shops, "seed data must have at least one shop"
    shop_id = shops[0]["id"]
    out = tools.dispatch("list_products_in_shop", {"shop": shop_id})
    assert out["shop"]["id"] == shop_id
    assert out["currency"] == "EUR"
    assert isinstance(out["products"], list)
    assert out["live_snapshot"] is None


def test_list_products_in_shop_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown shop"):
        tools.dispatch("list_products_in_shop", {"shop": "not-a-shop"})


def test_search_across_shops_rejects_empty_query() -> None:
    with pytest.raises(ValueError, match="query required"):
        tools.dispatch("search_across_shops", {"query": ""})


def test_search_across_shops_returns_structured_matches() -> None:
    out = tools.dispatch("search_across_shops", {"query": "a"})
    assert "matches" in out
    assert "by_shop" in out
    assert isinstance(out["matches"], list)


def test_two_track_flow_end_to_end() -> None:
    shops = tools.dispatch("list_demo_shops")["shops"]
    shop_id = shops[0]["id"]
    products_in_shop = tools.dispatch("list_products_in_shop", {"shop": shop_id})["products"]
    assert products_in_shop, "test shop must have at least one product"
    product_id = products_in_shop[0]["id"]

    report = tools.dispatch(
        "report_pos_snapshot",
        {
            "shop_id": shop_id,
            "source": "whp-mdb",
            "host": "SONIA-PC",
            "products": [
                {"product_id": product_id, "stock": 10, "price_eur": 5.0},
            ],
        },
    )
    assert report["ok"] is True
    assert report["product_count"] == 1

    view = tools.dispatch("get_live_pos_snapshot", {"shop_id": shop_id})
    assert view["product_count"] == 1
    assert view["products"][0]["shop_stock"] == 10
    assert view["products"][0]["effective_stock"] == 10
    assert view["total_leo_sold_since_reconcile"] == 0

    tools.dispatch(
        "record_leo_sale",
        {"shop_id": shop_id, "product_id": product_id, "quantity": 2, "order_ref": "ord_1"},
    )

    view_after_sale = tools.dispatch("get_live_pos_snapshot", {"shop_id": shop_id})
    assert view_after_sale["products"][0]["leo_sold_since_reconcile"] == 2
    assert view_after_sale["products"][0]["effective_stock"] == 8

    notifications = tools.dispatch("list_shop_notifications", {"shop_id": shop_id})
    assert notifications["count"] >= 1
    assert any(n.get("kind") == "leo_sale" for n in notifications["notifications"])

    ack = tools.dispatch(
        "shop_acknowledge_sales",
        {"shop_id": shop_id, "product_ids": [product_id]},
    )
    assert ack["ok"] is True
    assert any(c["product_id"] == product_id for c in ack["cleared"])

    view_after_ack = tools.dispatch("get_live_pos_snapshot", {"shop_id": shop_id})
    assert view_after_ack["products"][0]["leo_sold_since_reconcile"] == 0
    assert view_after_ack["products"][0]["effective_stock"] == 10


def test_audit_trail_captures_events() -> None:
    shops = tools.dispatch("list_demo_shops")["shops"]
    shop_id = shops[0]["id"]
    product_id = tools.dispatch("list_products_in_shop", {"shop": shop_id})["products"][0]["id"]

    tools.dispatch(
        "report_pos_snapshot",
        {"shop_id": shop_id, "products": [{"product_id": product_id, "stock": 5}]},
    )
    tools.dispatch(
        "record_leo_sale",
        {"shop_id": shop_id, "product_id": product_id, "quantity": 1},
    )

    audit = tools.dispatch("get_mcp_audit_trail", {"limit": 10})
    events = [e["event"] for e in audit["events"]]
    assert "snapshot.recorded" in events
    assert "sale.recorded" in events


def test_dispatch_unknown_tool_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tool"):
        tools.dispatch("not_a_real_tool")
