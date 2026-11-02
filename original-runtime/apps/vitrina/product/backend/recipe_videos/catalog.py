"""Preprocessed creator-video catalog for Vitrina Discover.

The catalog file is treated as a *persistent cache*: every recipe we've ever
extracted stays here. Discover surfaces a fresh randomised slice each app-load
via a session-scoped ``seed`` (deterministic for stable pagination, fresh per
new session)."""

from __future__ import annotations

import json
import os
import random
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "creator_catalog.json"
SEED_DIR = HERE.parent / "mcp" / "seed"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import shop_matcher  # noqa: E402  (sibling module)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return stripped.lower()


_SKUISH_RE = re.compile(r"\s*\((?:[A-Z]{1,6}[-_][A-Z0-9][A-Z0-9._-]*|SKU[:\s-]*[^)]*)\)\s*")
_INTERNAL_PHRASES = (
    "customer should confirm",
    "customer should substitute",
    "confirm or substitute",
    "ask customer",
    "not customer facing",
)


def _customer_text(value: Any) -> str | None:
    """Strip SKU/internal matcher language before anything reaches the UI."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    lower = text.lower()
    if any(phrase in lower for phrase in _INTERNAL_PHRASES):
        return None
    cleaned = _SKUISH_RE.sub(" ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—")
    return cleaned or None


def _customer_name(value: Any) -> str:
    text = _customer_text(value) or ""
    if not text:
        return ""
    # Preserve deliberate casing for short all-caps words (BBQ, EVOO), but make
    # demo/POS-style lowercase names read like normal customer-facing labels.
    if text == text.lower():
        return text.title()
    return text


@lru_cache(maxsize=1)
def _catalog() -> list[dict[str, Any]]:
    rows = _read_json(CATALOG_PATH)
    return sorted(rows, key=lambda row: int(row.get("sort", 9999)))


def invalidate_cache() -> None:
    """Drop the cached catalog so the next read pulls fresh rows from disk.

    Called after the chat ``extract`` path mutates the catalog file so the
    new row shows up in the next ``GET /recipe-videos`` response without
    needing a process restart.
    """

    _catalog.cache_clear()


@lru_cache(maxsize=1)
def _products() -> dict[str, dict[str, Any]]:
    rows = _read_json(SEED_DIR / "products.json")
    extra = SEED_DIR / "recipe_products.json"
    if extra.exists():
        rows.extend(_read_json(extra))
    return {row["id"]: row for row in rows}


@lru_cache(maxsize=1)
def _shops() -> dict[str, dict[str, Any]]:
    rows = _read_json(SEED_DIR / "shops.json")
    extra = SEED_DIR / "recipe_shops.json"
    if extra.exists():
        rows.extend(_read_json(extra))
    return {row["id"]: row for row in rows}


def _product_card(row: dict[str, Any]) -> dict[str, Any]:
    price_eur = float(row.get("price_eur") or 0)
    raw_name = row.get("name_en") or row.get("name_bg") or row["id"]
    return {
        "id": row["id"],
        "shop_id": row["shop_id"],
        "name": _customer_name(raw_name) or row["id"],
        "description": _customer_text(row.get("description_en") or row.get("description_bg")),
        "category": row.get("category"),
        "unit": row.get("unit"),
        "unit_size": row.get("unit_size"),
        "price_eur": price_eur,
        "price_minor": int(round(price_eur * 100)),
        "image_url": row.get("image"),
    }


_BAD_QUANTITY_STRINGS = {
    "",
    "unspecified",
    "n/a",
    "none",
    "null",
    "tbd",
    "?",
}


def _clean_quantity(value: Any) -> Any:
    """Strip the LLM-hedged "unspecified" placeholder from quantity strings.

    The matcher is now prompted to never emit "unspecified" but on the
    rare miss we replace it with a polite catch-all rather than show it
    in the modal — Peter's review explicitly called this out as ugly.
    """
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _BAD_QUANTITY_STRINGS:
            return "to taste"
    return value


def _shop_card(row: dict[str, Any]) -> dict[str, Any]:
    address = row.get("address") or {}
    return {
        "id": row["id"],
        "name": row.get("name_en") or row.get("name_bg") or row["id"],
        "trade": row.get("trade_en") or row.get("trade"),
        "address": address.get("street_en") or address.get("street_bg") or address.get("city_en"),
    }


def _search_product_ids(query: str, *, max_results: int = 2) -> list[str]:
    q = _normalize(query)
    if not q:
        return []
    generic_herb = any(
        token in q
        for token in ("fresh herbs", "fresh herb", "herbs", "herb", "green herbs")
    )
    scored: list[tuple[int, str]] = []
    for product_id, product in _products().items():
        hay = " ".join(
            [
                product.get("name_en") or "",
                product.get("description_en") or "",
                " ".join(product.get("keywords") or []),
                product.get("category") or "",
                product.get("subcategory") or "",
            ]
        )
        norm = _normalize(hay)
        score = 0
        for token in q.replace("/", " ").replace("-", " ").split():
            if len(token) < 3:
                continue
            if token in norm:
                score += 1
        if generic_herb and product.get("subcategory") == "herb":
            score += 3
        if score:
            scored.append((score, product_id))
    return [product_id for _, product_id in sorted(scored, reverse=True)[:max_results]]


def _ingredient_product_ids(ingredient: dict[str, Any]) -> list[str]:
    ids = [pid for pid in ingredient.get("product_ids", []) if pid in _products()]
    if ids:
        return ids
    return _search_product_ids(str(ingredient.get("description") or ""), max_results=1)


def _stock_groups_from_match(match: dict[str, Any]) -> list[dict[str, Any]]:
    """Render LLM-matched shop groups into the public ``stock_groups`` shape.

    The matcher persists its full payload on the catalog row; the
    Discover/Modal frontend wants a flat ``[ {shop, items[]} ]`` list with
    `quantity` resolved to a string and `product` carrying its EUR price
    + image. We just project the shape here, keeping the substitution +
    confidence fields so the modal can render the italic caption.
    """
    groups: list[dict[str, Any]] = []
    for group in match.get("shop_groups") or []:
        items: list[dict[str, Any]] = []
        for item in group.get("items") or []:
            product = item.get("product") or {}
            product = {
                **product,
                "name": _customer_name(product.get("name")) or product.get("id"),
                "description": _customer_text(product.get("description")),
            }
            items.append(
                {
                    "ingredient": _customer_name(item.get("ingredient")) or item.get("ingredient"),
                    "quantity": _clean_quantity(
                        item.get("quantity_recipe") or item.get("quantity_purchase")
                    ),
                    "quantity_recipe": _clean_quantity(item.get("quantity_recipe")),
                    "quantity_purchase": _clean_quantity(item.get("quantity_purchase")),
                    "quantity_g": item.get("quantity_g"),
                    "pack_grams": item.get("pack_grams"),
                    "product": product,
                    "substitution": _customer_text(item.get("substitution")),
                    "confidence": item.get("confidence"),
                }
            )
        if not items:
            continue
        groups.append(
            {
                "shop": group.get("shop") or {},
                "items": items,
                "subtotal_eur": group.get("subtotal_eur"),
                "subtotal_minor": group.get("subtotal_minor"),
            }
        )
    return groups


def _repair_obvious_gaps(match: dict[str, Any]) -> dict[str, Any]:
    """Resolve cached matcher gaps for generic items that the demo pantry has.

    This is intentionally narrow: common recipe words like "coleslaw" and
    "fresh herbs" should become cabbage/herb substitutions even if an older
    cached LLM response put them in missing/blockers. We only choose existing
    product ids and reuse the same stock-group shape as the matcher.
    """

    gaps = list(match.get("blockers") or []) + list(match.get("missing") or [])
    if not gaps:
        return match
    repaired = dict(match)
    groups = [dict(group) for group in repaired.get("shop_groups") or []]
    repaired_blockers = []
    repaired_missing = []

    def append_item(gap: dict[str, Any]) -> bool:
        ingredient = gap.get("ingredient") or ""
        product_ids = _search_product_ids(ingredient, max_results=1)
        if not product_ids:
            return False
        product = _products().get(product_ids[0])
        if not product:
            return False
        card = _product_card(product)
        price_minor = int(card.get("price_minor") or 0)
        shop = _shop_card(_shops().get(product["shop_id"], {"id": product["shop_id"]}))
        item = {
            "ingredient": ingredient,
            "quantity_recipe": "to taste",
            "quantity_purchase": "1 bunch" if product.get("subcategory") == "herb" else "1 piece",
            "quantity_g": None,
            "pack_grams": None,
            "product": card,
            "substitution": f"{card['name']} works for {ingredient}.",
            "confidence": 0.74,
        }
        for group in groups:
            if (group.get("shop") or {}).get("id") == product["shop_id"]:
                group["items"] = list(group.get("items") or []) + [item]
                if isinstance(group.get("subtotal_minor"), int):
                    group["subtotal_minor"] += price_minor
                    group["subtotal_eur"] = round(group["subtotal_minor"] / 100, 2)
                return True
        groups.append(
            {
                "shop": shop,
                "items": [item],
                "subtotal_eur": round(price_minor / 100, 2),
                "subtotal_minor": price_minor,
            }
        )
        return True

    for gap in match.get("blockers") or []:
        if not append_item(gap):
            repaired_blockers.append(gap)
    for gap in match.get("missing") or []:
        if not append_item(gap):
            repaired_missing.append(gap)

    repaired["shop_groups"] = groups
    repaired["blockers"] = repaired_blockers
    repaired["missing"] = repaired_missing
    if groups and repaired["feasibility"] == "infeasible":
        repaired["feasibility"] = "partial" if repaired_blockers else "full"
    return repaired


def _stock_groups_legacy(video: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback keyword matcher — used only when ``match_universal`` is absent.

    Kept around for catalog rows that pre-date the LLM matcher (or whose
    matcher call failed). Discover prefers the LLM match when both exist.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for ingredient in video.get("recipe", {}).get("ingredients", []):
        for product_id in _ingredient_product_ids(ingredient):
            product = _products().get(product_id)
            if not product:
                continue
            shop_id = product["shop_id"]
            group = grouped.setdefault(
                shop_id,
                {
                    "shop": _shop_card(_shops().get(shop_id, {"id": shop_id})),
                    "items": [],
                },
            )
            if any(item["product"]["id"] == product_id for item in group["items"]):
                continue
            group["items"].append(
                {
                    "ingredient": ingredient.get("description"),
                    "quantity": ingredient.get("quantity"),
                    "product": _product_card(product),
                }
            )
    return [group for group in grouped.values() if group["items"]]


def _resolved_match(
    video: dict[str, Any],
    *,
    user_lat: float | None,
    user_lng: float | None,
    catchment_km: float | None,
) -> dict[str, Any] | None:
    """Filter the persisted ``match_universal`` to the user's catchment.

    Cheap path only — does NOT re-invoke the LLM. The result powers the
    request-time ``feasibility`` decision (drop ``infeasible`` rows from
    Discover, keep them reachable by direct id). The expensive LLM rerun
    on `partial` is deferred to the per-recipe modal open or to a
    background job, so listing 20 recipes never burns 20 LLM calls.
    """
    universal = video.get("match_universal")
    if not universal:
        return None
    if user_lat is None or user_lng is None or catchment_km is None:
        return universal
    filtered, _ = shop_matcher._filter_universal_to_catchment(  # noqa: SLF001
        universal,
        user_lat=user_lat,
        user_lng=user_lng,
        catchment_km=catchment_km,
        shops_index=shop_matcher._all_shops_indexed(),  # noqa: SLF001
    )
    return filtered


def _media_payload(video: dict[str, Any]) -> dict[str, Any]:
    """Public, frontend-friendly summary of a video's media metadata.

    Thumbnail URLs are routed through the API so they outlive the upstream
    signed CDN links from TikTok/Instagram (`thumbnails/<id>.jpg` is committed
    alongside the catalog).
    """

    media = dict(video.get("media") or {})
    cover_path = media.get("cover_path")
    if cover_path:
        media["thumbnail_url"] = f"/recipe-videos/{video['id']}/cover.jpg"
        media.pop("cover_path", None)
    video_path = media.get("video_path")
    # ``"cdn"`` is the sentinel persist_video writes when the MP4 was
    # uploaded straight to the Hetzner recipe-mp4 nginx box (Railway
    # production path); the file does not exist locally and any URL
    # must come from RECIPE_VIDEO_BASE_URL below.
    pinned_remote = video_path == "cdn"
    if pinned_remote:
        video_path = None
    # If the catalog row didn't pin video_path but the MP4 exists on
    # disk under videos/<id>.mp4 (the canonical location
    # persist_video writes to in dev), expose it anyway.
    if not video_path and not pinned_remote:
        candidate = HERE / "videos" / f"{video['id']}.mp4"
        if candidate.exists():
            video_path = f"videos/{video['id']}.mp4"
            media["video_path"] = video_path

    base = os.environ.get("RECIPE_VIDEO_BASE_URL", "").rstrip("/")
    if pinned_remote and base:
        media["video_url"] = f"{base}/{video['id']}.mp4"
        media.pop("video_path", None)
        return media
    if base:
        # When a CDN base URL is configured (e.g.
        # `https://api.leo.casa/recipe-mp4`), trust the CDN and emit
        # an absolute URL for every catalog row — even when the MP4
        # isn't shipped inside the deploy tree (the rsync filter
        # deliberately excludes the 160 MB videos folder). The
        # frontend's `localVideoUrl` accepts absolute URLs unchanged,
        # so it gracefully falls back to the embed if the CDN 404s.
        media["video_url"] = f"{base}/{video['id']}.mp4"
        media.pop("video_path", None)
    elif video_path:
        # Local-dev fallback: serve the MP4 through the API itself.
        media["video_url"] = f"/recipe-videos/{video['id']}/source.mp4"
        media.pop("video_path", None)
    return media


def _public_video(
    video: dict[str, Any],
    *,
    include_recipe: bool = False,
    user_lat: float | None = None,
    user_lng: float | None = None,
    catchment_km: float | None = None,
) -> dict[str, Any]:
    payload = {
        key: video[key]
        for key in ("id", "sort", "source", "source_url", "source_id", "creator", "title", "status")
        if key in video
    }
    payload["summary"] = video.get("summary", "")
    payload["recipe_title"] = (video.get("recipe") or {}).get("title") or video.get("title")
    payload["ingredient_preview"] = [
        item.get("description")
        for item in (video.get("recipe") or {}).get("ingredients", [])[:4]
        if item.get("description")
    ]
    payload["media"] = _media_payload(video)
    match = _resolved_match(
        video,
        user_lat=user_lat,
        user_lng=user_lng,
        catchment_km=catchment_km,
    )
    if match:
        match = _repair_obvious_gaps(match)
    payload["feasibility"] = (match or {}).get("feasibility") or "unknown"
    if include_recipe:
        payload["recipe"] = video.get("recipe") or {}
        payload["match"] = match
        if match and match.get("shop_groups"):
            payload["stock_groups"] = _stock_groups_from_match(match)
        else:
            payload["stock_groups"] = _stock_groups_legacy(video)
    return payload


def _resolve_committed_path(video_id: str, key: str) -> Path | None:
    """Resolve a path stored on the catalog row's ``media[key]`` to an
    absolute path under ``HERE``. Returns None if missing or escapes the
    recipe_videos directory.
    """
    for row in _catalog():
        if row.get("id") != video_id:
            continue
        rel = (row.get("media") or {}).get(key)
        if not rel:
            return None
        candidate = HERE / rel
        if not candidate.exists():
            return None
        try:
            candidate.resolve().relative_to(HERE.resolve())
        except ValueError:
            return None
        return candidate
    return None


def cover_path(video_id: str) -> Path | None:
    """Resolve a committed cover image for ``video_id``, if present."""
    return _resolve_committed_path(video_id, "cover_path")


def video_path(video_id: str) -> Path | None:
    """Resolve a committed source MP4 for ``video_id``, if present.

    Two lookup strategies — the catalog row's ``media.video_path`` wins
    when populated, but we also fall back to ``videos/<id>.mp4`` on
    disk so the back-catalog (where rows pre-date the persist_video
    field) still serves a native MP4. Symmetrical with
    :func:`_media_payload`.
    """
    explicit = _resolve_committed_path(video_id, "video_path")
    if explicit:
        return explicit
    candidate = HERE / "videos" / f"{video_id}.mp4"
    if candidate.exists():
        try:
            candidate.resolve().relative_to(HERE.resolve())
        except ValueError:
            return None
        return candidate
    return None


def _ordered_rows(seed: int | None) -> list[dict[str, Any]]:
    """Return catalog rows in the order Discover should surface them.

    Without a seed: stable curated order by ``sort``. With a seed: the same
    rows shuffled deterministically, so pagination within a session stays
    consistent but each cold load gets a fresh mix.
    """

    rows = list(_catalog())
    if seed is None:
        return rows
    rng = random.Random(seed)
    rng.shuffle(rows)
    return rows


def list_videos(
    *,
    cursor: int = 0,
    limit: int = 20,
    seed: int | None = None,
    user_lat: float | None = None,
    user_lng: float | None = None,
    catchment_km: float | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Return a paginated, optionally-shuffled slice of the Discover catalog.

    When ``user_lat``/``user_lng`` are supplied, each row's
    ``match_universal`` is resolved per-user (cheap filter, no LLM rerun)
    and rows whose resolved feasibility is ``infeasible`` are dropped —
    we won't show ceviche to someone with no fishmonger nearby, but the
    detail endpoint still serves it if linked directly. Without a pin we
    fall back to the row's stored universal feasibility.

    The ``user_id`` arg is currently unused but reserved for the per-user
    prematch path (see plan C1–C4): once ``user_recipe_matches`` is
    populated, this listing will join against that table.
    """
    cursor = max(0, cursor)
    limit = min(max(1, limit), 50)

    rows = _ordered_rows(seed)

    have_pin = user_lat is not None and user_lng is not None
    effective_catchment = catchment_km or shop_matcher.DEFAULT_CATCHMENT_KM

    visible: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "processed":
            continue
        match = _resolved_match(
            row,
            user_lat=user_lat,
            user_lng=user_lng,
            catchment_km=effective_catchment if have_pin else None,
        )
        if match is not None:
            shop_groups = match.get("shop_groups") or []
            blockers = match.get("blockers") or []
            if not shop_groups and blockers:
                continue
        visible.append(row)

    page = visible[cursor : cursor + limit]
    next_cursor = cursor + limit if cursor + limit < len(visible) else None
    return {
        "videos": [
            _public_video(
                row,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=effective_catchment if have_pin else None,
            )
            for row in page
        ],
        "next_cursor": next_cursor,
        "total": len(visible),
        "seed": seed,
        "catchment_km": effective_catchment if have_pin else None,
    }


