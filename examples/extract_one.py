#!/usr/bin/env python3
"""Minimal end-to-end demo: a URL in, a recipe + playable MP4 out.

    export OPENROUTER_API_KEY=sk-or-...
    pip install -e packages/recipe-extractor
    python examples/extract_one.py "https://www.tiktok.com/@user/video/123"

Prints the structured recipe and the path to the downloaded MP4 (which you can
serve to the recipe-video-player React component).
"""

from __future__ import annotations

import json
import sys

from recipe_extractor import extract_recipe


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python extract_one.py <recipe-video-url>", file=sys.stderr)
        return 2

    result = extract_recipe(sys.argv[1])

    if result["status"] != "processed":
        print("extraction failed:", result["extraction"].get("error"), file=sys.stderr)
        return 1

    recipe = result["recipe"]
    print(f"\n  {recipe.get('title', '(untitled)')}")
    print(f"  by {result.get('creator')}  —  {result['source']}\n")

    print("  Ingredients:")
    for ing in recipe.get("ingredients", []):
        grams = f" (~{ing['quantity_g']} g)" if ing.get("quantity_g") else ""
        print(f"    - {ing.get('description', ing.get('canonical', '?'))}{grams}")

    print("\n  Steps:")
    for step in recipe.get("directions", []):
        print(f"    {step.get('step')}. {step.get('text')}")

    print(f"\n  Playable MP4: {result['media'].get('video_path')}")
    print(f"  (full result JSON below)\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
