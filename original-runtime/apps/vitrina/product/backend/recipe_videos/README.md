# `backend/recipe_videos/` — Social Recipe Pipeline

Audience: `customer`

## Grand Goal

Vitrina's product-owned recipe extractor: creator video URL → structured
recipe JSON → ingredients matched to local shops → one-tap basket. The
**Discover** feed and the **chat URL-share** path both flow through this
pipeline; only the trigger differs (pre-seeded TikTok pool vs. user
pasting a URL).

It graduated from `product/scratch/experiments/social-recipe-extraction/`
after the first ClipRecipe-style spike proved the shape: `yt-dlp` downloads
public media, `ffmpeg` samples frames, and an OpenRouter vision model
returns strict recipe JSON.

## Why This Lives In Product

The current behaviour has one real consumer (Vitrina) and contains
Vitrina-specific policy: starter creators, Sofia shop grouping, cart
metadata, UI copy. Keep it here until a second app needs generic social
video extraction. If/when that happens, extract only the reusable engine
pieces into `packages/scraping`, and leave feed curation + stock matching
in this app.

## File Map

### Canonical inputs
- **`feeds/seed_creators_tiktok.json`** — the **single canonical seed
  list**. ~38 TikTok handles for the starter creators we trust to produce
  consistently great food content. Edit this file to add/remove starter
  creators. There is exactly one input file by design — see plan
  `vitrina-async-chat-and-seed-pool` for the rationale (the previous
  design bounced through `starter_handles.json → creators.json →
  starter_feed.py` and left a trail of dead seeds).

### Working catalog
- **`creator_catalog.json`** — flat JSON array of every recipe row we've
  ever extracted. Append-only; rows are upserted by `source_url`.
  Contains the full extracted recipe + universal shop match payload
  (`match_universal`). Persistent cache, not "the inventory".
- **`thumbnails/<video_id>.jpg`** — locally-cached cover image, served at
  `/recipe-videos/{id}/cover.jpg`. We pin these because TikTok/Instagram
  cover URLs are signed and expire (~36 h on TikTok).
- **`videos/<video_id>.mp4`** — pinned source MP4. Two storage modes:
  - **Dev / Hetzner-local**: written to this folder, served at
    `/recipe-videos/{id}/source.mp4` (FastAPI) and at
    `https://api.leo.casa/recipe-mp4/<id>.mp4` (nginx via the
    `/var/www/recipe-mp4/` bind-mount on the Hetzner box).
  - **Production / Railway**: `persist_video` rsyncs straight to the
    Hetzner CDN via a write-only SSH channel
    (`RECIPE_VIDEO_UPLOAD_SSH` + `RECIPE_VIDEO_UPLOAD_SSH_KEY`) and
    leaves no copy on the Railway container's ephemeral disk. The
    catalog row records the literal `"cdn"` sentinel as `media.video_path`
    so we know the file is pinned remotely; the frontend reads
    `media.video_url` (built from `RECIPE_VIDEO_BASE_URL`) regardless.
  The frontend renders a native `<video autoplay muted playsinline loop>`
  against the resulting URL, which bypasses two showstoppers in the
  platform iframes: (1) iOS Safari forbids cross-origin iframe
  autoplay, and (2) TikTok's `/embed/v2/{id}` page silently swaps in a
  random "For You" clip when the original can't autoplay (the famous
  "open BLP, get Sunday Roast" bug). When the CDN 404s for a row
  whose extraction couldn't fetch the MP4 (e.g. TikTok auth-walls),
  `<video onError>` flips a flag and the same component falls through
  to the platform iframe. Storage budget: ~5 MB / TikTok clip
  → ≈12 800 videos per 64 GB on the Hetzner CDN.
- **`runs/<video_id>/<timestamp>/`** — raw `yt-dlp` artifacts (info JSON,
  description, MP4, sampled frames, OpenRouter response). Kept for audit
  and re-extraction without re-downloading. Safe to wipe when disk
  pressure demands it; the canonical playable copy lives in `videos/`.

