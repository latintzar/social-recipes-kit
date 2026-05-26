# recipe-extractor

URL → structured recipe JSON + a persisted, natively-playable MP4. Reads the
*video* (frames + vision LLM), not just the caption, so it works on clips with
no description and no transcript. Includes the Instagram-DM CDN crack.

> Part of [recipe-kit](../../README.md) — see the root README for the full
> method writeup.

## Install

```bash
pip install recipe-extractor              # library + CLI
pip install "recipe-extractor[service]"   # + the FastAPI HTTP wrapper
```

Needs **`yt-dlp`** and **`ffmpeg`** on PATH, and an OpenAI-compatible **vision**
LLM key. Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY`.

## Use it as a library

```python
from recipe_extractor import extract_recipe

result = extract_recipe("https://www.tiktok.com/@user/video/123")

result["recipe"]            # structured recipe JSON (see schema below)
result["media"]["video_path"]   # path to the downloaded MP4 (native playback)
result["status"]            # "processed" | "failed" (never raises per-video)
```

Text-only (no video):

```python
from recipe_extractor import synthesize_recipe_from_brief
recipe = synthesize_recipe_from_brief("something with leftover chicken and spinach")
```

## Use it as a CLI

```bash
recipe-extractor "https://www.instagram.com/reel/ABC123/"
recipe-extractor "https://youtu.be/XXXX" --max-frames 12 --model openai/gpt-4o-mini
```

## Use it as a service (any-language apps)

```bash
recipe-extractor-serve            # http://127.0.0.1:8000
```

```
POST /extract     {"url": "..."}      -> recipe result JSON
POST /synthesize  {"brief": "..."}    -> recipe JSON
GET  /videos/<id>.mp4                  -> the persisted MP4 for your player
```

## The recipe schema

Every ingredient is normalised into three fields so downstream matching is easy:

- `canonical` — one unbranded singular noun ("tomato")
- `prep` — the cut/prep verb, kept separate ("diced")
- `quantity_g` — best-effort grams even when the recipe is vague

```jsonc
{
  "title": "string",
  "source_url": "string",
  "source_platform": "instagram|tiktok|youtube|facebook|unknown",
  "ingredients": [
    { "description": "2 cloves garlic, minced", "canonical": "garlic",
      "prep": "minced", "quantity_g": 8, "optional": false, "confidence": 0.9 }
  ],
  "directions": [ { "step": 1, "text": "…" } ],
  "equipment": ["string"], "tips": ["string"], "confidence": 0.0
}
```

## The Instagram DM-share flow

If you want users to *share a reel into your Instagram DMs* and have it ingested,
set `INSTAGRAM_PAGE_ACCESS_TOKEN` and use `instagram_cdn.py`:

```python
from recipe_extractor import resolve_ig_share_url_from_message_mid, extract_recipe

# `mid` comes from your Instagram Messaging webhook payload
public_url = resolve_ig_share_url_from_message_mid(mid)
if public_url:
    result = extract_recipe(public_url)
```

`lookaside.fbsbx.com/ig_messaging_cdn?asset_id=…` links are also accepted
directly by `extract_recipe` — they serve the MP4 straight, so we stream them
even though yt-dlp can't. See the root README for *why* this works.

## Config

All via env — see [`.env.example`](.env.example). The only required one is
`OPENROUTER_API_KEY`. Everything else (model, frame caps, proxy, remote video
host, IG token) is optional.
