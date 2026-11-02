"""Shop-hours validation for Vitrina.

Given a shop row in Supabase (``shops`` table) with an IANA ``timezone``
column and an ``opening_hours`` JSON column of shape::

    {"mon": [["09:00", "20:00"]],
     "tue": [["09:00", "13:00"], ["16:00", "20:00"]],   # split shift
     ...
     "sun": []}                                          # closed

…compute whether a shop is currently open (or open at an arbitrary
``at_ts``), and if closed, find the **next** moment it opens by walking
up to 14 days ahead. DST transitions are handled by ``zoneinfo`` — we
build a timezone-aware local ``datetime`` for each candidate boundary
instead of doing manual offset math.

Used by both the replan tool family (``replan.py``) and the checkout
pre-flight (another subagent's module) to short-circuit carts whose
shops are already closed.

Boundary semantics (locked so tests + API + UI agree):

* ``time == open_time``  → OPEN   (inclusive lower bound)
* ``time == close_time`` → CLOSED (exclusive upper bound)
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from supabase_client import get_supabase  # type: ignore[import-not-found]


_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── helpers ──────────────────────────────────────────────────────────


def _parse_hhmm(s: str) -> time:
    """Parse ``"HH:MM"`` into a naive :class:`datetime.time`.

    Accepts ``"24:00"`` (a common ISO-style end-of-day marker used in the
    seed migrations) by clamping it to ``23:59`` — Python's
    :class:`datetime.time` rejects hour=24, but treating the close edge
    as one minute before midnight is the desired "open until end of day"
    semantic for our open/closed checks.
    """
    hh_s, mm_s = s.split(":")
    hh, mm = int(hh_s), int(mm_s)
    if hh == 24 and mm == 0:
        return time(23, 59)
    return time(hh, mm)


def _day_key(dt_local: datetime) -> str:
    """Return the lowercase 3-letter day-of-week key for a local datetime."""
    return _DAY_KEYS[dt_local.weekday()]


def _iter_ranges(
    opening_hours: dict[str, list[list[str]]], day: str
) -> Iterable[tuple[time, time]]:
    """Yield ``(open, close)`` ``time`` tuples for the given day key.

    Empty list or missing key → no ranges (closed that day).
    """
    for rng in opening_hours.get(day, []) or []:
        if not rng or len(rng) != 2:
            continue
        yield _parse_hhmm(rng[0]), _parse_hhmm(rng[1])


def _to_utc_iso(dt: datetime) -> str:
    """Format an aware datetime as a UTC ISO-8601 string with trailing Z."""
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_shop(shop_id: str) -> dict[str, Any]:
    """Fetch a single shop row. Raises if not found."""
    sb = get_supabase()
    row = (
        sb.table("shops")
        .select("id,name,timezone,opening_hours,lat,lng")
        .eq("id", shop_id)
        .single()
        .execute()
    )
    if not row:
        raise ValueError(f"shop_not_found: {shop_id}")
    return row


def _compute_open(
    shop: dict[str, Any], at_ts: datetime | None
) -> tuple[bool, str | None, datetime]:
    """Core logic: given a shop row + moment, return (open, next_open_iso, local_dt).

    ``at_ts`` defaults to now-UTC. ``next_open_iso`` is ``None`` when the
    shop is open. Looks forward up to 14 days when searching.
    """
    tz_name = shop.get("timezone") or "UTC"
    tz = ZoneInfo(tz_name)
    now_utc = at_ts or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_local = now_utc.astimezone(tz)
    hours = shop.get("opening_hours") or {}

    # Is it open right now?
    for open_t, close_t in _iter_ranges(hours, _day_key(now_local)):
        # Boundary: open inclusive, close exclusive.
        if open_t <= now_local.time() < close_t:
            return True, None, now_local

    # Closed — find the next opening within 14 days.
    for offset in range(0, 15):
        day_local_date = (now_local + timedelta(days=offset)).date()
        day_key = _DAY_KEYS[day_local_date.weekday()]
        # Ranges are stored in chronological order; walk them.
        ranges = sorted(_iter_ranges(hours, day_key), key=lambda r: r[0])
        for open_t, _close_t in ranges:
            candidate_local = datetime.combine(day_local_date, open_t, tzinfo=tz)
            if candidate_local > now_local:
                return False, _to_utc_iso(candidate_local), now_local

    return False, None, now_local


# ── public API ───────────────────────────────────────────────────────


def is_shop_open_now(
    shop_id: str, *, at_ts: datetime | None = None
) -> dict[str, Any]:
    """Return an open/closed snapshot for a single shop.

    Output shape::

        {"shop_id": str,
         "open": bool,
         "next_open_at": str | None,   # UTC ISO-8601 with trailing Z
         "local_time": str}            # the evaluated moment in shop tz

    ``at_ts`` lets callers (and tests) evaluate at a fixed moment; if
    omitted, ``datetime.now(utc)`` is used. Any naive datetime passed is
    treated as UTC.
    """
    shop = _load_shop(shop_id)
    is_open, next_open_iso, local_dt = _compute_open(shop, at_ts)
    return {
        "shop_id": shop_id,
        "open": is_open,
        "next_open_at": next_open_iso,
        "local_time": local_dt.replace(microsecond=0).isoformat(),
    }


def validate_cart_shops_open(
    cart_ids: list[str], *, at_ts: datetime | None = None
) -> list[dict[str, Any]]:
    """For each cart id, return its shop's open/closed status.

    Output shape per entry::

        {"cart_id": str, "shop_id": str,
         "open": bool, "next_open_at": str | None}

    Unknown cart ids are skipped with a sentinel entry carrying
    ``open=False`` and ``shop_id=None`` so callers can surface the error
    without the whole batch failing.
    """
    if not cart_ids:
        return []
    sb = get_supabase()
    carts = (
        sb.table("carts")
        .select("id,shop_id")
        .in_("id", cart_ids)
        .execute()
    ) or []
    # Cache shops so a batch of N carts at the same shop is one fetch.
    shop_cache: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    # Preserve input order.
    by_id = {c["id"]: c for c in carts}
    for cart_id in cart_ids:
        cart = by_id.get(cart_id)
        if not cart:
            out.append(
                {"cart_id": cart_id, "shop_id": None, "open": False, "next_open_at": None}
            )
            continue
        shop_id = cart["shop_id"]
        shop = shop_cache.get(shop_id)
        if shop is None:
            try:
                shop = _load_shop(shop_id)
            except ValueError:
                out.append(
                    {"cart_id": cart_id, "shop_id": shop_id, "open": False, "next_open_at": None}
                )
                continue
            shop_cache[shop_id] = shop
        is_open, next_open_iso, _ = _compute_open(shop, at_ts)
        out.append(
            {
                "cart_id": cart_id,
                "shop_id": shop_id,
                "open": is_open,
                "next_open_at": next_open_iso,
            }
        )
    return out


# ── window-based validation (next-day standard pickup) ────────────────


def _is_shop_open_during_window(
    shop: dict[str, Any], window_start_utc: datetime, window_end_utc: datetime
) -> bool:
    """True iff the shop has at least one opening range that covers any
    part of the [window_start, window_end] interval.

    The pickup grace is one minute — if the shop opens exactly at the
    window's end we treat it as not covering the window, matching the
    boundary semantics in :func:`_compute_open`.
    """
    if window_end_utc <= window_start_utc:
        return False
    tz_name = shop.get("timezone") or "UTC"
    tz = ZoneInfo(tz_name)
    hours = shop.get("opening_hours") or {}
    start_local = window_start_utc.astimezone(tz)
    end_local = window_end_utc.astimezone(tz)

    # The window may span midnight; iterate every local date it touches.
    cursor = start_local.date()
    last = end_local.date()
    while cursor <= last:
        day_key = _DAY_KEYS[cursor.weekday()]
        for open_t, close_t in _iter_ranges(hours, day_key):
            range_start = datetime.combine(cursor, open_t, tzinfo=tz)
            range_end = datetime.combine(cursor, close_t, tzinfo=tz)
            # Overlap test (open inclusive on both sides for a window —
            # we just need *some* shared minute).
            if range_end > start_local and range_start < end_local:
                return True
        cursor = cursor + timedelta(days=1)
    return False


def validate_cart_shops_open_during_window(
    cart_ids: list[str],
    window_start: datetime | str,
    window_end: datetime | str,
) -> list[dict[str, Any]]:
    """For each cart id, return whether its shop is open during the
    booked pickup window.

    Used by the next-day / scheduled checkout path so that a shop being
    closed *now* doesn't block a checkout for an order the user is
    picking up tomorrow morning. Boundary semantics match
    :func:`validate_cart_shops_open`.

    ``window_start`` / ``window_end`` accept either ``datetime`` or an
    ISO-8601 string (with or without trailing ``Z``).
    """
    if not cart_ids:
        return []

    def _coerce(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        s = value.strip().replace("Z", "+00:00") if value else ""
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    start_utc = _coerce(window_start).astimezone(timezone.utc)
    end_utc = _coerce(window_end).astimezone(timezone.utc)

    sb = get_supabase()
    carts = (
        sb.table("carts")
        .select("id,shop_id")
        .in_("id", cart_ids)
        .execute()
    ) or []
    shop_cache: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    by_id = {c["id"]: c for c in carts}
    for cart_id in cart_ids:
        cart = by_id.get(cart_id)
        if not cart:
            out.append(
                {
                    "cart_id": cart_id,
                    "shop_id": None,
                    "open": False,
                    "window_start": _to_utc_iso(start_utc),
                    "window_end": _to_utc_iso(end_utc),
                }
            )
            continue
        shop_id = cart["shop_id"]
        shop = shop_cache.get(shop_id)
        if shop is None:
            try:
                shop = _load_shop(shop_id)
            except ValueError:
                out.append(
                    {
                        "cart_id": cart_id,
                        "shop_id": shop_id,
                        "open": False,
                        "window_start": _to_utc_iso(start_utc),
                        "window_end": _to_utc_iso(end_utc),
                    }
                )
                continue
            shop_cache[shop_id] = shop
        covers = _is_shop_open_during_window(shop, start_utc, end_utc)
        # When the shop is closed during the booked window, it's useful
        # for the UI to surface when the shop *would* next be open so
        # the user can pick a different slot rather than guess.
        next_open: str | None = None
        if not covers:
            _, next_open, _ = _compute_open(shop, start_utc)
        out.append(
            {
                "cart_id": cart_id,
                "shop_id": shop_id,
                "open": covers,
                "next_open_at": next_open,
                "window_start": _to_utc_iso(start_utc),
                "window_end": _to_utc_iso(end_utc),
            }
        )
    return out