def _ingredient_keys(recipe: dict[str, Any] | None) -> set[str]:
    """Tokenise a recipe's ingredient list into a set of normalised tokens.

    Lowercased, accent-stripped, ≥3 chars. Used by the upsell endpoint to
    score how many ingredients two recipes share without needing a real
    NLP pipeline — good enough at our scale (200 recipes in the catalog).
    """

    if not recipe:
        return set()
    out: set[str] = set()
    for ingredient in recipe.get("ingredients") or []:
        for source_field in (
            ingredient.get("description"),
            ingredient.get("name"),
        ):
            if not source_field:
                continue
            for token in _normalize(str(source_field)).split():
                if len(token) >= 3:
                    out.add(token)
    return out


def upsell_candidates(
    video_id: str,
    *,
    limit: int = 3,
    user_lat: float | None = None,
    user_lng: float | None = None,
    catchment_km: float | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` recipe rows whose ingredients overlap most with
    the seed recipe identified by ``video_id``.

    Used by the chat agent's "stretch the basket" path: after a user adds a
    recipe to their basket, surface 2-3 sibling recipes that maximise
    ingredient overlap so the same shopping trip yields more meals.

    When a pin is supplied we filter out candidates that are fully
    infeasible in the user's catchment (mirrors the Discover list filter)
    so we never recommend something they can't actually buy.
    """

    seed_row: dict[str, Any] | None = None
    for row in _catalog():
        if row.get("id") == video_id:
            seed_row = row
            break
    if seed_row is None:
        raise KeyError(video_id)

    seed_keys = _ingredient_keys(seed_row.get("recipe"))
    if not seed_keys:
        return []

    have_pin = user_lat is not None and user_lng is not None
    effective_catchment = catchment_km or shop_matcher.DEFAULT_CATCHMENT_KM

    scored: list[tuple[int, dict[str, Any]]] = []
    for row in _catalog():
        if row.get("id") == video_id:
            continue
        if row.get("status") and row["status"] != "processed":
            continue
        keys = _ingredient_keys(row.get("recipe"))
        if not keys:
            continue
        overlap = len(seed_keys & keys)
        if overlap < 1:
            continue
        if have_pin:
            match = _resolved_match(
                row,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=effective_catchment,
            )
            if match is not None:
                shop_groups = match.get("shop_groups") or []
                blockers = match.get("blockers") or []
                if not shop_groups and blockers:
                    continue
        scored.append((overlap, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    out: list[dict[str, Any]] = []
    for overlap, row in scored[:limit]:
        public = _public_video(
            row,
            user_lat=user_lat,
            user_lng=user_lng,
            catchment_km=effective_catchment if have_pin else None,
        )
        public["overlap_score"] = overlap
        public["overlap_ingredients"] = sorted(
            seed_keys & _ingredient_keys(row.get("recipe"))
        )
        out.append(public)
    return out


def get_video(
    video_id: str,
    *,
    user_lat: float | None = None,
    user_lng: float | None = None,
    catchment_km: float | None = None,
) -> dict[str, Any]:
    have_pin = user_lat is not None and user_lng is not None
    effective_catchment = catchment_km or shop_matcher.DEFAULT_CATCHMENT_KM
    for row in _catalog():
        if row.get("id") == video_id:
            return _public_video(
                row,
                include_recipe=True,
                user_lat=user_lat,
                user_lng=user_lng,
                catchment_km=effective_catchment if have_pin else None,
            )
    raise KeyError(video_id)
