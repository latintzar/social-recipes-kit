"""Recipe-driven shopping MCP tools.

Eight chat-callable tools that turn Vitrina into a single chat-centric
shopping surface:

- ``recipes.extract_from_url``    — pull a recipe + match from a TikTok/IG/YT URL.
- ``recipes.discover_social``     — SearXNG + rank social video URLs (optional auto-extract).
- ``recipes.synthesize_from_brief`` — LLM text recipe + same catalog/match shape as video rows.
- ``recipes.add_to_basket``       — open per-shop UCP carts for a recipe.
- ``recipes.replace_recipe_group`` — remove one recipe group then re-add at new portions.
- ``recipes.substitute``          — swap an ingredient in a recipe group.
- ``recipes.stretch_basket``      — leftover-grams-driven sibling recipes.
- ``basket.snapshot``             — compact basket projection for the agent loop.

These are deliberately registered alongside the existing
``dev.ucp.shopping.cart.*`` family in :mod:`tools` so the chat agent
sees one unified tool surface. The same tools will light up an
Instagram bot or any other channel later — no per-channel forks.

Why "leftover-driven" stretch matters: the previous
``catalog.upsell_candidates`` ranked overlap by lexical token match
("both contain 'onion'"). That misses the actual win — the user just
bought a 500 g pack and the recipe only needs 200 g; a sibling recipe
that uses the leftover 300 g is far more valuable than one that
duplicates the purchase. We compute ``leftover_grams`` per product
from the cart's ``group_metadata.lines`` and rank candidates by how
many of those grams they would consume.
"""
from __future__ import annotations

import logging
import math
import re
import sys
from pathlib import Path
from typing import Any

_MCP_DIR = Path(__file__).resolve().parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

# Eager-load every sibling module we need so we don't depend on
# ``/app/mcp`` staying on ``sys.path`` after the trampoline cleans it up.
# When a tool handler later does ``import chat_jobs`` lazily, the path is
# gone (the trampoline removes it on purpose so it doesn't shadow
# Hermes' top-level ``tools`` package), and the import fails with
# ``ModuleNotFoundError`` — which is exactly the production crash that
# left ``recipes.extract_from_url_async`` dead on every Instagram turn.
import cart  # noqa: E402 — sibling module under mcp/
import chat_jobs  # noqa: E402,F401 — sibling, used by extract_from_url_async


# Recipe modules live under apps/vitrina/product/backend/recipe_videos/.
# We're imported from apps/vitrina/product/backend/mcp/, so the parent
# package directory is what we need on sys.path.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from recipe_videos import catalog as recipe_catalog  # noqa: E402
from recipe_videos import shop_matcher  # noqa: E402
from recipe_videos.unit_parser import parse_pack_grams  # noqa: E402

logger = logging.getLogger("vitrina.mcp.recipes_tools")

_WEIGHT_PURCHASE_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(kg|кг|ml|мл|g|г|l|л)\b",
    re.IGNORECASE,
)


def _purchase_string_is_weight_dominant(purchase: str) -> bool:
    return bool(_WEIGHT_PURCHASE_RE.search((purchase or "").strip()))


def _grams_from_weight_purchase(purchase: str) -> float | None:
    m = _WEIGHT_PURCHASE_RE.search((purchase or "").strip())
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    if n <= 0:
        return None
    unit = m.group(2).lower()
    if unit in {"kg", "кг"}:
        return n * 1000.0
    if unit in {"l", "л"}:
        return n * 1000.0
    return n


def _effective_pack_grams(item: dict[str, Any], product: dict[str, Any]) -> float | None:
    pg = item.get("pack_grams")
    try:
        if pg is not None:
            v = float(pg)
            if v > 0:
                return v
    except (TypeError, ValueError):
        pass
    ps = parse_pack_grams(
        product.get("unit") if isinstance(product.get("unit"), str) else None,
        product.get("unit_size") if isinstance(product.get("unit_size"), str) else None,
    )
    if ps.grams is not None and ps.grams > 0:
        return float(ps.grams)
    return None


def _piece_pack_count(product: dict[str, Any]) -> int | None:
    text = str(product.get("unit_size") or "").lower()
    if not re.search(r"(pc|pcs|piece|pieces|бр|брой)", text):
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return 1
    try:
        return max(1, int(float(m.group(1).replace(",", "."))))
    except ValueError:
        return 1


def _recipe_count(item: dict[str, Any]) -> float | None:
    raw = item.get("quantity_recipe")
    if raw is None:
        raw = item.get("quantity")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            v = float(raw)
            return v if v > 0 else None
    except (TypeError, ValueError):
        pass
    if isinstance(raw, str):
        m = re.search(r"(\d+(?:[.,]\d+)?)", raw.strip())
        if m:
            try:
                v = float(m.group(1).replace(",", "."))
                return v if v > 0 else None
            except ValueError:
                return None
    return None


