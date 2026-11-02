"""SearXNG-backed discovery of social recipe video URLs (rank vs leftovers)."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from recipe_videos.searxng_client import searxng_base_url, searxng_search

logger = logging.getLogger("vitrina.recipe_videos.social_discover")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("VITRINA_RECIPE_MODEL", "google/gemini-2.5-flash")

_ALLOWED_SUFFIXES = ("tiktok.com", "instagram.com", "youtube.com", "youtu.be")


def _host_allowed(netloc: str) -> bool:
    nl = (netloc or "").lower().split("@")[-1]
    if nl in ("youtu.be", "www.youtube.com", "m.youtube.com", "music.youtube.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "www.instagram.com"):
        return True
    for suf in _ALLOWED_SUFFIXES:
        if nl == suf or nl.endswith("." + suf):
            return True
    return False


def normalize_video_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip().split()[0]
    try:
        p = urlparse(cleaned)
    except Exception:  # noqa: BLE001
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    if not _host_allowed(p.netloc):
        return None
    return cleaned.split("?", 1)[0].rstrip("/") or cleaned


def heuristic_queries(
    brief: str,
    leftover_ledger: dict[str, float] | None,
    channel: str | None = None,
) -> list[str]:
    b = (brief or "").strip() or "easy dinner"
    b_short = b[:120]
    queries: list[str] = [
        f"site:tiktok.com {b_short} recipe",
        f"site:youtube.com {b_short} recipe cooking",
        f"tiktok {b_short} recipe",
        f"youtube shorts {b_short} recipe",
    ]
    if channel in ("instagram_dm", "instagram"):
        queries.insert(0, f"site:instagram.com/reel {b_short} recipe")
        queries.insert(1, f"instagram reel {b_short} recipe")
    if leftover_ledger:
        queries.append(f"site:tiktok.com leftover {b_short} recipe")
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:8]


def _openrouter_json_object(system: str, user: str, *, temperature: float = 0.2) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": DEFAULT_MODEL,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://vitrina.bg",
            "X-Title": "Vitrina social discover",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"]
        return json.loads(text.strip())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError, IndexError) as exc:
        logger.warning("social_discover.openrouter_failed err=%s", exc)
        return None


def planned_queries_llm(brief: str, leftover_ledger: dict[str, float] | None) -> list[str] | None:
    sys = (
        "You plan web search queries to find real cooking recipe videos on "
        "TikTok, Instagram, or YouTube. Return JSON only: "
        '{"queries": ["string", ...]} with 4 to 6 distinct queries. '
        "Prefer site: filters (site:tiktok.com, site:youtube.com) where helpful."
    )
    user = json.dumps(
        {"brief": brief, "leftover_product_ids_grams": leftover_ledger or {}},
        ensure_ascii=False,
    )
    parsed = _openrouter_json_object(sys, user, temperature=0.3)
    if not parsed:
        return None
    raw = parsed.get("queries")
    if not isinstance(raw, list):
        return None
    out = [str(q).strip() for q in raw if str(q).strip()]
    return out[:6] or None


def rank_candidates_llm(
    brief: str,
    leftover_ledger: dict[str, float] | None,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    compact = [
        {
            "url": r.get("url"),
            "title": r.get("title"),
            "snippet": (r.get("content") or r.get("snippet") or "")[:400],
        }
        for r in rows
        if r.get("url")
    ]
    sys = (
        "You rank social video search hits for a grocery shopper. Return JSON only: "
        '{"ranked": [{"url": "string", "score": 0.0, "reason": "short"}], ...} '
        "sorted by descending score (0-1). Prefer real recipe videos matching the brief; "
        "boost if likely to use listed leftover product ids (grams are hints). "
        "Penalize spam or non-food."
    )
    user = json.dumps(
        {"brief": brief, "leftover_product_ids_grams": leftover_ledger or {}, "hits": compact},
        ensure_ascii=False,
    )
    parsed = _openrouter_json_object(sys, user, temperature=0.1)
    if not parsed:
        return None
    ranked = parsed.get("ranked")
    if not isinstance(ranked, list):
        return None
    by_url: dict[str, dict[str, Any]] = {}
    for r in rows:
        u = normalize_video_url(r.get("url"))
        if u:
            by_url[u] = {
                "url": u,
                "title": r.get("title") or "",
                "snippet": (r.get("content") or r.get("snippet") or "")[:500],
                "source_query": r.get("source_query") or "",
            }
    out: list[dict[str, Any]] = []
    for item in ranked:
        if not isinstance(item, dict):
            continue
        u = normalize_video_url(str(item.get("url") or ""))
        if not u or u not in by_url:
            continue
        base = dict(by_url[u])
        try:
            sc = float(item.get("score", 0))
        except (TypeError, ValueError):
            sc = 0.0
        base["fit_score"] = max(0.0, min(1.0, sc))
        base["fit_reason"] = str(item.get("reason") or "")[:240]
        out.append(base)
    return out or None


def rank_keyword_fallback(brief: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tokens = [t for t in re.split(r"\W+", (brief or "").lower()) if len(t) >= 3][:12]
    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        u = normalize_video_url(r.get("url"))
        if not u:
            continue
        blob = f"{r.get('title') or ''} {(r.get('content') or r.get('snippet') or '')}".lower()
        score = 0.0
        for t in tokens:
            if t in blob:
                score += 0.08
        if "recipe" in blob or "cooking" in blob or "cook" in blob:
            score += 0.15
        rr = {
            "url": u,
            "title": r.get("title") or "",
            "snippet": (r.get("content") or r.get("snippet") or "")[:500],
            "source_query": r.get("source_query") or "",
            "fit_score": max(0.0, min(1.0, score)),
            "fit_reason": "keyword_fallback",
        }
        scored.append((-rr["fit_score"], rr))
    scored.sort(key=lambda x: x[0])
    return [r for _, r in scored]


def discover_social_recipe_candidates(
    brief: str,
    *,
    leftover_ledger: dict[str, float] | None = None,
    channel: str | None = None,
    max_queries: int = 5,
    max_per_query: int = 12,
    max_candidates: int = 20,
) -> dict[str, Any]:
    brief = (brief or "").strip()
    if not brief:
        return {
            "ok": False,
            "reason": "brief_required",
            "searxng_configured": bool(searxng_base_url()),
            "candidates": [],
            "queries_used": [],
        }
    if not searxng_base_url():
        return {
            "ok": False,
            "reason": "searxng_not_configured",
            "hint": "Set VITRINA_SEARXNG_URL or SEARXNG_URL to your SearXNG /search endpoint.",
            "searxng_configured": False,
            "candidates": [],
            "queries_used": [],
        }

    planned = planned_queries_llm(brief, leftover_ledger)
    base_queries = planned or heuristic_queries(brief, leftover_ledger, channel=channel)

    # When OpenRouter returns a generic plan, the channel-specific bias is
    # lost. Always inject the platform-preferred sources at the front so
    # Instagram DM searches lead with Reels (not TikTok), regardless of
    # whether the LLM plan ran. Dedup preserves order.
    if channel in ("instagram_dm", "instagram") and planned:
        b_short = brief[:120]
        ig_priority = [
            f"site:instagram.com/reel {b_short} recipe",
            f"instagram reel {b_short} recipe",
        ]
        seen: set[str] = set()
        merged: list[str] = []
        for q in ig_priority + base_queries:
            if q not in seen:
                seen.add(q)
                merged.append(q)
        base_queries = merged

    queries = base_queries[:max_queries]

    merged: dict[str, dict[str, Any]] = {}
    for q in queries:
        for row in searxng_search(q, max_results=max_per_query):
            url = normalize_video_url(row.get("url"))
            if not url or url in merged:
                continue
            merged[url] = {
                "url": url,
                "title": row.get("title") or "",
                "snippet": (row.get("content") or row.get("snippet") or "")[:500],
                "source_query": q,
            }

    raw_list = list(merged.values())[: max_candidates * 2]
    ranked = rank_candidates_llm(brief, leftover_ledger, raw_list)
    if not ranked:
        ranked = rank_keyword_fallback(brief, raw_list)

    ranked = ranked[:max_candidates]
    top_urls = [r["url"] for r in ranked[:3]]

    return {
        "ok": True,
        "searxng_configured": True,
        "queries_used": queries,
        "query_plan_source": "openrouter" if planned else "heuristic",
        "candidates": ranked,
        "top_urls": top_urls,
        "next_step": (
            "Call recipes.extract_from_url on top_urls[0] (then add_to_basket). "
            "If the shopper already has a basket, prefer stretch_basket first."
        ),
    }
