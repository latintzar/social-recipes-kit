"""Chat-synthesized recipes — same catalog row shape as social extraction."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import uuid
from typing import Any

import shop_matcher

from recipe_videos.extract_social_recipe import (
    DEFAULT_MODEL,
    HERE,
    MATCHER_TIMEOUT_SECONDS,
    OPENROUTER_URL,
    RUNS_DIR,
    load_catalog,
    parse_json_response,
    write_catalog,
)
from recipe_videos.recipe_schema_prompt import synthesis_system_prompt, synthesis_user_prompt

logger = logging.getLogger("vitrina.recipe_videos.synthesize")


def _openrouter_text_recipe(
    *,
    model: str,
    brief: str,
    leftover_product_ids_grams: dict[str, float] | None,
    dietary_notes: str | None,
) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for recipe synthesis")
    user = synthesis_user_prompt(
        brief,
        leftover_product_ids_grams=leftover_product_ids_grams,
        dietary_notes=dietary_notes,
    )
    payload = {
        "model": model,
        "temperature": 0.35,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": synthesis_system_prompt()},
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
            "X-Title": "Vitrina recipe synthesis",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return parse_json_response(text)


def synthesize_recipe_from_brief(
    brief: str,
    *,
    leftover_product_ids_grams: dict[str, float] | None = None,
    dietary_notes: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Create a catalog row identical in shape to ``process_video`` success.

    No video download — OpenRouter text-only JSON. Pre-bakes ``match_universal``.
    """

    brief = (brief or "").strip()
    if not brief:
        raise ValueError("brief is required")

    rid = f"chat-{uuid.uuid4().hex[:12]}"
    run_dir = RUNS_DIR / rid / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    use_model = model or DEFAULT_MODEL
    recipe = _openrouter_text_recipe(
        model=use_model,
        brief=brief,
        leftover_product_ids_grams=leftover_product_ids_grams,
        dietary_notes=dietary_notes,
    )
    recipe.setdefault("source_url", "")
    recipe.setdefault("source_platform", "unknown")
    (run_dir / "recipe.json").write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")

    match_universal: dict[str, Any] | None = None
    try:
        match_universal = shop_matcher.match_recipe(
            recipe,
            catchment_km=None,
            auto_expand=False,
            timeout=MATCHER_TIMEOUT_SECONDS,
        )
        (run_dir / "match_universal.json").write_text(
            json.dumps(match_universal, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("synthesize.matcher_failed id=%s err=%s", rid, exc)

    title = recipe.get("title") or brief[:80]
    catalog = {row["id"]: row for row in load_catalog()}
    sort = (max((row.get("sort") or 0) for row in catalog.values()) + 1) if catalog else 1

    row: dict[str, Any] = {
        "id": rid,
        "sort": sort,
        "source": "Chat",
        "source_id": rid.replace("chat-", "", 1),
        "source_url": "",
        "creator": "@vitrina-chat",
        "title": title,
        "summary": recipe.get("description") or "",
        "status": "processed",
        "recipe": recipe,
        "match_universal": match_universal,
        "media": {},
        "extraction": {
            "run_dir": str(run_dir.relative_to(HERE)),
            "model": use_model,
            "origin": "chat_synthesize",
        },
    }
    catalog[rid] = row
    write_catalog(list(catalog.values()))
    return row
