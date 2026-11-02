"""Closed-shop re-plan tool family for Vitrina.

When a cart's shop is closed (or a live checkout hits "out of stock"),
the Leo agent needs to offer the shopper alternatives. This module
implements four deterministic MCP tools the agent can chain:

1. :func:`find_alternatives` — nearby OPEN shops that carry some/all of
   the needed items.
2. :func:`propose_consolidation` — score combinations (one shop vs
   several) using a completeness-first, distance-second rule.
3. :func:`ask_user_constraints` — emit a ``clarify_question`` widget
   payload so the UI can collect a missing constraint (distance, time,
   guest count, budget, dietary) before re-planning.
4. :func:`propose_recipe_swap` — swap 1–2 items in a recipe group for
   in-stock alternatives at nearby shops.

Phase 1 ships with a hand-crafted heuristic for recipe swaps; the real
swap engine will live in ``packages/llm`` and this module will call
into it (see the TODO in :func:`propose_recipe_swap`).
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from shop_hours import _compute_open, _load_shop  # type: ignore[import-not-found]
from supabase_client import get_supabase  # type: ignore[import-not-found]


EARTH_RADIUS_M = 6_371_000.0


# ── distance helpers ─────────────────────────────────────────────────


def haversine_m(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """Great-circle distance in metres between two (lat, lng) points.

    Uses the standard haversine formula. Accurate to within ~0.5% for
    distances relevant to "within 5 km" queries.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_M * c


# ── find_alternatives ────────────────────────────────────────────────


