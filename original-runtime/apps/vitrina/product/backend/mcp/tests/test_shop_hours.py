"""Unit tests for ``mcp/shop_hours.py``.

Covers:

* Simple open / closed checks against a single range.
* Split shifts (morning + afternoon) and between-shift CLOSED windows.
* Boundary semantics — open inclusive, close exclusive.
* DST handling via ``zoneinfo`` (evaluated at a known Europe/Sofia
  transition moment).
* ``validate_cart_shops_open`` over mixed open + closed carts.

The supabase client seam is monkeypatched per-test with a tiny inline
fake so no real HTTP is issued. See ``fixtures/supabase_mock.py`` (A4)
for the project-wide mock if you need a richer one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

import shop_hours  # type: ignore[import-not-found]


# ── inline supabase fake ─────────────────────────────────────────────


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
        monkeypatch.setattr(shop_hours, "get_supabase", lambda: sb)
        return sb
    return _install


# ── shop fixtures ────────────────────────────────────────────────────


def _shop_sofia_mon_fri(hours: dict[str, list[list[str]]]) -> dict[str, Any]:
    return {
        "id": "shop-sofia",
        "name": "Sofia Demo",
        "timezone": "Europe/Sofia",
        "opening_hours": hours,
        "lat": 42.6977,
        "lng": 23.3219,
    }


# ── basic open/closed ────────────────────────────────────────────────


def test_open_at_1000_on_monday_with_0900_to_2000(fake_sb) -> None:
    fake_sb({
        "shops": [_shop_sofia_mon_fri({"mon": [["09:00", "20:00"]]})],
    })
    # 2026-04-20 is a Monday. 10:00 Europe/Sofia = 07:00 UTC (summer).
    at = datetime(2026, 4, 20, 7, 0, tzinfo=timezone.utc)
    result = shop_hours.is_shop_open_now("shop-sofia", at_ts=at)
    assert result["open"] is True
    assert result["next_open_at"] is None


def test_closed_on_sunday_2200_next_open_monday_0900(fake_sb) -> None:
    fake_sb({
        "shops": [
            _shop_sofia_mon_fri({
                "mon": [["09:00", "20:00"]],
                "tue": [["09:00", "20:00"]],
                "wed": [["09:00", "20:00"]],
                "thu": [["09:00", "20:00"]],
                "fri": [["09:00", "20:00"]],
                "sat": [],
                "sun": [],
            })
        ],
    })
    # 2026-04-19 Sunday 22:00 Europe/Sofia = 19:00 UTC (summer).
    at = datetime(2026, 4, 19, 19, 0, tzinfo=timezone.utc)
    result = shop_hours.is_shop_open_now("shop-sofia", at_ts=at)
    assert result["open"] is False
    # Next open ≈ Mon 2026-04-20 09:00 Sofia = 06:00 UTC.
    assert result["next_open_at"] == "2026-04-20T06:00:00Z"


# ── split shifts ─────────────────────────────────────────────────────


def test_split_shift_between_ranges_is_closed(fake_sb) -> None:
    fake_sb({
        "shops": [
            _shop_sofia_mon_fri({
                "mon": [["09:00", "13:00"], ["16:00", "20:00"]],
            })
        ],
    })
    # 2026-04-20 Monday 14:30 Europe/Sofia = 11:30 UTC.
    at = datetime(2026, 4, 20, 11, 30, tzinfo=timezone.utc)
    result = shop_hours.is_shop_open_now("shop-sofia", at_ts=at)
    assert result["open"] is False
    # Next open = same day 16:00 Sofia = 13:00 UTC.
    assert result["next_open_at"] == "2026-04-20T13:00:00Z"


# ── boundary semantics ───────────────────────────────────────────────


def test_exactly_at_open_time_is_open(fake_sb) -> None:
    fake_sb({
        "shops": [_shop_sofia_mon_fri({"mon": [["09:00", "20:00"]]})],
    })
    # 09:00 Sofia = 06:00 UTC.
    at = datetime(2026, 4, 20, 6, 0, tzinfo=timezone.utc)
    assert shop_hours.is_shop_open_now("shop-sofia", at_ts=at)["open"] is True


def test_exactly_at_close_time_is_closed(fake_sb) -> None:
    fake_sb({
        "shops": [_shop_sofia_mon_fri({"mon": [["09:00", "20:00"]]})],
    })
    # 20:00 Sofia = 17:00 UTC.
    at = datetime(2026, 4, 20, 17, 0, tzinfo=timezone.utc)
    assert shop_hours.is_shop_open_now("shop-sofia", at_ts=at)["open"] is False


# ── DST correctness ─────────────────────────────────────────────────


def test_dst_transition_europe_sofia(fake_sb) -> None:
    """Sofia switched to EEST (UTC+3) on 2026-03-29 at 03:00 local.

    At 08:30 UTC on 2026-03-30 (Monday), Sofia local time is 11:30
    (summer-time). With hours 09:00–20:00 the shop must be OPEN — if
    we'd used a fixed +02:00 offset we'd still compute 10:30, still
    open, so pick a boundary instead: 06:30 UTC → Sofia 09:30 summer
    (OPEN) but Sofia 08:30 winter (CLOSED). The summer calc is the
    correct one; this locks DST handling in.
    """
    fake_sb({
        "shops": [_shop_sofia_mon_fri({"mon": [["09:00", "20:00"]]})],
    })
    at = datetime(2026, 3, 30, 6, 30, tzinfo=timezone.utc)
    result = shop_hours.is_shop_open_now("shop-sofia", at_ts=at)
    assert result["open"] is True


# ── validate_cart_shops_open ─────────────────────────────────────────


def test_validate_cart_shops_open_mixed(fake_sb) -> None:
    fake_sb({
        "shops": [
            {
                "id": "open-shop",
                "name": "Open",
                "timezone": "Europe/Sofia",
                "opening_hours": {"mon": [["09:00", "20:00"]]},
                "lat": 0.0,
                "lng": 0.0,
            },
            {
                "id": "closed-shop",
                "name": "Closed",
                "timezone": "Europe/Sofia",
                "opening_hours": {"mon": []},
                "lat": 0.0,
                "lng": 0.0,
            },
        ],
        "carts": [
            {"id": "cart-a", "shop_id": "open-shop"},
            {"id": "cart-b", "shop_id": "closed-shop"},
        ],
    })
    at = datetime(2026, 4, 20, 7, 0, tzinfo=timezone.utc)  # Mon 10:00 Sofia
    results = shop_hours.validate_cart_shops_open(["cart-a", "cart-b"], at_ts=at)
    by_id = {r["cart_id"]: r for r in results}
    assert by_id["cart-a"]["open"] is True
    assert by_id["cart-b"]["open"] is False