### Modules
- **`extract_social_recipe.py`** — production extractor. Downloads with
  `yt-dlp` (Webshare proxy → direct fallback), samples frames with
  `ffmpeg` scene-change detection (`select='gt(scene,0.30)'` plus pinned
  first/last frames, capped between `VITRINA_RECIPE_FRAME_FLOOR` (6) and
  `VITRINA_RECIPE_FRAME_CAP` (20)), asks OpenRouter for strict recipe
  JSON, persists the cover, then upserts the row into `creator_catalog.json`.
- **`shop_matcher.py`** — universal-then-per-user shop matcher. Default
  model is `anthropic/claude-haiku-4.5` (override via
  `VITRINA_MATCHER_MODEL`); chosen for structured-judgment + JSON
  adherence at ~Flash latency.
- **`catalog.py`** — read-side: paginated `list_videos` (with optional
  shuffle seed + per-user catchment filter), `get_video`,
  `upsell_candidates`, `cover_path`. Frontend talks to this via
  `/recipe-videos*` routes.
- **`synthesize_recipe.py`** — text-only OpenRouter recipe + matcher
  upsert (`chat-*` ids, `source: Chat`).
- **`recipe_schema_prompt.py`** — single JSON schema string shared by
  vision extraction and chat synthesis.
- **`searxng_client.py` + `social_discover.py`** — SearXNG JSON search
  for social video URLs (`VITRINA_SEARXNG_URL` or `SEARXNG_URL`).
  Exposed as MCP tool `recipes.discover_social`.
- **`unit_parser.py`** — small ingredient unit/quantity parser shared
  between extractor and matcher.

### Builder script
- **`scripts/build_seed_pool.py`** — assembles the 40-row seed pool.
  Reads `feeds/seed_creators_tiktok.json`, uses
  `yt-dlp --flat-playlist --playlist-end N` per handle to get URLs,
  balances them across creators, then pipes each URL straight into
  `extract_social_recipe.extract_from_url`. **No intermediate feed
  file.** Idempotent — URLs already in `creator_catalog.json` are
  skipped unless `--force`. Cost ≈ \$3–4 / wall-time ≈ 7–10 min for a
  fresh 40-row build (Gemini 2.5 Flash extraction + Haiku 4.5 matcher).
- **`scripts/backfill_matches.py`** — one-shot helper to re-run
  `match_universal` for catalog rows whose match is stale or missing.

## Runtime Contract

- `GET /recipe-videos` — Discover list. Optional `seed` shuffles the
  cache deterministically per session; `lat`/`lng`/`catchment_km`
  filter to rows whose match resolves inside the catchment.
- `GET /recipe-videos/{id}` — full recipe + match (chat + Discover modal).
- `GET /recipe-videos/{id}/cover.jpg` — committed thumbnail (immutable
  cache).
- `GET /recipe-videos/{id}/source.mp4` — locally-pinned MP4 (Range-aware,
  immutable cache). Used by the recipe modal so playback is autoplay-
  capable and never serves the wrong clip.
- `POST /recipe-videos/extract` — synchronous URL ingest (chat fast path
  uses the **async** variant `recipes.extract_from_url_async`; this
  route remains for direct/CLI usage).
- `POST /recipe-videos/synthesize` — chat-only text-authored recipe row.
- `POST /recipe-videos/like` — Mem0 preference edge for signed-in users
  (browser still owns the canonical liked set in `localStorage`).

The chat URL-share path is **async** end-to-end: the agent dispatches
`recipes.extract_from_url_async` (see `mcp/recipes_tools.py`), which
enqueues a `chat_jobs` row, spawns a worker thread, and returns an
acknowledgement. The worker posts staged progress messages
(`Downloading…`, `Extracting recipe…`, `Matching shops…`, final
`recipe_video_card` widget) directly into `chat_messages` so the user
sees them via Supabase Realtime even if they leave and come back.

## Discover UX

