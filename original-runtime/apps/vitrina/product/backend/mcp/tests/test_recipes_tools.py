"""Tests for the new recipe-driven shopping MCP tools.

We focus on the deterministic paths (basket_snapshot, leftover-ledger,
stretch ranking with a stubbed catalog). The LLM-driven extract /
substitute paths are exercised by their respective unit tests
elsewhere — here we just smoke-test the dispatch shape.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_HERE = Path(__file__).resolve()
_TESTS_DIR = _HERE.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import cart  # noqa: E402
import recipes_tools  # noqa: E402
from fixtures import supabase_mock  # noqa: E402


PRODUCTS = [
    # pid, name, price_eur, pack_grams (snapshot hint)
    ("pork-belly-400", "Cured belly bacon 400g", 8.40, 400),
    ("soy-500", "Kikkoman soy sauce 500ml", 4.20, 500),
    ("rice-1kg", "Jasmine rice 1kg", 3.10, 1000),
    ("oil-1l", "Sunflower oil 1L", 2.20, 1000),
]


@pytest.fixture
def fake_sb(monkeypatch):
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(cart, "_get_supabase", lambda: fake)
    supabase_mock.seed("shops", [{"id": "s1", "name": "Test Shop"}])
    supabase_mock.seed(
        "products",
        [
            {"id": pid, "shop_id": "s1", "name": name, "price_eur": price}
            for pid, name, price, _ in PRODUCTS
        ],
    )
    return fake


def _add_recipe_group(
    cart_id: str,
    cart_key: str,
    *,
    recipe_id: str,
    name: str,
    items: list[tuple[str, int, int]],  # (pid, quantity_g, pack_grams)
):
    meta_lines = [
        {
            "product_id": pid,
            "ingredient": pid,
            "quantity_g": qg,
            "pack_grams": pg,
            "shop_id": "s1",
        }
        for pid, qg, pg in items
    ]
    group_meta = {
        "name": name,
        "serves": 4,
        "recipe_id": recipe_id,
        "lines": meta_lines,
        "substitutions": [],
    }
    last = None
    for pid, _qg, _pg in items:
        last = cart.add_cart_item(
            cart_id,
            pid,
            1,
            group_id=f"rcp_{recipe_id}",
            group_metadata=group_meta,
            cart_key=cart_key,
        )
    return last


def test_basket_snapshot_groups_by_recipe(fake_sb):
    c = cart.create_cart("s1", cart_key="k1")
    _add_recipe_group(
        c["id"], "k1",
        recipe_id="r1", name="Pork bao",
        items=[("pork-belly-400", 200, 400), ("soy-500", 30, 500)],
    )
    snap = recipes_tools.basket_snapshot(cart_key="k1")
    assert len(snap["groups"]) == 1
    g = snap["groups"][0]
    assert g["recipe_id"] == "r1"
    assert g["name"] == "Pork bao"
    assert {it["product_id"] for it in g["items"]} == {"pork-belly-400", "soy-500"}
    assert snap["total_eur"] > 0
    assert "s1" in snap["shops"]


def test_leftover_ledger_subtracts_used_grams(fake_sb):
    c = cart.create_cart("s1", cart_key="k2")
    _add_recipe_group(
        c["id"], "k2",
        recipe_id="r1", name="Pork bao",
        items=[("pork-belly-400", 150, 400)],  # 250g leftover
    )
    snap = recipes_tools.basket_snapshot(cart_key="k2")
    leftovers = snap["leftover_ledger"]
    assert leftovers.get("pork-belly-400") == 250.0


def test_leftover_ledger_multi_item_group_counts_each_ingredient_once(fake_sb):
    """Every line repeats full group_metadata.lines; ledger must not
    multiply quantity_g by the number of cart lines."""
    c = cart.create_cart("s1", cart_key="k2b")
    _add_recipe_group(
        c["id"], "k2b",
        recipe_id="r2", name="Pork bao",
        items=[
            ("pork-belly-400", 150, 400),
            ("soy-500", 30, 500),
        ],
    )
    snap = recipes_tools.basket_snapshot(cart_key="k2b")
    lo = snap["leftover_ledger"]
    assert lo.get("pork-belly-400") == 250.0  # 400 - 150
    assert lo.get("soy-500") == 470.0  # 500 - 30


def test_leftover_ledger_accumulates_pack_when_line_quantity_gt_one(fake_sb):
    c = cart.create_cart("s1", cart_key="k2c")
    meta_lines = [
        {
            "product_id": "pork-belly-400",
            "ingredient": "pork",
            "quantity_g": 150,
            "pack_grams": 400,
            "shop_id": "s1",
        },
    ]
    group_meta = {
        "name": "Scaled",
        "serves": 4,
        "recipe_id": "r3",
        "lines": meta_lines,
        "substitutions": [],
    }
    cart.add_cart_item(
        c["id"],
        "pork-belly-400",
        2,
        group_id="rcp_r3",
        group_metadata=group_meta,
        cart_key="k2c",
    )
    full = cart._cart_with_lines(c["id"])  # noqa: SLF001
    lo = recipes_tools._leftover_ledger([full])  # noqa: SLF001
    assert lo.get("pork-belly-400") == 650.0  # 2 * 400 - 150


def test_basket_snapshot_respects_active_cart_ids(fake_sb):
    visible = cart.create_cart("s1", cart_key="same-device")
    hidden_old = cart.create_cart("s1", cart_key="same-device")
    _add_recipe_group(
        visible["id"], "same-device",
        recipe_id="visible", name="Visible basket",
        items=[("pork-belly-400", 100, 400)],
    )
    _add_recipe_group(
        hidden_old["id"], "same-device",
        recipe_id="old", name="Old test basket",
        items=[("soy-500", 30, 500)],
    )

    snap = recipes_tools.basket_snapshot(
        cart_key="same-device",
        cart_ids=[visible["id"]],
    )

    assert [g["recipe_id"] for g in snap["groups"]] == ["visible"]
    assert {item["product_id"] for item in snap["items"]} == {"pork-belly-400"}


def test_purchase_units_round_up_to_sellable_pack_size():
    assert recipes_tools._purchase_units_for(  # noqa: SLF001
        {"quantity_g": 750, "pack_grams": 400},
        1.0,
    ) == 2
    assert recipes_tools._purchase_units_for(  # noqa: SLF001
        {"quantity_g": 250, "pack_grams": 500},
        2.0,
    ) == 1


def test_stretch_basket_ranks_by_leftover_reuse(fake_sb, monkeypatch):
    """Recipe B (uses 250g pork) should outrank C (no overlap) when
    the basket already has 300g of pork leftovers from recipe A."""

    c = cart.create_cart("s1", cart_key="k3")
    _add_recipe_group(
        c["id"], "k3",
        recipe_id="A", name="Pork sandwich",
        items=[("pork-belly-400", 100, 400)],  # 300g leftover
    )

    # Stub the catalog so we can fully control the candidates.
    fake_catalog = [
        {
            "id": "B",
            "title": "Pork bao buns",
            "status": "processed",
            "match_universal": {
                "feasibility": "full",
                "shop_groups": [
                    {
                        "shop": {"id": "s1"},
                        "items": [
                            {
                                "product": {"id": "pork-belly-400"},
                                "quantity_g": 250,
                            },
                        ],
                    }
                ],
            },
            "media": {},
            "recipe": {"title": "Pork bao buns"},
        },
        {
            "id": "C",
            "title": "Vegetable curry",
            "status": "processed",
            "match_universal": {
                "feasibility": "full",
                "shop_groups": [
                    {
                        "shop": {"id": "s1"},
                        "items": [
                            {
                                "product": {"id": "rice-1kg"},
                                "quantity_g": 200,
                            },
                        ],
                    }
                ],
            },
            "media": {},
            "recipe": {"title": "Vegetable curry"},
        },
    ]

    from recipe_videos import catalog as recipe_catalog

    monkeypatch.setattr(recipe_catalog, "_catalog", lambda: fake_catalog)
    monkeypatch.setattr(
        recipe_catalog,
        "_resolved_match",
        lambda row, **_: row.get("match_universal"),
    )

    out = recipes_tools.stretch_basket(cart_key="k3", min_reuse_ratio=0.4)
    assert out["candidates"], "expected at least one candidate"
    top = out["candidates"][0]
    assert top["recipe_id"] == "B"
    # 250g need, 300g leftover → fully consumed → reuse_ratio 1.0
    assert top["reuse_ratio"] == 1.0
    # Recipe C uses 0 leftovers (no pork) so its reuse_ratio is 0,
    # which falls below min_reuse_ratio=0.4 — must be filtered out.
    assert all(c["recipe_id"] != "C" for c in out["candidates"])


def test_substitute_falls_back_when_no_llm(monkeypatch, fake_sb):
    """With OPENROUTER_API_KEY unset, substitute uses the heuristic
    path and still returns a sensible replacement."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    supabase_mock.seed(
        "products",
        [{"id": "tofu-300", "shop_id": "s1", "name": "Firm tofu", "price_eur": 3.5}],
    )

    c = cart.create_cart("s1", cart_key="k4")
    _add_recipe_group(
        c["id"], "k4",
        recipe_id="r1", name="Pork bao",
        items=[("pork-belly-400", 200, 400)],
    )

    # Stub the matcher's product index to a small replaceable set.
    from recipe_videos import shop_matcher

    monkeypatch.setattr(
        shop_matcher,
        "_all_products_grouped",
        lambda: {"s1": [
            {"id": "pork-belly-400", "name_en": "Pork belly", "price_eur": 8.4},
            {"id": "tofu-300", "name_en": "Firm tofu", "price_eur": 3.5},
        ]},
    )

    result = recipes_tools.substitute(
        cart_id=c["id"],
        group_id="rcp_r1",
        intent="vegetarian",
        product_id="pork-belly-400",
        cart_key="k4",
    )
    assert result["ok"] is True
    assert result["replacement_product_id"] in {"tofu-300", "pork-belly-400"}


