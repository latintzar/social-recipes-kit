"""Text-only recipe synthesis — same JSON schema as video extraction.

Use this when there's no video: a user types "something with the chicken and
spinach I have", and you want a structured recipe back in the exact same shape
as :func:`recipe_extractor.extract.extract_recipe` returns under ``recipe``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

try:
    from .extract import OPENROUTER_URL, DEFAULT_MODEL, parse_json_response
    from .prompts import synthesis_system_prompt, synthesis_user_prompt
except ImportError:  # pragma: no cover
    from extract import OPENROUTER_URL, DEFAULT_MODEL, parse_json_response  # type: ignore
    from prompts import synthesis_system_prompt, synthesis_user_prompt  # type: ignore


def synthesize_recipe_from_brief(
    brief: str,
    *,
    dietary_notes: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Return a structured recipe JSON object for a free-text brief."""

    brief = (brief or "").strip()
    if not brief:
        raise ValueError("brief is required")
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("RECIPE_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY (or RECIPE_LLM_API_KEY) is required")

    use_model = model or DEFAULT_MODEL
    payload = {
        "model": use_model,
        "temperature": 0.35,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": synthesis_system_prompt()},
            {"role": "user", "content": synthesis_user_prompt(brief, dietary_notes=dietary_notes)},
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "recipe-extractor synthesis",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    recipe = parse_json_response(data["choices"][0]["message"]["content"])
    recipe.setdefault("source_url", "")
    recipe.setdefault("source_platform", "unknown")
    return recipe
