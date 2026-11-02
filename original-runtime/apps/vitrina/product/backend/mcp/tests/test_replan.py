"""Unit tests for ``mcp/replan.py``.

Covers:

* ``find_alternatives`` filters by radius + open state.
* ``propose_consolidation`` scores ``consolidate_all`` above a split
  plan when one shop covers everything.
* ``ask_user_constraints`` returns the right widget kind + answer
  shape.
* ``propose_recipe_swap`` stub returns ≥ 2 options.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

import replan  # type: ignore[import-not-found]
import shop_hours  # type: ignore[import-not-found]


# ── inline supabase fake (duplicated from test_shop_hours for isolation) ──


class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._single = False
        self._filters: list[tuple[str, Any]] = []
        self._in: tuple[str, list[Any]] | None = None

    def select(self, _cols: str = "*") -> "_FakeQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append((col, val))
        return self

    def in_(self, col: str, vals: Any) -> "_FakeQuery":
        self._in = (col, list(vals))
        return self

    def single(self) -> "_FakeQuery":
        self._single = True
        return self

    def execute(self) -> Any:
        rows = self._rows
        for col, val in self._filters:
            rows = [r for r in rows if r.get(col) == val]
        if self._in is not None:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in set(vals)]
        if self._single:
            return rows[0] if rows else None
        return rows


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(list(self._tables.get(name, [])))


@pytest.fixture
def fake_sb(monkeypatch: pytest.MonkeyPatch):
    def _install(tables: dict[str, list[dict[str, Any]]]) -> _FakeSupabase:
        sb = _FakeSupabase(tables)
        monkeypatch.setattr(replan, "get_supabase", lambda: sb)
        monkeypatch.setattr(shop_hours, "get_supabase", lambda: sb)
        return sb
    return _install


# ── find_alternatives ────────────────────────────────────────────────


def test_find_alternatives_filters_by_radius_and_openness(fake_sb) -> None:
    # Origin at (0, 0). Shop A is 1 km east + open. Shop B is 20 km +
    # open (out of 5 km radius). Shop C is 2 km + closed.
    fake_sb({
        "shops": [
            {
                "id": "shop-a", "name": "Near Open",
                "timezone": "UTC",
                "opening_hours": {"mon": [["00:00", "23:59"]]},
                "lat": 0.0, "lng": 0.00898,  # ~1 km east at equator
            },
            {
                "id": "shop-b", "name": "Far Open",
                "timezone": "UTC",
                "opening_hours": {"mon": [["00:00", "23:59"]]},
                "lat": 0.0, "lng": 0.1797,  # ~20 km east
            },
            {
                "id": "shop-c", "name": "Near Closed",
                "timezone": "UTC",
                "opening_hours": {"mon": []},
                "lat": 0.0, "lng": 0.01796,  # ~2 km east
            },
        ],
        "products": [
            {"id": "P1", "shop_id": "shop-a", "name": "P1", "stock": 5, "price_minor": 500},
            {"id": "P1", "shop_id": "shop-b", "name": "P1", "stock": 5, "price_minor": 500},
            {"id": "P1", "shop_id": "shop-c", "name": "P1", "stock": 5, "price_minor": 500},
        ],
    })
    # 2026-04-20 Monday, 12:00 UTC.
    at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    out = replan.find_alternatives(
        closed_shop_id="shop-origin",
        items=[{"product_id": "P1", "quantity": 1}],
        location={"lat": 0.0, "lng": 0.0},
        radius_km=5.0,
        at_ts=at,
    )
    assert len(out) == 1
    assert out[0]["shop_id"] == "shop-a"


# ── propose_consolidation ────────────────────────────────────────────


def test_consolidate_all_beats_split_when_single_shop_covers_everything() -> None:
    candidates = [
        {
            "shop_id": "super", "shop_name": "Super", "distance_m": 800.0,
            "items_available": [
                {"product_id": "P1", "in_stock": 5, "price_minor": 500},
                {"product_id": "P2", "in_stock": 5, "price_minor": 500},
                {"product_id": "P3", "in_stock": 5, "price_minor": 500},
            ],
            "items_missing": [],
        },
        {
            "shop_id": "tiny-a", "shop_name": "Tiny A", "distance_m": 200.0,
            "items_available": [
                {"product_id": "P1", "in_stock": 5, "price_minor": 500}
            ],
            "items_missing": ["P2", "P3"],
        },
        {
            "shop_id": "tiny-b", "shop_name": "Tiny B", "distance_m": 300.0,
            "items_available": [
                {"product_id": "P2", "in_stock": 5, "price_minor": 500}
            ],
            "items_missing": ["P1", "P3"],
        },
    ]
    options = replan.propose_consolidation(active_cart_ids=[], candidate_shops=candidates)
    assert options, "expected at least one option"
    top = options[0]
    assert top["kind"] == "consolidate_all"
    assert top["shop_ids"] == ["super"]
    assert top["completeness"] == 1.0


# ── ask_user_constraints ─────────────────────────────────────────────


def test_ask_user_constraints_distance() -> None:
    result = replan.ask_user_constraints(["distance"])
    assert result["widget"] == "clarify_question"
    payload = result["payload"]
    assert payload["kind"] == "distance"
    assert payload["answer_shape"]["type"] == "number"
    assert payload["answer_shape"]["unit"] == "km"


def test_ask_user_constraints_unknown_falls_back_to_distance() -> None:
    result = replan.ask_user_constraints(["unknown-key"])
    assert result["payload"]["kind"] == "distance"


# ── propose_recipe_swap ──────────────────────────────────────────────


def test_propose_recipe_swap_returns_multiple_options(fake_sb) -> None:
    fake_sb({
        "cart_line_items": [
            {
                "group_id": "grp-shopska-1",
                "group_metadata": {"name": "Shopska salad"},
                "product_id": "X",
                "quantity": 1,
            }
        ],
    })
    out = replan.propose_recipe_swap("grp-shopska-1", reason="Shop closed", at_ts=None)
    assert len(out) >= 2
    assert all("items" in alt and "name" in alt for alt in out)


def test_propose_recipe_swap_default_when_no_match(fake_sb) -> None:
    fake_sb({"cart_line_items": []})
    out = replan.propose_recipe_swap("grp-unknown-xyz", reason="test")
    assert len(out) >= 2