def test_purchase_units_weight_string_without_quantity_g():
    item = {
        "quantity_purchase": "250 g",
        "product": {"unit": "pack", "unit_size": "500 g"},
    }
    assert recipes_tools._purchase_units_for(item, 1.0) == 1


def test_purchase_units_quantity_g_derives_pack_from_unit_size():
    item = {
        "quantity_g": 800,
        "quantity_purchase": "800 g",
        "product": {"unit": "pack", "unit_size": "500g"},
    }
    assert recipes_tools._purchase_units_for(item, 1.0) == 2


def test_purchase_units_count_style_purchase_scaled():
    item = {
        "quantity_purchase": "2 packs",
        "product": {"unit": "pack", "unit_size": "400 g"},
    }
    assert recipes_tools._purchase_units_for(item, 2.0) == 4


def test_tool_registry_contains_all_recipe_tools(fake_sb):
    names = {t["name"] for t in recipes_tools.RECIPE_TOOLS}
    assert names == {
        "recipes.extract_from_url_async",
        "recipes.synthesize_from_brief",
        "recipes.discover_social",
        "recipes.add_to_basket",
        "recipes.replace_recipe_group",
        "recipes.substitute",
        "recipes.stretch_basket",
        "recipes.search",
        "recipes.get",
        "basket.snapshot",
    }
    for name in names:
        assert name in recipes_tools.RECIPE_HANDLERS


