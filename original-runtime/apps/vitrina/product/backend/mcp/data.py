"""Seed-data loader for the Sofia demo shops.

Mirrors the Node prototype's ``src/data.js``: lazy-loads the seed JSON
catalogue on first access, indexes products by shop, and shapes the JSON
rows into the card payloads the tool responses return.

This layer exists so the tool handlers never touch raw JSON and so the
eventual migration to Supabase is a one-file swap. When the second tenant
lands, replace the ``_load_*`` helpers with tenant-scoped queries through
``core.Supabase`` without touching the handlers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

_SEED_DIR = Path(__file__).resolve().parent / "seed"


@lru_cache(maxsize=1)
def _load_shops() -> list[dict[str, Any]]:
    shops = json.loads((_SEED_DIR / "shops.json").read_text(encoding="utf-8"))
    extra = _SEED_DIR / "recipe_shops.json"
    if extra.exists():
        shops.extend(json.loads(extra.read_text(encoding="utf-8")))
    return shops


@lru_cache(maxsize=1)
def _load_products() -> list[dict[str, Any]]:
    products = json.loads((_SEED_DIR / "products.json").read_text(encoding="utf-8"))
    extra = _SEED_DIR / "recipe_products.json"
    if extra.exists():
        products.extend(json.loads(extra.read_text(encoding="utf-8")))
    return products


@lru_cache(maxsize=1)
def _products_by_id() -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in _load_products()}


@lru_cache(maxsize=1)
def _products_by_shop() -> dict[str, list[dict[str, Any]]]:
    by_shop: dict[str, list[dict[str, Any]]] = {}
    for p in _load_products():
        by_shop.setdefault(p["shop_id"], []).append(p)
    return by_shop


def all_shops() -> list[dict[str, Any]]:
    return list(_load_shops())


def get_shop(shop_id: str) -> dict[str, Any] | None:
    for s in _load_shops():
        if s["id"] == shop_id:
            return s
    return None


def resolve_shop(ref: str | None) -> dict[str, Any] | None:
    """Resolve a shop by id, slug, or partial name (BG or EN).

    Callers pass ``"mandra-rodopa"``, ``"Родопа"``, or ``"Rhodope"`` and get
    back the same shop record. Case- and accent-insensitive on the name match.
    """
    if not ref:
        return None
    needle = str(ref).lower()
    direct = get_shop(needle)
    if direct:
        return direct
    for s in _load_shops():
        if (
            s["id"].lower() == needle
            or (s.get("slug") == needle)
            or (s.get("name_bg") and needle in s["name_bg"].lower())
            or (s.get("name_en") and needle in s["name_en"].lower())
        ):
            return s
    return None


def products_for_shop(shop_id: str) -> list[dict[str, Any]]:
    return list(_products_by_shop().get(shop_id, []))


def get_product(product_id: str) -> dict[str, Any] | None:
    return _products_by_id().get(product_id)


def shop_card(shop: dict[str, Any]) -> dict[str, Any]:
    """The JSON shape every tool response uses when referencing a shop."""
    today = datetime.now(timezone.utc).strftime("%a").lower()[:3]
    addr = shop["address"]
    return {
        "id": shop["id"],
        "name_bg": shop["name_bg"],
        "name_en": shop["name_en"],
        "trade_bg": shop["trade_bg"],
        "trade_en": shop["trade_en"],
        "tagline_bg": shop.get("tagline_bg"),
        "address_bg": f"{addr['street_bg']}, {addr['neighborhood_bg']}, {addr['city_bg']}",
        "address_en": f"{addr['street_en']}, {addr['neighborhood_en']}, {addr['city_en']}",
        "lat": addr["lat"],
        "lng": addr["lng"],
        "phone": shop.get("phone"),
        "hero_image": shop.get("hero_image"),
        "hours_today": (shop.get("hours") or {}).get(today),
        "rating": shop.get("rating"),
        "maps_url": addr.get("google_maps_url"),
    }


def chat_shop_product_widget_payload(
    shop: dict[str, Any], product: dict[str, Any]
) -> dict[str, Any]:
    """SSE `shop_product_card` / `product_card` payload for the web chat."""
    return {"shop": shop_card(shop), "product": product_card(product)}


def product_card(p: dict[str, Any], *, live_stock: int | None = None) -> dict[str, Any]:
    """The JSON shape every tool response uses when referencing a product."""
    stock = live_stock if live_stock is not None else p.get("stock")
    price = p.get("price_eur") or 0
    return {
        "id": p["id"],
        "shop_id": p["shop_id"],
        "name_bg": p.get("name_bg"),
        "name_en": p.get("name_en"),
        "category": p.get("category"),
        "unit_size": p.get("unit_size"),
        "price_eur": price,
        "price_display": f"{price:.2f} лв",
        "stock": stock,
        "image": p.get("image"),
        "tags": p.get("tags") or [],
    }