def _purchase_units_for(item: dict[str, Any], portion_scale: float) -> int:
    """Best-effort number of sellable units needed for a recipe item."""
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    if not isinstance(product, dict):
        product = {}
    try:
        scale = float(portion_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        scale = 1.0

    pack_g = _effective_pack_grams(item, product)

    qg = item.get("quantity_g")
    try:
        qgf = float(qg) if qg is not None else None
    except (TypeError, ValueError):
        qgf = None
    if qgf is not None and qgf > 0 and pack_g is not None and pack_g > 0:
        return max(1, math.ceil(qgf * scale / pack_g))

    purchase = str(item.get("quantity_purchase") or "").strip()
    if purchase and _purchase_string_is_weight_dominant(purchase):
        need = _grams_from_weight_purchase(purchase)
        if need is not None and need > 0 and pack_g is not None and pack_g > 0:
            return max(1, math.ceil(need * scale / pack_g))

    if purchase and not _purchase_string_is_weight_dominant(purchase):
        m = re.search(r"(\d+(?:[.,]\d+)?)", purchase)
        if m:
            try:
                n = float(m.group(1).replace(",", "."))
                if n > 0:
                    return max(1, math.ceil(n * scale))
            except ValueError:
                pass

    rc = _recipe_count(item)
    pieces = _piece_pack_count(product)
    if rc is not None and rc > 0 and pieces is not None and pieces > 0:
        return max(1, math.ceil(rc * scale / pieces))

    return 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_match_for(
    recipe_id: str,
    *,
    user_lat: float | None,
    user_lng: float | None,
    catchment_km: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(public_video, resolved_match)`` for ``recipe_id``.

    Uses the catchment-aware resolver when a pin is supplied, otherwise
    returns the universal match unfiltered.
    """
    video = recipe_catalog.get_video(
        recipe_id,
        user_lat=user_lat,
        user_lng=user_lng,
        catchment_km=catchment_km,
    )
    raw = next(row for row in recipe_catalog._catalog() if row.get("id") == recipe_id)  # noqa: SLF001
    universal = raw.get("match_universal") or {}
    if user_lat is not None and user_lng is not None:
        match = shop_matcher.resolve_for_user(
            recipe_id,
            universal,
            raw.get("recipe") or {},
            user_lat=user_lat,
            user_lng=user_lng,
            catchment_km=catchment_km or shop_matcher.DEFAULT_CATCHMENT_KM,
            rerun_on_partial=False,
        )
    else:
        match = universal
    return video, match


def _basket_lines_from_carts(carts: list[dict]) -> list[dict]:
    """Flatten cart_line_items across multiple carts into a uniform list."""
    out: list[dict] = []
    for c in carts:
        for line in c.get("line_items") or []:
            out.append({"cart_id": c.get("id"), "shop_id": c.get("shop_id"), **line})
    return out


def _carts_for_owner(
    *,
    cart_id: str | None,
    cart_ids: list[str] | None = None,
    cart_key: str | None,
    user_id: str | None,
) -> list[dict]:
    """Resolve every relevant cart for the given owner identity."""
    if cart_id:
        c = cart._cart_with_lines(cart_id)  # noqa: SLF001
        if c.get("status") != "open":
            return []
        if user_id or cart_key:
            cart._assert_owner(c, user_id, cart_key)  # noqa: SLF001
        return [c]
    if cart_ids:  # falsy empty list falls through to user_id/cart_key branches
        out = []
        for cid in dict.fromkeys(str(c) for c in cart_ids if c):
            c = cart._cart_with_lines(cid)  # noqa: SLF001
            if c.get("status") != "open":
                continue
            if user_id or cart_key:
                cart._assert_owner(c, user_id, cart_key)  # noqa: SLF001
            out.append(c)
        return out
    if user_id:
        rows = cart.list_user_carts(user_id) or []
        return [cart._cart_with_lines(r["id"]) for r in rows]  # noqa: SLF001
    if cart_key:
        rows = cart.list_carts_by_key(cart_key) or []
        return [cart._cart_with_lines(r["id"]) for r in rows]  # noqa: SLF001
    return []


def _leftover_ledger(carts: list[dict]) -> dict[str, float]:
    """Map ``product_id -> leftover_grams`` based on group_metadata.lines.

    Each cart line carries the full recipe in ``group_metadata.lines``,
    but that line only represents **one** product (``line.product_id``).
    We only count ``pack_grams`` and ``quantity_g`` for the metadata row
    whose ``product_id`` matches that line, and we **accumulate** packed
    grams when ``quantity`` is > 1 or the same SKU appears on multiple
    lines. Whatever remains after subtracting used grams is leftover
    that a sibling recipe could consume.
    """
    pack_grams_by_pid: dict[str, float] = {}
    needed_by_pid: dict[str, float] = {}
    for c in carts:
        for line in c.get("line_items") or []:
            meta = line.get("group_metadata") or {}
            pid = line.get("product_id")
            if not pid:
                continue
            qty = float(line.get("quantity") or 1)
            matched: dict[str, Any] | None = None
            for ml in meta.get("lines") or []:
                if ml.get("product_id") == pid:
                    matched = ml
                    break
            snap = line.get("product_snapshot") or {}
            if matched:
                pg = matched.get("pack_grams")
                if pg is not None:
                    pack_grams_by_pid[pid] = pack_grams_by_pid.get(pid, 0.0) + float(pg) * qty
                elif snap.get("pack_grams") is not None:
                    pack_grams_by_pid[pid] = (
                        pack_grams_by_pid.get(pid, 0.0) + float(snap["pack_grams"]) * qty
                    )
                qg = matched.get("quantity_g")
                if qg is not None:
                    needed_by_pid[pid] = needed_by_pid.get(pid, 0.0) + float(qg)
            elif snap.get("pack_grams") is not None:
                pack_grams_by_pid[pid] = pack_grams_by_pid.get(pid, 0.0) + float(snap["pack_grams"]) * qty

    leftovers: dict[str, float] = {}
    for pid, packed in pack_grams_by_pid.items():
        used = needed_by_pid.get(pid, 0.0)
        leftover = max(0.0, packed - used)
        if leftover > 0:
            leftovers[pid] = leftover
    return leftovers


def _current_shop_ids(carts: list[dict]) -> set[str]:
    out: set[str] = set()
    for c in carts:
        sid = c.get("shop_id")
        if sid:
            out.add(str(sid))
    return out


def _additional_shop_count(match: dict[str, Any], current_shop_ids: set[str]) -> int:
    cand: set[str] = set()
    for grp in match.get("shop_groups") or []:
        sid = (grp.get("shop") or {}).get("id")
        if sid:
            cand.add(str(sid))
    return len(cand - current_shop_ids)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def extract_from_url(
    url: str,
    *,
    user_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Extract a recipe from a social-video URL and pre-bake the match.

    Thin wrapper over :func:`extract_social_recipe.process_video` so the
    chat agent has a first-class tool for "user pasted a TikTok link".
    The resulting ``recipe_id`` becomes the handle the agent uses to
    add to basket, substitute, or stretch.

    .. note::
       This is the **synchronous** variant — it blocks for 30–90 s and
       is intentionally **not** registered for the chat agent (see
       ``RECIPE_HANDLERS`` below). Chat callers should use
       :func:`extract_from_url_async`. We keep the sync function for
       seed-pool batch extraction and tests.
    """
    from recipe_videos import extract_social_recipe as extractor  # noqa: F401

    if not url or not isinstance(url, str):
        raise ValueError("url is required")

    row = extractor.extract_from_url(url, title=None, added_via="chat_url")
    recipe_catalog.invalidate_cache()
    public, match = _resolve_match_for(
        row["id"], user_lat=lat, user_lng=lng
    )
    return {
        "recipe_id": row["id"],
        "detail": public,
        "match": match,
    }


# ── Async variant for the chat agent ──────────────────────────────────


_ASYNC_ACK_TEXT = (
    "On it — matching ingredients to shops; I'll message you here as soon as it's ready."
)


def _extract_url_worker(
    *,
    job_id: str,
    thread_id: str,
    user_id: str | None,
    cart_key: str | None,
    url: str,
    lat: float | None,
    lng: float | None,
) -> None:
    """Run the slow extract pipeline and post staged assistant messages.

    Each stage writes a row into ``chat_messages`` so the user sees
    progress as it happens — `useVitrinaChat`'s realtime subscription
    appends the bubbles live. Failures are surfaced as a single
    apology message with the agent_error ticket id.
    """
    is_ig = bool(cart_key and str(cart_key).startswith("ig:"))
    if is_ig:
        lines = (
            "Pulling the clip…",
            "Matching ingredients to shops near you…",
            "Done — say if you want this in your basket.",
        )
    else:
        lines = (
            "Downloading the video…",
            "Got the recipe — matching ingredients to shops in your area…",
            "Ready! Tap to view or add to your basket.",
        )

    try:
        chat_jobs.mark_running(job_id)
        chat_jobs.post_assistant_message(
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            text=lines[0],
        )

        from recipe_videos import extract_social_recipe as extractor  # noqa: F811

        row = extractor.extract_from_url(url, title=None, added_via="chat_url_async")
        recipe_catalog.invalidate_cache()
        chat_jobs.post_assistant_message(
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            text=lines[1],
        )

        public, match = _resolve_match_for(row["id"], user_lat=lat, user_lng=lng)

        final_widgets: list[dict[str, Any]] = []
        if not is_ig:
            final_widgets = [
                {
                    "type": "recipe_video_card",
                    "payload": {
                        "recipe_id": row["id"],
                        "detail": public,
                        "match": match,
                    },
                }
            ]

        chat_jobs.post_assistant_message(
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            text=lines[2],
            widgets=final_widgets,
        )
        # Save title alongside recipe_id so the channel adapter can remind the
        # model what was extracted on the previous turn ("add it", "yes")
        # without re-extracting. Persisted in the job result; the IG channel
        # reads the most recent succeeded job for this cart_key.
        recipe_title = (
            (row.get("recipe") or {}).get("title")
            or row.get("title")
            or (public.get("title") if isinstance(public, dict) else None)
            or ""
        )
        chat_jobs.mark_succeeded(
            job_id,
            {
                "recipe_id": row["id"],
                "recipe_title": str(recipe_title)[:200],
                "match_resolved": bool(match),
                "url": url,
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface to the user
        logger.exception("recipes.extract_from_url_async failed url=%s", url)
        chat_jobs.post_assistant_message(
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            text=(
                "I couldn't extract that recipe — the video host may be "
                "rate-limiting us, or the post might be private. "
                "Send me a different link or describe what you'd like to "
                "cook and I'll find similar recipes."
            ),
        )
        chat_jobs.mark_failed(job_id, str(exc))


def extract_from_url_async(
    url: str,
    *,
    thread_id: str,
    user_id: str | None = None,
    cart_key: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Spawn a background extraction job and return immediately.

    The actual work runs in a daemon thread (see
    :mod:`chat_jobs`) and posts staged assistant messages into the
    thread as it progresses. The agent should reply with the returned
    ``ack_text`` so the user sees an immediate "on it" while the job
    runs (vs. the old behaviour where ``recipes.extract_from_url``
    blocked the SSE stream for 30–90 s and looked like a hang).
    """
    from _session_norm import normalize_session  # noqa: PLC0415
    user_id, cart_key = normalize_session(user_id, cart_key)
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    if not thread_id:
        raise ValueError("thread_id is required for the async extractor")

    job = chat_jobs.enqueue(
        thread_id=thread_id,
        kind="recipes.extract_from_url",
        payload={"url": url, "lat": lat, "lng": lng},
        user_id=user_id,
        cart_key=cart_key,
    )
    job_id = job["id"]

    chat_jobs.spawn(
        lambda: _extract_url_worker(
            job_id=job_id,
            thread_id=thread_id,
            user_id=user_id,
            cart_key=cart_key,
            url=url,
            lat=lat,
            lng=lng,
        ),
        name=f"vitrina-extract-{job_id[:8]}",
    )

    return {"job_id": job_id, "ack_text": _ASYNC_ACK_TEXT, "status": "queued"}


def synthesize_from_brief(
    brief: str,
    *,
    leftover_product_ids_grams: dict[str, float] | None = None,
    dietary_notes: str | None = None,
    user_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Create a chat-synthesized recipe row (same catalog shape as video extract)."""

    from recipe_videos import synthesize_recipe as synth_mod  # noqa: F401

    if not brief or not isinstance(brief, str):
        raise ValueError("brief is required")
    row = synth_mod.synthesize_recipe_from_brief(
        brief.strip(),
        leftover_product_ids_grams=leftover_product_ids_grams,
        dietary_notes=dietary_notes,
    )
    recipe_catalog.invalidate_cache()
    public, match = _resolve_match_for(row["id"], user_lat=lat, user_lng=lng)
    return {
        "recipe_id": row["id"],
        "detail": public,
        "match": match,
        "synthetic": True,
    }


def discover_social(
    brief: str,
    *,
    cart_key: str | None = None,
    user_id: str | None = None,
    cart_id: str | None = None,
    cart_ids: list[str] | None = None,
    channel: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    auto_extract: bool = False,
    max_extract_attempts: int = 2,
    max_additional_shops: int | None = None,
    synthesize_fallback: bool = True,
) -> dict[str, Any]:
    """SearXNG-backed social URL discovery; optional bounded ``extract_from_url``."""

    from recipe_videos import extract_social_recipe as extractor  # noqa: F401
    from recipe_videos import social_discover  # noqa: F401

    if not brief or not isinstance(brief, str):
        raise ValueError("brief is required")

    carts = _carts_for_owner(
        cart_id=cart_id, cart_ids=cart_ids, cart_key=cart_key, user_id=user_id
    )
    leftovers = _leftover_ledger(carts)
    current_shops = _current_shop_ids(carts)

    stretch_out: dict[str, Any] | None = None
    if (cart_key or user_id) and leftovers:
        stretch_out = stretch_basket(
            cart_id=cart_id,
            cart_ids=cart_ids,
            cart_key=cart_key,
            user_id=user_id,
            lat=lat,
            lng=lng,
            max_candidates=5,
            min_reuse_ratio=0.25,
        )

    ledger_rounded = {k: round(v, 1) for k, v in leftovers.items()} if leftovers else None
    disc = social_discover.discover_social_recipe_candidates(
        brief.strip(),
        leftover_ledger=leftovers or None,
        channel=channel,
    )
    out: dict[str, Any] = {
        **disc,
        "leftover_ledger": ledger_rounded or {},
        "stretch_basket": stretch_out,
    }

    if auto_extract and disc.get("ok") and disc.get("top_urls"):
        attempts = max(1, min(int(max_extract_attempts or 1), 5))
        for url in disc["top_urls"][:attempts]:
            try:
                row = extractor.extract_from_url(url, title=None, added_via="chat_discover")
            except (ValueError, RuntimeError) as exc:
                logger.warning("discover.extract_failed url=%s err=%s", url, exc)
                continue
            if row.get("status") != "processed":
                continue
            recipe_catalog.invalidate_cache()
            public, match = _resolve_match_for(row["id"], user_lat=lat, user_lng=lng)
            addl = _additional_shop_count(match, current_shops)
            if max_additional_shops is not None and addl > int(max_additional_shops):
                continue
            out["extracted"] = {
                "recipe_id": row["id"],
                "url": url,
                "detail": public,
                "match": match,
                "additional_shops": addl,
            }
            return out

    if synthesize_fallback and not out.get("extracted"):
        tried_auto = bool(auto_extract and disc.get("top_urls"))
        no_candidates = not (disc.get("candidates") or [])
        if (not disc.get("ok")) or no_candidates or tried_auto:
            try:
                syn = synthesize_from_brief(
                    brief.strip(),
                    leftover_product_ids_grams=leftovers or None,
                    user_id=user_id,
                    lat=lat,
                    lng=lng,
                )
                out["synthesized_fallback"] = syn
            except Exception as exc:  # noqa: BLE001
                logger.warning("discover.synth_fallback_failed err=%s", exc)

    return out


def add_to_basket(
    recipe_id: str,
    portions: int = 4,
    *,
    cart_key: str | None = None,
    user_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Add every shop_group of a recipe to the shopper's UCP carts.

    Opens (or reuses) one cart per shop, attaches a stable
    ``group_id = "rcp_<recipe_id>"`` and a populated ``group_metadata``
    so the leftover-driven stretch logic can work without re-deriving
    pack sizes downstream.
    """
    if not cart_key and not user_id:
        raise ValueError("add_to_basket requires user_id or cart_key")
    portions = max(1, int(portions or 4))

    public, match = _resolve_match_for(
        recipe_id, user_lat=lat, user_lng=lng
    )
    shop_groups = match.get("shop_groups") or []
    if not shop_groups:
        return {
            "ok": False,
            "reason": "no_shop_groups",
            "recipe_id": recipe_id,
            "match": match,
        }

    group_id = f"rcp_{recipe_id}"
    name = (public.get("title") or public.get("recipe", {}).get("title") or recipe_id)
    base_serves = float(
        ((public.get("recipe") or {}).get("servings") or {}).get("quantity") or 4
    )
    portion_scale = portions / base_serves if base_serves > 0 else 1.0

    cart_snapshots: list[dict] = []
    for grp in shop_groups:
        shop_id = grp.get("shop", {}).get("id")
        if not shop_id:
            continue
        existing_rows = (
            cart.list_user_carts(user_id) if user_id
            else cart.list_carts_by_key(cart_key) if cart_key
            else []
        )
        existing = [
            c for c in existing_rows
            if c.get("shop_id") == shop_id and c.get("status") == "open"
        ]
        if existing:
            target_cart_id = existing[0]["id"]
        else:
            new_cart = cart.create_cart(
                shop_id=shop_id, user_id=user_id, cart_key=cart_key
            )
            target_cart_id = new_cart["id"]

        meta_lines: list[dict[str, Any]] = []
        for item in grp.get("items") or []:
            product = item.get("product") or {}
            units_needed = _purchase_units_for(item, portion_scale)
            meta_lines.append(
                {
                    "product_id": product.get("id"),
                    "shop_id": shop_id,
                    "ingredient": item.get("ingredient"),
                    "quantity_recipe": item.get("quantity_recipe") or item.get("quantity"),
                    "quantity_purchase": item.get("quantity_purchase"),
                    "quantity_g": (
                        (item.get("quantity_g") or 0) * portion_scale
                        if item.get("quantity_g") is not None
                        else None
                    ),
                    "pack_grams": item.get("pack_grams"),
                    "unit_size": product.get("unit_size"),
                    "unit": product.get("unit"),
                    "units_needed": units_needed,
                    "substitution": item.get("substitution"),
                }
            )
        group_metadata = {
            "name": name,
            "serves": portions,
            "origin": "chat",
            "recipe_id": recipe_id,
            "lines": meta_lines,
            "substitutions": [],
        }
        # One cart line per ingredient in the group.
        snap = None
        for item in grp.get("items") or []:
            product = item.get("product") or {}
            pid = product.get("id")
            if not pid:
                continue
            units_needed = _purchase_units_for(item, portion_scale)
            snap = cart.add_cart_item(
                cart_id=target_cart_id,
                product_id=pid,
                quantity=units_needed,
                group_id=group_id,
                group_metadata=group_metadata,
                user_id=user_id,
                cart_key=cart_key,
            )
        if snap is not None:
            cart_snapshots.append(snap)

    leftovers = _leftover_ledger(cart_snapshots)
    # Bundle hint — proactively compute the top 2 leftover-driven sibling
    # recipes so the model has the data inline. Even if the agent forgets
    # to call recipes.stretch_basket explicitly, it sees these candidates
    # in the tool result and can offer them in the same turn. This is
    # what makes "I added BLP, want me to use the leftover bacon for X?"
    # feel native rather than the user having to ask.
    bundle_hint: list[dict[str, Any]] = []
    try:
        stretch = stretch_basket(
            cart_ids=[c["id"] for c in cart_snapshots if c.get("id")] or None,
            cart_key=cart_key,
            user_id=user_id,
            lat=lat,
            lng=lng,
            max_candidates=2,
            min_reuse_ratio=0.4,
        )
        bundle_hint = stretch.get("candidates") or []
    except Exception as exc:  # noqa: BLE001 — bundle hint is best-effort
        logger.warning("bundle_hint computation failed: %s", exc)

    return {
        "ok": True,
        "recipe_id": recipe_id,
        "group_id": group_id,
        "portions": portions,
        "carts": cart_snapshots,
        "leftover_ledger": leftovers,
        "bundle_hint": bundle_hint,
    }


def replace_recipe_group(
    cart_id: str,
    group_id: str,
    recipe_id: str,
    portions: int = 4,
    *,
    cart_key: str | None = None,
    user_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Remove every line for ``group_id`` in ``cart_id``, then ``add_to_basket``."""

    if not cart_key and not user_id:
        raise ValueError("replace_recipe_group requires user_id or cart_key")
    cart.remove_group_lines(
        cart_id, group_id, user_id=user_id, cart_key=cart_key
    )
    return add_to_basket(
        recipe_id,
        portions,
        cart_key=cart_key,
        user_id=user_id,
        lat=lat,
        lng=lng,
    )


def substitute(
    cart_id: str,
    group_id: str,
    intent: str,
    *,
    ingredient: str | None = None,
    product_id: str | None = None,
    user_id: str | None = None,
    cart_key: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Swap an ingredient inside a recipe group based on a free-text intent.

    ``intent`` is the user's natural-language reason: "vegetarian",
    "no peanuts", "cheaper alternative", "no Heinz brand". We ask the
    matcher to pick a replacement product, then dispatch to the
    existing :func:`cart.substitute_in_recipe` so the substitution
    trail is appended to ``group_metadata.substitutions``.
    """
    if not ingredient and not product_id:
        raise ValueError("substitute requires ingredient or product_id")
    cart_row = cart._cart_with_lines(cart_id)  # noqa: SLF001
    target_line: dict[str, Any] | None = None
    for line in cart_row.get("line_items") or []:
        if line.get("group_id") != group_id:
            continue
        snap = line.get("product_snapshot") or {}
        if product_id and line.get("product_id") == product_id:
            target_line = line
            break
        if ingredient:
            meta = line.get("group_metadata") or {}
            for ml in meta.get("lines") or []:
                if ingredient.lower() in (ml.get("ingredient") or "").lower():
                    if ml.get("product_id") == line.get("product_id"):
                        target_line = line
                        break
            if target_line:
                break
    if target_line is None:
        raise ValueError("substitution_target_not_found")

    original_pid = target_line["product_id"]
    shop_id = cart_row.get("shop_id")
    products = shop_matcher._all_products_grouped().get(shop_id, [])  # noqa: SLF001
    candidates = [p for p in products if p["id"] != original_pid]

    replacement_pid = _pick_substitute(
        original_product=target_line.get("product_snapshot") or {},
        ingredient_text=(
            (target_line.get("group_metadata") or {}).get("lines") or [{}]
        )[0].get("ingredient")
        or ingredient
        or "",
        intent=intent,
        candidates=candidates,
    )
    if not replacement_pid:
        return {
            "ok": False,
            "reason": "no_substitute_found",
            "intent": intent,
            "original_product_id": original_pid,
        }

    updated = cart.substitute_in_recipe(
        cart_id=cart_id,
        group_id=group_id,
        original_product_id=original_pid,
        replacement_product_id=replacement_pid,
        reason=intent,
        user_id=user_id,
        cart_key=cart_key,
    )
    return {
        "ok": True,
        "cart": updated,
        "original_product_id": original_pid,
        "replacement_product_id": replacement_pid,
        "intent": intent,
    }


def _pick_substitute(
    *,
    original_product: dict[str, Any],
    ingredient_text: str,
    intent: str,
    candidates: list[dict[str, Any]],
) -> str | None:
    """Tiny LLM call: given an intent, pick a replacement product_id.

    Falls back to a keyword heuristic when no API key is configured —
    keeps tests + offline development working.
    """
    if not candidates:
        return None
    import json
    import os

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        intent_lc = (intent or "").lower()
        for cand in candidates:
            name = (cand.get("name_en") or cand.get("name_bg") or "").lower()
            if "vegetarian" in intent_lc and any(
                tok in name for tok in ("tofu", "mushroom", "bean")
            ):
                return cand["id"]
            if "cheap" in intent_lc:
                pass
        if "cheap" in (intent or "").lower():
            return min(
                candidates,
                key=lambda c: float(c.get("price_eur") or 1e9),
            )["id"]
        return candidates[0]["id"]

    prompt = (
        f"Original product: {original_product.get('name')} "
        f"({original_product.get('description')})\n"
        f"Ingredient role: {ingredient_text}\n"
        f"User intent: {intent}\n\n"
        f"Pick ONE replacement product_id from this shop's catalogue:\n"
        + json.dumps(
            [
                {
                    "id": c["id"],
                    "name": c.get("name_en") or c.get("name_bg"),
                    "description": c.get("description_en")
                    or c.get("description_bg"),
                    "price_eur": c.get("price_eur"),
                }
                for c in candidates
            ],
            ensure_ascii=False,
        )
        + '\n\nRespond with strict JSON: {"product_id": "..."}.'
    )
    try:
        parsed, _ = shop_matcher._call_openrouter(  # noqa: SLF001
            prompt,
            model=shop_matcher.DEFAULT_MODEL,
            api_key=api_key,
            timeout=30.0,
        )
        pid = parsed.get("product_id")
        if pid and any(c["id"] == pid for c in candidates):
            return pid
    except Exception as exc:  # noqa: BLE001
        logger.warning("substitute.llm_failed err=%s", exc)
    return candidates[0]["id"]


def stretch_basket(
    *,
    cart_id: str | None = None,
    cart_ids: list[str] | None = None,
    cart_key: str | None = None,
    user_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    max_candidates: int = 3,
    min_reuse_ratio: float = 0.4,
) -> dict[str, Any]:
    """Leftover-grams-driven stretch: rank sibling recipes by reuse.

    Beats the old lexical-overlap heuristic by computing the actual
    grams of leftover (pack_grams - used grams) per product, and then
    asking which catalog recipe would consume the most of those grams
    if it were added next.
    """
    carts = _carts_for_owner(
        cart_id=cart_id, cart_ids=cart_ids, cart_key=cart_key, user_id=user_id
    )
    leftovers = _leftover_ledger(carts)
    current_shop_ids = {c.get("shop_id") for c in carts if c.get("shop_id")}

    if not leftovers:
        return {"candidates": [], "leftover_ledger": {}}

    catchment_km = shop_matcher.DEFAULT_CATCHMENT_KM if lat is not None else None
    scored: list[dict[str, Any]] = []
    for row in recipe_catalog._catalog():  # noqa: SLF001
        if row.get("status") and row["status"] != "processed":
            continue
        rid = row.get("id")
        if not rid:
            continue
        match = recipe_catalog._resolved_match(  # noqa: SLF001
            row,
            user_lat=lat,
            user_lng=lng,
            catchment_km=catchment_km,
        )
        if not match or not (match.get("shop_groups") or []):
            continue
        if (match.get("blockers") or []) and not (match.get("shop_groups") or []):
            continue

        needed_total = 0.0
        leftover_consumed = 0.0
        candidate_shop_ids: set[str] = set()
        for grp in match.get("shop_groups") or []:
            sid = grp.get("shop", {}).get("id")
            if sid:
                candidate_shop_ids.add(sid)
            for item in grp.get("items") or []:
                qg = item.get("quantity_g")
                if qg is None:
                    continue
                pid = (item.get("product") or {}).get("id")
                needed = float(qg)
                needed_total += needed
                if pid in leftovers:
                    leftover_consumed += min(needed, leftovers[pid])
        if needed_total <= 0:
            continue
        reuse_ratio = leftover_consumed / needed_total
        if reuse_ratio < min_reuse_ratio:
            continue
        new_shops = candidate_shop_ids - current_shop_ids
        scored.append(
            {
                "recipe_id": rid,
                "title": row.get("title")
                or (row.get("recipe") or {}).get("title")
                or rid,
                "reuse_ratio": round(reuse_ratio, 3),
                "leftover_consumed_grams": round(leftover_consumed, 1),
                "additional_shops": sorted(new_shops),
                "additional_cost_eur": match.get("total_eur"),
                "match_summary": match.get("summary"),
                "thumbnail_url": (row.get("media") or {}).get("thumbnail_url"),
            }
        )

    # Tiebreak: more reuse first, then fewer new shops.
    scored.sort(
        key=lambda c: (-c["reuse_ratio"], len(c["additional_shops"]))
    )
    return {
        "candidates": scored[:max_candidates],
        "leftover_ledger": {k: round(v, 1) for k, v in leftovers.items()},
    }


def basket_snapshot(
    *,
    cart_id: str | None = None,
    cart_ids: list[str] | None = None,
    cart_key: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Compact basket projection — used by the agent loop and the UI."""
    carts = _carts_for_owner(
        cart_id=cart_id, cart_ids=cart_ids, cart_key=cart_key, user_id=user_id
    )
    leftovers = _leftover_ledger(carts)
    groups: dict[str, dict[str, Any]] = {}
    flat_items: list[dict[str, Any]] = []
    total_minor = 0
    for c in carts:
        shop_id = c.get("shop_id")
        for line in c.get("line_items") or []:
            gid = line.get("group_id") or "_loose"
            meta = line.get("group_metadata") or {}
            snap = line.get("product_snapshot") or {}
            pid = line.get("product_id")
            ingredient = None
            for ml in meta.get("lines") or []:
                if ml.get("product_id") == pid:
                    ingredient = ml.get("ingredient")
                    break
            item = {
                "cart_id": c.get("id"),
                "line_id": line.get("id"),
                "shop_id": shop_id,
                "product_id": pid,
                "name": snap.get("name"),
                "qty": int(line.get("quantity") or 0),
                "ingredient": ingredient,
                "pack_grams": snap.get("pack_grams"),
                "leftover_g": leftovers.get(pid),
                "subtotal_eur": (line.get("total_minor") or 0) / 100.0,
            }
            flat_items.append(item)
            total_minor += int(line.get("total_minor") or 0)
            if gid not in groups:
                groups[gid] = {
                    "group_id": gid,
                    "name": meta.get("name") or "Loose items",
                    "recipe_id": meta.get("recipe_id"),
                    "serves": meta.get("serves"),
                    "items": [],
                    "substitutions": meta.get("substitutions") or [],
                }
            groups[gid]["items"].append(item)
    result: dict[str, Any] = {
        "groups": list(groups.values()),
        "items": flat_items,
        "total_eur": round(total_minor / 100.0, 2),
        "shops": sorted({c.get("shop_id") for c in carts if c.get("shop_id")}),
        "leftover_ledger": {k: round(v, 1) for k, v in leftovers.items()},
    }
    # Stamp the basket_summary auto-widget so both the web SSE adapter and
    # any MCP-aware client receive the structured widget in the same call,
    # without a second round-trip. The SSE adapter strips this and emits a
    # ``data-widget`` frame; other clients see a harmless extra JSON field.
    #
    # IMPORTANT: ``payload.snapshot`` MUST be a shallow copy of ``result``
    # taken BEFORE we attach ``_widget``. Embedding ``result`` itself
    # creates a literal self-reference (result._widget.payload.snapshot →
    # result), which makes FastMCP's pydantic JSON serializer raise
    # ``PydanticSerializationError: Circular reference detected``. The
    # symptom is `Error calling tool 'basket.snapshot'` on every turn the
    # agent inspects the cart — which the model then surfaces as
    # "your basket is empty" or "let me try that again".
    n_items = len(flat_items)
    qty_total = sum(int(i.get("qty") or 0) for i in flat_items)
    head = (
        f"{qty_total} item{'s' if qty_total != 1 else ''} "
        f"across {n_items} line{'s' if n_items != 1 else ''}"
    )
    total_eur = result["total_eur"]
    summary = f"{head} · €{float(total_eur):.2f}" if flat_items else "Your basket is empty."
    snapshot_payload = {k: v for k, v in result.items()}
    result["_widget"] = {
        "type": "basket_summary",
        "payload": {"summary": summary, "snapshot": snapshot_payload},
    }
    return result


# ---------------------------------------------------------------------------
# Catalog awareness — let the agent browse the recipe library
# ---------------------------------------------------------------------------
#
# Without these, the only thing the agent can introspect when the user
# names a recipe ("modify Noodle Soup") is the cart. That made the agent
# look broken — it would say "I don't see Noodle Soup in your basket"
# instead of fetching it from the catalog and proposing edits.


def _recipe_search_tokens(row: dict[str, Any]) -> str:
    """Flatten title + ingredients into one normalised search blob."""
    recipe = row.get("recipe") or {}
    parts: list[str] = []
    for value in (
        row.get("title"),
        recipe.get("title"),
        row.get("summary"),
    ):
        if value:
            parts.append(str(value))
    for item in recipe.get("ingredients") or []:
        for key in ("description", "name", "ingredient"):
            v = item.get(key)
            if v:
                parts.append(str(v))
    return recipe_catalog._normalize(" ".join(parts))  # noqa: SLF001


def search_recipes(
    query: str,
    *,
    limit: int = 8,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Tool: ``recipes.search`` — fuzzy-match the catalog by title / ingredients.

    Returns up to ``limit`` shallow recipe rows ranked by token overlap.
    No LLM cost — pure in-memory string match over `recipe_catalog._catalog()`.
    """
    q_norm = recipe_catalog._normalize(query or "")  # noqa: SLF001
    if not q_norm.strip():
        return {"query": query, "results": []}
    q_tokens = [t for t in q_norm.split() if len(t) >= 2]

    catchment_km = shop_matcher.DEFAULT_CATCHMENT_KM if lat is not None else None
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in recipe_catalog._catalog():  # noqa: SLF001
        if row.get("status") and row["status"] != "processed":
            continue
        blob = _recipe_search_tokens(row)
        if not blob:
            continue
        score = 0
        if q_norm in blob:
            score += 10
        for tok in q_tokens:
            if tok in blob:
                score += 1
        if score == 0:
            continue
        public = recipe_catalog._public_video(  # noqa: SLF001
            row,
            user_lat=lat,
            user_lng=lng,
            catchment_km=catchment_km,
        )
        scored.append(
            (
                score,
                {
                    "id": public.get("id"),
                    "title": public.get("title") or public.get("recipe_title"),
                    "summary": public.get("summary"),
                    "ingredient_preview": public.get("ingredient_preview") or [],
                    "feasibility": public.get("feasibility"),
                    "creator": public.get("creator"),
                },
            )
        )
    scored.sort(key=lambda pair: -pair[0])
    return {
        "query": query,
        "results": [row for _, row in scored[: max(1, int(limit))]],
    }


def get_recipe(
    recipe_id: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
) -> dict[str, Any]:
    """Tool: ``recipes.get`` — full recipe + shop-match payload by id."""
    try:
        return recipe_catalog.get_video(
            recipe_id,
            user_lat=lat,
            user_lng=lng,
        )
    except KeyError:
        return {"ok": False, "reason": "not_found", "recipe_id": recipe_id}


# ---------------------------------------------------------------------------
# Tool registration (consumed by tools.py)
# ---------------------------------------------------------------------------


RECIPE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "recipes.extract_from_url_async",
        "description": (
            "Extract a recipe from a TikTok / Instagram / YouTube URL "
            "WITHOUT blocking the agent turn. Spawns a background job "
            "and returns {job_id, ack_text, status='queued'} immediately. "
            "The user will see staged progress messages and the final "
            "recipe_video_card via realtime — your job is to reply with "
            "ack_text (or your own warm equivalent), then stop the turn."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["url", "thread_id"],
            "properties": {
                "url": {"type": "string"},
                "thread_id": {"type": "string"},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.synthesize_from_brief",
        "description": "Create a chat-synthesized recipe (same catalog + shop match shape as video extract). Use when there is no URL or discovery failed.",
        "inputSchema": {
            "type": "object",
            "required": ["brief"],
            "properties": {
                "brief": {"type": "string"},
                "leftover_product_ids_grams": {"type": ["object", "null"]},
                "dietary_notes": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.discover_social",
        "description": "SearXNG social video discovery + snippet rank vs basket leftovers; optional auto_extract (bounded). Returns stretch_basket when leftovers exist.",
        "inputSchema": {
            "type": "object",
            "required": ["brief"],
            "properties": {
                "brief": {"type": "string"},
                "cart_key": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
                "cart_id": {"type": ["string", "null"]},
                "cart_ids": {"type": "array", "items": {"type": "string"}},
                "channel": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
                "auto_extract": {"type": "boolean", "default": False},
                "max_extract_attempts": {"type": "integer", "minimum": 1, "maximum": 5, "default": 2},
                "max_additional_shops": {"type": ["integer", "null"]},
                "synthesize_fallback": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.add_to_basket",
        "description": "Add every shop_group of a recipe to the shopper's UCP carts as one recipe group. Returns the new cart snapshot + leftover ledger.",
        "inputSchema": {
            "type": "object",
            "required": ["recipe_id"],
            "properties": {
                "recipe_id": {"type": "string"},
                "portions": {"type": "integer", "minimum": 1, "default": 4},
                "cart_key": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.replace_recipe_group",
        "description": "Delete all cart lines for a recipe group_id then add the recipe again at new portions (avoids duplicate merge on re-add).",
        "inputSchema": {
            "type": "object",
            "required": ["cart_id", "group_id", "recipe_id"],
            "properties": {
                "cart_id": {"type": "string"},
                "group_id": {"type": "string"},
                "recipe_id": {"type": "string"},
                "portions": {"type": "integer", "minimum": 1, "default": 4},
                "cart_key": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.substitute",
        "description": "Swap an ingredient inside a recipe group based on a free-text intent ('vegetarian', 'no peanuts', 'cheaper', 'no Heinz brand').",
        "inputSchema": {
            "type": "object",
            "required": ["cart_id", "group_id", "intent"],
            "properties": {
                "cart_id": {"type": "string"},
                "group_id": {"type": "string"},
                "intent": {"type": "string"},
                "ingredient": {"type": ["string", "null"]},
                "product_id": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
                "cart_key": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.stretch_basket",
        "description": "Leftover-grams-driven sibling recipes — rank catalog recipes by how much of the basket's leftovers they would consume.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": ["string", "null"]},
                "cart_ids": {"type": "array", "items": {"type": "string"}},
                "cart_key": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
                "max_candidates": {"type": "integer", "minimum": 1, "default": 3},
                "min_reuse_ratio": {"type": "number", "default": 0.4},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.search",
        "description": (
            "Fuzzy-search the existing recipe catalog by title or ingredient. "
            "ALWAYS call this first when the user names a recipe ('modify Noodle Soup', "
            "'add the BLP sandwich') — the catalog is the source of truth, not the basket."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "recipes.get",
        "description": (
            "Fetch the full recipe payload (ingredients, shop match, media) by recipe_id. "
            "Use after recipes.search to inspect or modify a specific recipe."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["recipe_id"],
            "properties": {
                "recipe_id": {"type": "string"},
                "lat": {"type": ["number", "null"]},
                "lng": {"type": ["number", "null"]},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "basket.snapshot",
        "description": "Compact basket projection grouped by recipe. Cheap (no LLM) — used by the agent loop each turn so the chat agent never has 'amnesia' about manual UI edits.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": ["string", "null"]},
                "cart_ids": {"type": "array", "items": {"type": "string"}},
                "cart_key": {"type": ["string", "null"]},
                "user_id": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
    },
]


RECIPE_HANDLERS: dict[str, Any] = {
    "recipes.extract_from_url_async": extract_from_url_async,
    "recipes.synthesize_from_brief": synthesize_from_brief,
    "recipes.discover_social": discover_social,
    "recipes.add_to_basket": add_to_basket,
    "recipes.replace_recipe_group": replace_recipe_group,
    "recipes.substitute": substitute,
    "recipes.stretch_basket": stretch_basket,
    "recipes.search": search_recipes,
    "recipes.get": get_recipe,
    "basket.snapshot": basket_snapshot,
}
