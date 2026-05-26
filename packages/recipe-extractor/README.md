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

## The Instagram Messages API flow (share a reel → recipe)

Let people **share a reel into your Instagram DM inbox** and have it ingested
automatically. Two ways in:

**A. Drop-in webhook (FastAPI).** `make_router` implements the full Meta
connection — GET verify handshake, `X-Hub-Signature-256` HMAC check, payload
normalization, reel resolution, and an optional auto-reply — and runs each
shared reel through `extract_recipe`:

```python
from fastapi import FastAPI
from recipe_extractor import make_router

app = FastAPI()

def on_reel(sender_id, result):
    save_to_my_db(sender_id, result)         # your storage
    return f"Saved {result['recipe']['title']} ✅"   # DM'd back to the sender

app.include_router(make_router(on_reel=on_reel))
# exposes GET/POST /instagram/webhook
```

Set `INSTAGRAM_VERIFY_TOKEN`, `INSTAGRAM_APP_SECRET`, and
`INSTAGRAM_PAGE_ACCESS_TOKEN`, then point your Meta app's Messaging webhook
(subscribed to `messages`) at `…/instagram/webhook`.

**B. Bring your own server.** Use the framework-agnostic helpers directly:

```python
from recipe_extractor import (
    verify_subscription, verify_signature, iter_messaging_events,
    send_message, extract_recipe,
)

# GET handshake -> echo verify_subscription(mode, token, challenge)
# POST: verify_signature(header, raw_body), then:
for msg in iter_messaging_events(json_payload):
    if msg["reel_url"] and not msg["is_echo"]:
        result = extract_recipe(msg["reel_url"])   # reel_url already resolved
        send_message(msg["sender_id"], f"Saved {result['recipe']['title']}")
```

`iter_messaging_events` does the hard part: it resolves an `ig_reel` attachment
(via the Graph media node) or a message `mid` (via its `shares` object) into an
extractable `reel_url`. `lookaside.fbsbx.com/ig_messaging_cdn?asset_id=…` links
and pasted permalinks are also accepted directly by `extract_recipe`. See the
root README for *why* the lookaside trick works.

## Config

All via env — see [`.env.example`](.env.example). The only required one is
`OPENROUTER_API_KEY`. Everything else (model, frame caps, proxy, remote video
host, IG token) is optional.