- Grid uses the cached portrait JPG (4:5 cards, addictive feed feel).
- Recipe modal plays the pinned MP4 from the Hetzner CDN
  (`https://api.leo.casa/recipe-mp4/<id>.mp4`) via a native `<video>`
  element, so playback autoplays muted on every modern browser and
  never silently shows a different recipe (which is what TikTok's
  `/embed/v2/{id}` page started doing when its autoplay was blocked).
  When the CDN 404s — brand-new extractions whose upload is racing
  the page load, or clips yt-dlp couldn't fetch from the Railway IP
  — `<video onError>` falls through to the platform iframe. The
  fallback is structural: every catalog row gets the same `video_url`
  shape regardless of whether the MP4 has reached Hetzner yet.
  Backfill old rows with `scripts/backfill_videos.py` (re-downloads
  via yt-dlp + persists through `persist_video`, so it works in both
  CDN and local-mirror modes).
- Pagination: client asks for 20 summaries, renders 10, prefetches the
  next 10, and pre-fetches detail payloads for visible/near-visible
  cards so the modal feels instant.
- Pull-to-refresh bumps the seed for a fresh shuffle.
- Per-user prematch (plan C1–C4): when we know the user's location, we
  background-resolve `match_universal` for the seed pool against their
  shops and store the result in the `user_recipe_matches` table — that
  way the Discover list only shows rows the user can actually buy.

## Models

| Stage           | Default                        | Override env var          |
| --------------- | ------------------------------ | ------------------------- |
| Recipe extract  | `google/gemini-2.5-flash`      | `VITRINA_RECIPE_MODEL`    |
| Shop matcher    | `anthropic/claude-haiku-4.5`   | `VITRINA_MATCHER_MODEL`   |
| Chat synthesise | `google/gemini-2.5-flash`      | `VITRINA_RECIPE_MODEL`    |

Decision (2026-04-28): Haiku 4.5 wins for the matcher because it adheres
to the strict ingredient→product JSON schema more reliably than Flash on
edge cases; Flash stays on extraction because it handles 6–20-frame
visual reasoning faster at lower cost.

## Network Policy

Webshare rotating residential proxies are the **primary** downloader for
`yt-dlp`. The extractor tries Webshare first, then falls back to a
direct download if the proxy attempt fails.

Configure either `WEBSHARE_PROXY_URL` or the dashboard credential pair:

```bash
WEBSHARE_PROXY_USERNAME=...
WEBSHARE_PROXY_PASSWORD=...
```

Default endpoint: `http://<username>:<password>@p.webshare.io:80`.
Optional `WEBSHARE_COUNTRY=US` prefixes the username for country
targeting.

Bright Data remains useful for Unlocker / browser-style page scraping
elsewhere, but it is not the media downloader for this pipeline.
Failures are recorded in `creator_catalog.json` (`status: "failed"`) so
batches can retry without blocking the web app.

### Bandwidth Budget

Webshare bandwidth is consumed by the full video download, not the
sampled frames we send to the model. One 1 GB plan covers roughly:

- ~100 videos at 10 MB each
- ~40 videos at 25 MB each
- ~20 videos at 50 MB each

Keep the `--max-filesize 250m` guard and let `build_seed_pool.py`'s
idempotency skip already-extracted URLs across reruns.

### Cookies

`VITRINA_YTDLP_COOKIES_BROWSER=chrome` (or another
`yt-dlp --cookies-from-browser` value) helps when TikTok / Instagram
require login for some accounts. Many handles work without cookies via
the Webshare proxy alone.

## Frequent Workflows

```bash
# Refresh the 40-row Discover seed pool from latest creator videos.
OPENROUTER_API_KEY=… python3 scripts/build_seed_pool.py

# Dry-run only — see which URLs would be extracted, nothing called yet.
python3 scripts/build_seed_pool.py --dry-run

# Re-extract a single URL (e.g. fixed an extractor bug).
OPENROUTER_API_KEY=… python3 extract_social_recipe.py \
  --url https://www.tiktok.com/@creator/video/123 --force

# Backfill missing/stale matcher results without re-downloading.
OPENROUTER_API_KEY=… python3 scripts/backfill_matches.py

# Pin every catalog row's MP4. Re-downloads from source_url when no
# stashed run dir exists; persists through persist_video, so the same
# script works in dev (writes videos/<id>.mp4) and in production
# (rsyncs straight to the Hetzner recipe-mp4 CDN). Pass --dry-run to
# preview, --limit N to cap re-downloads, --ids X Y to target a subset.
python3 scripts/backfill_videos.py
```