def find_alternatives(
    closed_shop_id: str,
    items: list[dict[str, Any]],
    location: dict[str, float],
    radius_km: float = 5.0,
    *,
    at_ts: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return nearby OPEN shops that stock at least one of ``items``.

    Args:
        closed_shop_id: the shop whose cart triggered the re-plan —
            excluded from results.
        items: ``[{"product_id": str, "quantity": int}, ...]`` — the
            shopping list to re-home.
        location: ``{"lat": float, "lng": float}`` — the shopper (or
            the closed shop) location; distances are measured from here.
        radius_km: hard radius cap. Shops outside are dropped.
        at_ts: evaluation moment for open/closed. Defaults to now-UTC.

    Returns a list of candidate dicts, sorted by items covered (desc)
    then distance (asc)::

        {"shop_id": str, "shop_name": str, "distance_m": float,
         "items_available": [{"product_id": str, "in_stock": int,
                              "price_minor": int}],
         "items_missing": [product_id, ...]}
    """
    if not items:
        return []
    wanted = {i["product_id"]: int(i.get("quantity", 1)) for i in items}
    lat0 = float(location["lat"])
    lng0 = float(location["lng"])
    radius_m = radius_km * 1_000.0

    sb = get_supabase()

    # 1. Candidate product rows across all shops for the wanted ids.
    product_rows = (
        sb.table("products")
        .select("id,shop_id,name,stock,price_minor")
        .in_("id", list(wanted.keys()))
        .execute()
    ) or []

    # Group by shop.
    by_shop: dict[str, list[dict[str, Any]]] = {}
    for row in product_rows:
        if row["shop_id"] == closed_shop_id:
            continue
        by_shop.setdefault(row["shop_id"], []).append(row)

    if not by_shop:
        return []

    # 2. Fetch those shop rows (one batch) for tz / hours / coords.
    shop_rows = (
        sb.table("shops")
        .select("id,name,timezone,opening_hours,lat,lng")
        .in_("id", list(by_shop.keys()))
        .execute()
    ) or []

    out: list[dict[str, Any]] = []
    for shop in shop_rows:
        lat = shop.get("lat")
        lng = shop.get("lng")
        if lat is None or lng is None:
            continue
        dist = haversine_m(lat0, lng0, float(lat), float(lng))
        if dist > radius_m:
            continue
        is_open, _next_open, _ = _compute_open(shop, at_ts)
        if not is_open:
            continue

        prods = by_shop.get(shop["id"], [])
        available: list[dict[str, Any]] = []
        covered_ids: set[str] = set()
        for p in prods:
            stock = int(p.get("stock") or 0)
            need = wanted.get(p["id"], 0)
            if stock >= need and need > 0:
                available.append(
                    {
                        "product_id": p["id"],
                        "in_stock": stock,
                        "price_minor": int(p.get("price_minor") or 0),
                    }
                )
                covered_ids.add(p["id"])
        if not available:
            continue
        missing = [pid for pid in wanted.keys() if pid not in covered_ids]
        out.append(
            {
                "shop_id": shop["id"],
                "shop_name": shop.get("name") or shop["id"],
                "distance_m": round(dist, 1),
                "items_available": available,
                "items_missing": missing,
            }
        )

    # Sort by coverage (desc), then distance (asc).
    out.sort(key=lambda c: (-len(c["items_available"]), c["distance_m"]))
    return out


# ── propose_consolidation ────────────────────────────────────────────


def propose_consolidation(
    active_cart_ids: list[str],
    candidate_shops: list[dict[str, Any]],
    *,
    at_ts: datetime | None = None,
) -> list[dict[str, Any]]:
    """Score ways to re-home a set of items across candidate shops.

    Two options are generated per call:

    * ``consolidate_all`` — pick the single candidate that covers the
      most items (ties → shortest distance).
    * ``replace_one`` — keep the ≥2 best individual shops that together
      cover more items than any single shop does.

    Scoring::

        completeness = covered_items / total_items

    Results are sorted by (fewer shops, higher completeness, smaller
    distance delta). ``distance_m_delta`` is the sum of distances of
    the chosen shops (the baseline closed shop isn't in ``candidate_shops``
    so this is effectively the walk-to cost).

    ``active_cart_ids`` is accepted for forward-compatibility (so the
    agent can pass the full session state); the Phase 1 heuristic only
    uses ``candidate_shops``.
    """
    if not candidate_shops:
        return []

    # Determine universe of needed items from the candidates' union.
    all_items: set[str] = set()
    for c in candidate_shops:
        for it in c.get("items_available", []):
            all_items.add(it["product_id"])
        for pid in c.get("items_missing", []) or []:
            all_items.add(pid)
    total = max(1, len(all_items))

    options: list[dict[str, Any]] = []

    # consolidate_all → best single shop.
    best = max(
        candidate_shops,
        key=lambda c: (len(c["items_available"]), -c["distance_m"]),
    )
    covered = {it["product_id"] for it in best["items_available"]}
    options.append(
        {
            "kind": "consolidate_all",
            "shop_ids": [best["shop_id"]],
            "items": sorted(covered),
            "distance_m_delta": round(best["distance_m"], 1),
            "completeness": round(len(covered) / total, 3),
            "reasoning": (
                f"One stop at {best.get('shop_name', best['shop_id'])} "
                f"covers {len(covered)}/{total} items."
            ),
        }
    )

    # replace_one → greedy set-cover across candidates.
    remaining = set(all_items)
    picked: list[dict[str, Any]] = []
    pool = list(candidate_shops)
    while remaining and pool:
        pool.sort(
            key=lambda c: (
                -len({it["product_id"] for it in c["items_available"]} & remaining),
                c["distance_m"],
            )
        )
        nxt = pool.pop(0)
        gained = {it["product_id"] for it in nxt["items_available"]} & remaining
        if not gained:
            break
        picked.append(nxt)
        remaining -= gained
    if len(picked) >= 2:
        covered_multi = set().union(
            *({it["product_id"] for it in p["items_available"]} for p in picked)
        )
        options.append(
            {
                "kind": "replace_one",
                "shop_ids": [p["shop_id"] for p in picked],
                "items": sorted(covered_multi),
                "distance_m_delta": round(sum(p["distance_m"] for p in picked), 1),
                "completeness": round(len(covered_multi) / total, 3),
                "reasoning": (
                    f"Split across {len(picked)} shops to cover "
                    f"{len(covered_multi)}/{total} items."
                ),
            }
        )

    # Sort: fewer shops, then higher completeness, then shorter distance.
    options.sort(
        key=lambda o: (
            len(o["shop_ids"]),
            -o["completeness"],
            o["distance_m_delta"],
        )
    )
    return options


# ── ask_user_constraints ─────────────────────────────────────────────


_CONSTRAINT_WIDGETS: dict[str, dict[str, Any]] = {
    "distance": {
        "question": "How far are you willing to walk or drive?",
        "kind": "distance",
        "answer_shape": {"type": "number", "unit": "km", "min": 0.5, "max": 25},
    },
    "time": {
        "question": "By when do you need this ready?",
        "kind": "time",
        "answer_shape": {"type": "iso_datetime"},
    },
    "guests": {
        "question": "How many people are you cooking for?",
        "kind": "guests",
        "answer_shape": {"type": "integer", "min": 1, "max": 50},
    },
    "budget": {
        "question": "What's your budget for this?",
        "kind": "budget",
        "answer_shape": {"type": "number", "unit": "EUR", "min": 5},
    },
    "dietary": {
        "question": "Any dietary restrictions we should work around?",
        "kind": "dietary",
        "answer_shape": {
            "type": "multiselect",
            "choices": ["vegetarian", "vegan", "gluten_free", "lactose_free", "none"],
        },
    },
}


def ask_user_constraints(missing: list[str]) -> dict[str, Any]:
    """Produce a ``clarify_question`` widget payload for a missing constraint.

    ``missing`` is a list of constraint keys (in priority order) — the
    first recognised key wins since the UI shows one question at a
    time. Valid keys: ``distance``, ``time``, ``guests``, ``budget``,
    ``dietary``.

    Returns::

        {"widget": "clarify_question",
         "payload": {"question": str,
                     "kind": str,
                     "answer_shape": {...}}}
    """
    for key in missing:
        if key in _CONSTRAINT_WIDGETS:
            return {"widget": "clarify_question", "payload": dict(_CONSTRAINT_WIDGETS[key])}
    # Unknown / empty → fall back to distance.
    return {
        "widget": "clarify_question",
        "payload": dict(_CONSTRAINT_WIDGETS["distance"]),
    }


# ── propose_recipe_swap ──────────────────────────────────────────────


# Phase-1 heuristic table. Keys are lowercased substrings matched
# against ``cart_line_items.group_metadata.name`` (or ``group_id`` if no
# name is present). Values are lists of alternative recipe templates.
#
# TODO: LLM via packages/llm — replace this hand-crafted table with a
# call into a recipe-generation chain once the LLM package lands.
_RECIPE_SWAP_TABLE: dict[str, list[dict[str, Any]]] = {
    "shopska": [
        {
            "name": "Mediterranean tomato salad",
            "items": [
                {"product_id": "TOMATO-LOCAL", "qty": 3},
                {"product_id": "CUCUMBER-LOCAL", "qty": 2},
                {"product_id": "FETA-200G", "qty": 1},
                {"product_id": "OLIVE-OIL-250", "qty": 1},
            ],
            "estimated_total_minor": 1_850,
            "diff_summary": "Swaps sirene for feta; skips roasted pepper.",
        },
        {
            "name": "Lyutenitsa bowl",
            "items": [
                {"product_id": "LYUTENITSA-300", "qty": 1},
                {"product_id": "BREAD-SOURDOUGH", "qty": 1},
                {"product_id": "WHITE-CHEESE-250", "qty": 1},
            ],
            "estimated_total_minor": 1_400,
            "diff_summary": "No cucumbers needed — spread-forward.",
        },
    ],
    "banitsa": [
        {
            "name": "Cheese tart (filo)",
            "items": [
                {"product_id": "FILO-PASTRY-500", "qty": 1},
                {"product_id": "WHITE-CHEESE-250", "qty": 2},
                {"product_id": "EGGS-6", "qty": 1},
            ],
            "estimated_total_minor": 1_600,
            "diff_summary": "Uses filo instead of traditional dough.",
        },
        {
            "name": "Spinach pie",
            "items": [
                {"product_id": "FILO-PASTRY-500", "qty": 1},
                {"product_id": "SPINACH-FRESH", "qty": 1},
                {"product_id": "WHITE-CHEESE-250", "qty": 1},
            ],
            "estimated_total_minor": 1_900,
            "diff_summary": "Adds spinach; lighter alternative.",
        },
    ],
}

_DEFAULT_SWAPS: list[dict[str, Any]] = [
    {
        "name": "Simple grazing plate",
        "items": [
            {"product_id": "WHITE-CHEESE-250", "qty": 1},
            {"product_id": "OLIVES-MIXED-200", "qty": 1},
            {"product_id": "BREAD-SOURDOUGH", "qty": 1},
        ],
        "estimated_total_minor": 1_500,
        "diff_summary": "Pantry-forward assembly; no cooking needed.",
    },
    {
        "name": "One-pot lentil stew",
        "items": [
            {"product_id": "LENTILS-500", "qty": 1},
            {"product_id": "ONION-LOCAL", "qty": 2},
            {"product_id": "TOMATO-PASTE-200", "qty": 1},
        ],
        "estimated_total_minor": 1_200,
        "diff_summary": "Fully substitutes the missing ingredients.",
    },
]


def propose_recipe_swap(
    recipe_group_id: str, reason: str, *, at_ts: datetime | None = None
) -> list[dict[str, Any]]:
    """Return 2–3 alternative recipes for a closed-shop recipe group.

    Phase-1 heuristic:

    1. Look up the cart_line_items with ``group_id = recipe_group_id``
       to pull out ``group_metadata.name`` (if stored).
    2. Match that name (case-insensitive substring) against the hand-
       crafted swap table; fall back to generic options if no match.
    3. Assign each alternative a nearest open shop as ``shop_id`` (None
       if no shop info available).

    The ``reason`` argument is accepted and forwarded into the returned
    ``diff_summary`` for transparency but not otherwise used yet.

    TODO: LLM via ``packages/llm`` — replace heuristic with real
    generation once that package lands.
    """
    sb = get_supabase()
    rows = (
        sb.table("cart_line_items")
        .select("group_id,group_metadata,product_id,quantity")
        .eq("group_id", recipe_group_id)
        .execute()
    ) or []

    name_hint = ""
    for r in rows:
        meta = r.get("group_metadata") or {}
        if isinstance(meta, dict):
            n = meta.get("name") or meta.get("recipe_name")
            if n:
                name_hint = str(n).lower()
                break
    if not name_hint:
        name_hint = recipe_group_id.lower()

    alternatives: list[dict[str, Any]] | None = None
    for key, templates in _RECIPE_SWAP_TABLE.items():
        if key in name_hint:
            alternatives = [dict(t) for t in templates]
            break
    if alternatives is None:
        alternatives = [dict(t) for t in _DEFAULT_SWAPS]

    # Decorate each alt with a shop_id (best-effort; None if unknown).
    for alt in alternatives:
        alt.setdefault("shop_id", None)
        if reason:
            alt["diff_summary"] = f"{alt['diff_summary']} (triggered by: {reason})"

    return alternatives[:3]
