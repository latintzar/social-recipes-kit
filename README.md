# recipe-kit

Turn a TikTok / Instagram / YouTube / Facebook cooking video into a **structured
recipe** and a **video that actually plays in your own app** — even when the clip
has no caption, no transcript, and no public permalink.

This is extracted from a production system that ingests recipe videos every
night and lets people share reels into a DM inbox. Two installable packages:

| Package | Lang | What it does |
|---|---|---|
| [`recipe-extractor`](packages/recipe-extractor) | Python (PyPI) | URL → structured recipe JSON + a persisted, natively-playable MP4. Includes the Instagram-DM CDN crack. |
| [`recipe-video-player`](packages/recipe-video-player) | TS/React (npm) | Plays your self-hosted MP4 natively, with a platform-iframe fallback. |

The extractor is a small CLI/HTTP service, so it doesn't matter what your app is
written in — call it over HTTP and you get JSON back.

---

## The method (why this is better than "just embed the reel")

### 1. Read the video, not the caption

Most recipe scrapers read the description text. Half of cooking videos have a
useless caption ("link in bio 🔥") and no transcript. So instead we **watch the
video**:

- `ffmpeg` samples frames at **scene cuts**, not at fixed intervals. The cut
  where the chef pulls the tray out of the oven, the on-screen ingredient card,
  the final plated shot — those are exactly the frames that carry recipe
  information, and scene-change detection (`select='gt(scene,0.3)'`) keeps them
  by definition. We always pin the first and last frame, then bracket the count
  between a floor and a cap so the LLM bill is predictable.
- Those frames go to a **vision LLM** with a strict JSON schema. No caption
  needed; it reads the cooking.

The prompt enforces ingredient discipline that pays off downstream: each
ingredient is split into a `canonical` single noun ("tomato"), a separate `prep`
note ("diced"), and a best-effort `quantity_g`. Combination ingredients ("pico
de gallo") are decomposed into buyable components. You get clean, normalised
rows whether or not you ever do shop-matching.

### 2. Self-host the video so it plays

Platform embeds are hostile:

- **iOS Safari** refuses cross-origin iframe autoplay no matter what `allow`
  attributes you set.
- **TikTok's** embed silently swaps in a random "For You" clip when the original
  can't autoplay — your user asked for carbonara and gets a stranger's roast.
- **Instagram's** embed disables autoplay entirely and forces a tap-through.

So we download the MP4 once at extraction time and serve it ourselves. The
React player renders a plain `<video autoplay muted playsinline loop>` — which
works everywhere — and only falls back to the platform iframe if the local file
is missing.

### 3. The Instagram CDN crack (the social "send us a reel" path)

This is the part that's genuinely hard. When someone **shares a reel into your
Instagram DM inbox**, the webhook does *not* give you `instagram.com/reel/…`.
You get one of:

- a `lookaside.fbsbx.com/ig_messaging_cdn?asset_id=…` link, **or**
- a message id you have to expand.

Two findings that make this work (both encoded in
[`instagram_cdn.py`](packages/recipe-extractor/src/recipe_extractor/instagram_cdn.py)
and [`extract.py`](packages/recipe-extractor/src/recipe_extractor/extract.py)):

1. **The `lookaside` CDN URL serves the raw MP4 directly** — `Content-Type:
   video/mp4`, no redirect, no HTML. yt-dlp has no extractor for it, so we
   stream it ourselves with httpx and an iOS user-agent. (Meanwhile
   `cdninstagram.com` / `fbcdn.net` URLs are session-bound and 403 server-side —
   we detect and reject those early instead of hanging.)
2. **Message `shares` objects use `link`, not `url`.** Requesting
   `fields=shares{url}` makes Graph *omit the field entirely*. You must request
   `shares{link,url,type,…}` and read both. If the share is a CDN link, resolve
   its `asset_id` via `GET /{asset_id}?fields=permalink,shortcode` to recover a
   public permalink yt-dlp can then handle.

Net result: a user DMs you a reel and it becomes a structured, replayable recipe
— no copy-paste, no "open in Instagram."

The whole inbound connection (verify handshake, signature check, payload
normalization, reel resolution, auto-reply) is turnkey via `make_router` — see
[`instagram_webhook.py`](packages/recipe-extractor/src/recipe_extractor/instagram_webhook.py)
and the [package README](packages/recipe-extractor/README.md#the-instagram-messages-api-flow-share-a-reel--recipe).

---

## Quick start

```bash
# 1. extractor (Python)
cd packages/recipe-extractor
cp .env.example .env          # set OPENROUTER_API_KEY at minimum
pip install -e ".[service]"   # needs yt-dlp + ffmpeg on PATH

# one-shot CLI
recipe-extractor "https://www.tiktok.com/@user/video/123"

# or run it as a service your app calls
recipe-extractor-serve        # POST /extract {"url": "..."}  ->  recipe JSON
                              # GET  /videos/<id>.mp4         ->  the MP4
```

```bash
# 2. player (React)
cd packages/recipe-video-player
npm install && npm run build
```

```tsx
import { RecipeVideoPlayer } from 'recipe-video-player';

<RecipeVideoPlayer
  source="TikTok"
  sourceUrl={recipe.source_url}
  sourceId={recipe.source_id}
  videoUrl={`${API}/videos/${recipe.id}.mp4`}  // your self-hosted MP4
  posterUrl={posterUrl}
/>
```

See [`examples/extract_one.py`](examples/extract_one.py) for an end-to-end run.

## Requirements

- Python 3.10+, with **`yt-dlp`** and **`ffmpeg`** on PATH.
- An OpenAI-compatible **vision** LLM key (OpenRouter by default — swap the
  endpoint/model via env to use OpenAI, Groq, etc).
- For the DM-share flow only: a Meta Graph API token
  (`INSTAGRAM_PAGE_ACCESS_TOKEN`).

## License

MIT — see [LICENSE](LICENSE).
