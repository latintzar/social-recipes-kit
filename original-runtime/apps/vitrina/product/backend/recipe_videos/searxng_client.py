"""Thin SearXNG JSON client for Vitrina (stdlib HTTP — no extra deps on PATH)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("vitrina.recipe_videos.searxng")

_MIN_QUERY_INTERVAL = float(os.environ.get("VITRINA_SEARXNG_MIN_INTERVAL", "1.5"))
_lock = threading.Lock()
_last_query_time = 0.0


def searxng_base_url() -> str | None:
    raw = (os.environ.get("VITRINA_SEARXNG_URL") or os.environ.get("SEARXNG_URL") or "").strip()
    return raw.rstrip("/") if raw else None


def searxng_search(query: str, *, max_results: int = 15) -> list[dict[str, Any]]:
    base = searxng_base_url()
    if not base:
        return []
    global _last_query_time
    with _lock:
        elapsed = time.monotonic() - _last_query_time
        if elapsed < _MIN_QUERY_INTERVAL:
            time.sleep(_MIN_QUERY_INTERVAL - elapsed)
        _last_query_time = time.monotonic()
        q = urllib.parse.quote_plus(query)
        url = f"{base}?q={q}&format=json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            logger.warning("searxng.request_failed err=%s", exc)
            return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("searxng.json_failed err=%s", exc)
        return []
    rows = data.get("results") or []
    return list(rows[:max_results])
