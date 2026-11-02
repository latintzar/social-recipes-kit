"""Recipe-group + substitution tests for mcp/cart.py.

Recipes in Vitrina are represented as a set of cart_line_items sharing a
``group_id`` plus a ``group_metadata`` blob that carries the recipe name
and any substitution history. These tests cover the add / substitute /
remove lifecycle that the chat agent drives.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import cart  # noqa: E402
from fixtures import supabase_mock  # noqa: E402


RECIPE_INGREDIENTS = [
    ("flour", "Flour", 1.20),
    ("yeast", "Yeast", 0.80),
    ("salt", "Salt", 0.40),
    ("water", "Water", 0.10),
    ("sugar", "Sugar", 0.90),
    ("oil", "Oil", 2.10),
]


@pytest.fixture
def fake_sb(monkeypatch):
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(cart, "_get_supabase", lambda: fake)
    supabase_mock.seed("shops", [{"id": "s1", "name": "Shop One"}])
    supabase_mock.seed(
        "products",
        [
            {"id": pid, "shop_id": "s1", "name": name, "price_eur": price}
            for pid, name, price in RECIPE_INGREDIENTS
        ]
        + [
            {"id": "flour-gf", "shop_id": "s1", "name": "Gluten-Free Flour", "price_eur": 4.50},
        ],
    )
    return fake


def _build_focaccia(cart_id: str, cart_key: str) -> dict:
    group_meta = {"name": "Focaccia", "servings": 4, "substitutions": []}
    last = None
    for pid, _name, _price in RECIPE_INGREDIENTS:
        last = cart.add_cart_item(
            cart_id,
            pid,
            1,
            group_id="recipe-focaccia",
            group_metadata=group_meta,
            cart_key=cart_key,
        )
    return last


def test_add_six_lines_with_group_metadata(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    final = _build_focaccia(c["id"], "k")
    assert len(final["line_items"]) == 6
    for line in final["line_items"]:
        assert line["group_id"] == "recipe-focaccia"
        assert line["group_metadata"]["name"] == "Focaccia"
        assert line["group_metadata"]["servings"] == 4


def test_substitute_appends_substitution_and_updates_snapshot(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    _build_focaccia(c["id"], "k")

    out = cart.substitute_in_recipe(
        c["id"],
        group_id="recipe-focaccia",
        original_product_id="flour",
        replacement_product_id="flour-gf",
        reason="out_of_stock",
        cart_key="k",
    )

    swapped = [l for l in out["line_items"] if l["product_id"] == "flour-gf"]
    assert len(swapped) == 1
    line = swapped[0]
    assert line["product_snapshot"]["name"] == "Gluten-Free Flour"
    assert line["base_amount_minor"] == 450  # 4.50 € * 1 qty
    subs = line["group_metadata"]["substitutions"]
    assert len(subs) == 1
    assert subs[0]["original_product_id"] == "flour"
    assert subs[0]["replacement_product_id"] == "flour-gf"
    assert subs[0]["reason"] == "out_of_stock"
    assert "at" in subs[0]

    assert not any(l["product_id"] == "flour" for l in out["line_items"])


def test_substitute_unknown_line_raises(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    _build_focaccia(c["id"], "k")
    with pytest.raises(cart.CartNotFound):
        cart.substitute_in_recipe(
            c["id"],
            group_id="recipe-focaccia",
            original_product_id="not-there",
            replacement_product_id="flour-gf",
            reason="x",
            cart_key="k",
        )


def test_remove_one_line_keeps_group_metadata_on_others(fake_sb):
    c = cart.create_cart("s1", cart_key="k")
    out = _build_focaccia(c["id"], "k")
    target = next(l for l in out["line_items"] if l["product_id"] == "salt")

    after = cart.remove_cart_item(c["id"], target["id"], cart_key="k")
    assert len(after["line_items"]) == 5
    for line in after["line_items"]:
        assert line["group_id"] == "recipe-focaccia"
        assert line["group_metadata"]["name"] == "Focaccia"


def test_group_id_isolates_duplicate_products(fake_sb):
    """Same product_id in two different groups must NOT merge lines."""
    c = cart.create_cart("s1", cart_key="k")
    cart.add_cart_item(
        c["id"], "flour", 1, group_id="recipe-a",
        group_metadata={"name": "Recipe A"}, cart_key="k",
    )
    out = cart.add_cart_item(
        c["id"], "flour", 1, group_id="recipe-b",
        group_metadata={"name": "Recipe B"}, cart_key="k",
    )
    flour_lines = [l for l in out["line_items"] if l["product_id"] == "flour"]
    assert len(flour_lines) == 2
    groups = {l["group_id"] for l in flour_lines}
    assert groups == {"recipe-a", "recipe-b"}