def test_additional_shop_count(fake_sb):
    match = {
        "shop_groups": [
            {"shop": {"id": "s1"}, "items": []},
            {"shop": {"id": "s2"}, "items": []},
        ]
    }
    assert recipes_tools._additional_shop_count(match, {"s1"}) == 1
    assert recipes_tools._additional_shop_count(match, {"s1", "s2"}) == 0


def test_discover_social_no_searx(monkeypatch, fake_sb):
    monkeypatch.delenv("VITRINA_SEARXNG_URL", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    out = recipes_tools.discover_social(
        "high protein bowl",
        cart_key="k-disco",
        synthesize_fallback=False,
    )
    assert out.get("ok") is False
    assert out.get("reason") == "searxng_not_configured"


def test_discover_social_synth_fallback(monkeypatch, fake_sb):
    monkeypatch.delenv("VITRINA_SEARXNG_URL", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)

    def _fake_synth(*_a, **_k):
        return {
            "recipe_id": "chat-fake123",
            "detail": {"id": "chat-fake123", "title": "Fake"},
            "match": {},
            "synthetic": True,
        }

    monkeypatch.setattr(recipes_tools, "synthesize_from_brief", _fake_synth)
    out = recipes_tools.discover_social(
        "comfort pasta",
        cart_key="k-disco2",
        synthesize_fallback=True,
    )
    assert "synthesized_fallback" in out
    assert out["synthesized_fallback"]["recipe_id"] == "chat-fake123"


def test_replace_recipe_group_resets_lines(fake_sb, monkeypatch):
    c = cart.create_cart("s1", cart_key="k-rep")
    _add_recipe_group(
        c["id"], "k-rep",
        recipe_id="r1", name="Pork bao",
        items=[("pork-belly-400", 150, 400), ("soy-500", 30, 500)],
    )
    full_before = cart._cart_with_lines(c["id"])  # noqa: SLF001
    assert len(full_before["line_items"]) == 2
    gid = "rcp_r1"

    def _fake_resolve(rid: str, **kwargs):
        return (
            {
                "id": rid,
                "title": "Pork bao",
                "recipe": {"title": "Pork bao", "servings": {"quantity": 4}},
            },
            {
                "shop_groups": [
                    {
                        "shop": {"id": "s1"},
                        "items": [
                            {
                                "ingredient": "pork",
                                "quantity_g": 150,
                                "pack_grams": 400,
                                "product": {"id": "pork-belly-400", "price_eur": 8.4, "price_minor": 840},
                            },
                            {
                                "ingredient": "soy",
                                "quantity_g": 30,
                                "pack_grams": 500,
                                "product": {"id": "soy-500", "price_eur": 4.2, "price_minor": 420},
                            },
                        ],
                    }
                ],
            },
        )

    monkeypatch.setattr(recipes_tools, "_resolve_match_for", _fake_resolve)

    out = recipes_tools.replace_recipe_group(
        c["id"],
        gid,
        "r1",
        8,
        cart_key="k-rep",
    )
    assert out.get("ok") is True
    assert out["portions"] == 8
    full_after = cart._cart_with_lines(c["id"])  # noqa: SLF001
    assert len(full_after["line_items"]) == 2
    meta = (full_after["line_items"][0].get("group_metadata") or {})
    assert meta.get("serves") == 8


def test_search_recipes_uses_real_catalog(fake_sb):
    """Smoke-test that search_recipes is wired to recipe_catalog without raising."""
    out = recipes_tools.search_recipes("noodle", limit=5)
    assert "results" in out
    assert isinstance(out["results"], list)
    # Each result must carry the public-shape fields the model expects.
    for row in out["results"]:
        assert "id" in row
        assert "title" in row


def test_search_recipes_empty_query_returns_no_results(fake_sb):
    out = recipes_tools.search_recipes("", limit=5)
    assert out["results"] == []


def test_get_recipe_unknown_id_returns_not_found(fake_sb):
    out = recipes_tools.get_recipe("rcp_does_not_exist_xyz")
    assert out.get("ok") is False
    assert out.get("reason") == "not_found"
