"""Tests for UCP cart CRUD + transfer flow (mcp/cart.py)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import cart  # noqa: E402 — sys.path set up by conftest
from fixtures import supabase_mock  # noqa: E402


@pytest.fixture
def fake_sb(monkeypatch):
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(cart, "_get_supabase", lambda: fake)
    supabase_mock.seed("shops", [{"id": "s1", "name": "Shop One"}])
    supabase_mock.seed(
        "products",
        [
            {"id": "p1", "shop_id": "s1", "name": "Apple", "price_eur": 2.50},
            {"id": "p2", "shop_id": "s1", "name": "Bread", "price_eur": 3.00},
        ],
    )
    return fake


def test_create_cart_anon(fake_sb):
    c = cart.create_cart("s1", cart_key="abc")
    assert c["shop_id"] == "s1"
    assert c["cart_key"] == "abc"
    assert c["user_id"] is None
    assert c["status"] == "open"
    assert c["line_items"] == []
    assert c["totals"]["item_count"] == 0


def test_create_cart_requires_identity(fake_sb):
    with pytest.raises(ValueError):
        cart.create_cart("s1")


def test_add_item_computes_pricing(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    out = cart.add_cart_item(c["id"], "p1", 3, cart_key="k")
    line = out["line_items"][0]
    assert line["quantity"] == 3
    assert line["base_amount_minor"] == 750
    assert line["subtotal_minor"] == 750
    assert line["total_minor"] == 750
    assert line["product_snapshot"]["name"] == "Apple"
    assert out["totals"] == {
        "subtotal_minor": 750,
        "tax_minor": 0,
        "total_minor": 750,
        "item_count": 3,
    }


def test_add_item_increments_existing(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    cart.add_cart_item(c["id"], "p1", 2, cart_key="k")
    out = cart.add_cart_item(c["id"], "p1", 3, cart_key="k")
    assert len(out["line_items"]) == 1
    assert out["line_items"][0]["quantity"] == 5
    assert out["line_items"][0]["total_minor"] == 1250


def test_add_item_unknown_product(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    with pytest.raises(cart.ProductNotFound):
        cart.add_cart_item(c["id"], "nope", 1, cart_key="k")


def test_update_item_to_zero_deletes(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    out = cart.add_cart_item(c["id"], "p1", 2, cart_key="k")
    line_id = out["line_items"][0]["id"]
    after = cart.update_cart_item(c["id"], line_id, 0, cart_key="k")
    assert after["line_items"] == []


def test_update_item_changes_price(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    out = cart.add_cart_item(c["id"], "p1", 2, cart_key="k")
    line_id = out["line_items"][0]["id"]
    after = cart.update_cart_item(c["id"], line_id, 4, cart_key="k")
    assert after["line_items"][0]["quantity"] == 4
    assert after["line_items"][0]["total_minor"] == 1000


def test_remove_item(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    out = cart.add_cart_item(c["id"], "p1", 1, cart_key="k")
    line_id = out["line_items"][0]["id"]
    after = cart.remove_cart_item(c["id"], line_id, cart_key="k")
    assert after["line_items"] == []


def test_access_denied_wrong_key(fake_sb):
    c = cart.create_cart("s1", cart_key="secret")
    with pytest.raises(cart.CartAccessDenied):
        cart.get_cart(c["id"], cart_key="wrong")


def test_access_denied_user_vs_key(fake_sb):
    c = cart.create_cart("s1", user_id="u1")
    with pytest.raises(cart.CartAccessDenied):
        cart.get_cart(c["id"], cart_key="anything")


def test_access_ok_matching_user(fake_sb):
    c = cart.create_cart("s1", user_id="u1")
    got = cart.get_cart(c["id"], user_id="u1")
    assert got["id"] == c["id"]


def test_list_carts_by_key(fake_sb):
    cart.create_cart("s1", cart_key="k1")
    cart.create_cart("s1", cart_key="k1")
    cart.create_cart("s1", cart_key="other")
    rows = cart.list_carts_by_key("k1")
    assert len(rows) == 2
    assert all(r["cart_key"] == "k1" for r in rows)


def test_list_user_carts(fake_sb):
    cart.create_cart("s1", user_id="u1")
    cart.create_cart("s1", user_id="u1")
    cart.create_cart("s1", user_id="u2")
    rows = cart.list_user_carts("u1")
    assert len(rows) == 2


def test_discard_cart(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    res = cart.discard_cart(c["id"], cart_key="k")
    assert res == {"cart_id": c["id"], "status": "discarded"}
    row = cart._load_cart_row(c["id"])
    assert row["status"] == "discarded"


def test_discard_current_carts_clears_all_open_carts_for_key(fake_sb):
    a = cart.create_cart("s1", cart_key="device-1")
    b = cart.create_cart("s1", cart_key="device-1")
    other = cart.create_cart("s1", cart_key="device-2")

    res = cart.discard_current_carts(cart_key="device-1")

    assert res["count"] == 2
    assert set(res["cart_ids"]) == {a["id"], b["id"]}
    assert cart.list_carts_by_key("device-1") == []
    assert [row["id"] for row in cart.list_carts_by_key("device-2")] == [other["id"]]


def test_transfer_cart_reassigns_carts_and_threads(fake_sb):
    a = cart.create_cart("s1", cart_key="anon-1")
    b = cart.create_cart("s1", cart_key="anon-1")
    _ = cart.create_cart("s1", cart_key="anon-2")  # different key, untouched
    supabase_mock.seed(
        "chat_threads",
        [
            {"id": "t1", "user_id": None, "cart_key": "anon-1"},
            {"id": "t2", "user_id": None, "cart_key": "anon-1"},
            {"id": "t3", "user_id": None, "cart_key": "other"},
        ],
    )

    result = cart.transfer_cart(["anon-1"], "user-xyz")
    assert result == {"carts_transferred": 2, "threads_transferred": 2}

    transferred_a = cart._load_cart_row(a["id"])
    transferred_b = cart._load_cart_row(b["id"])
    assert transferred_a["user_id"] == "user-xyz"
    assert transferred_a["cart_key"] is None
    assert transferred_b["user_id"] == "user-xyz"

    threads = supabase_mock.dump("chat_threads")
    by_id = {t["id"]: t for t in threads}
    assert by_id["t1"]["user_id"] == "user-xyz"
    assert by_id["t1"]["cart_key"] is None
    assert by_id["t2"]["user_id"] == "user-xyz"
    assert by_id["t3"]["user_id"] is None
    assert by_id["t3"]["cart_key"] == "other"


def test_anon_to_user_flow_end_to_end(fake_sb):
    anon = cart.create_cart("s1", cart_key="device-7")
    cart.add_cart_item(anon["id"], "p1", 2, cart_key="device-7")
    cart.transfer_cart(["device-7"], "user-u")
    # Post-transfer the user can read it via user_id
    reloaded = cart.get_cart(anon["id"], user_id="user-u")
    assert reloaded["user_id"] == "user-u"
    assert reloaded["cart_key"] is None
    assert len(reloaded["line_items"]) == 1
    # And the old cart_key no longer grants access
    with pytest.raises(cart.CartAccessDenied):
        cart.get_cart(anon["id"], cart_key="device-7")


def test_cart_not_found(fake_sb):
    with pytest.raises(cart.CartNotFound):
        cart.get_cart("does-not-exist", cart_key="k")


def test_supabase_client_requires_env(monkeypatch):
    import supabase_client as sbc

    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    sbc.reset_singleton()
    with pytest.raises(RuntimeError, match="supabase_not_configured"):
        sbc.Supabase()
