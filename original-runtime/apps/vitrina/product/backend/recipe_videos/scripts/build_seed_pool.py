#!/usr/bin/env python3
"""Build the 40-row TikTok seed pool that backs Vitrina Discover.

Reads ``feeds/seed_creators_tiktok.json`` (the single canonical creator
list — see plan B1), uses ``yt-dlp --flat-playlist --playlist-end N``
to fetch the latest N video URLs per handle, balances them across
creators to a target pool size (default 40), then pipes each URL
straight into :func:`extract_social_recipe.process_video`. There is
**no intermediate feed JSON** — the script writes results directly
into ``creator_catalog.json`` (the renamed ``owen_han_catalog.json``).

Why no intermediate file? The plan calls out repo-clutter as a real
problem: the previous design bounced through ``starter_handles.json``
→ ``creators.json`` → ``starter_feed.py`` → ``catalog.py`` and left a
trail of dead seeds. Keeping the build script as a pure pipe (handles
in → catalog rows out) means there's exactly one input and one output
to reason about.

Usage::

    OPENROUTER_API_KEY=… python3 scripts/build_seed_pool.py
    OPENROUTER_API_KEY=… python3 scripts/build_seed_pool.py --target 40 --per-creator 4
    OPENROUTER_API_KEY=… python3 scripts/build_seed_pool.py --dry-run

Cost: roughly $3–4 for a fresh 40-row build (Gemini 2.5 Flash for
recipe extraction + Haiku 4.5 for the universal shop match). Wall
time is ~7–10 min depending on network for the yt-dlp downloads.

This script is **idempotent**. Recipes already present in
``creator_catalog.json`` (matched by source URL) are skipped unless
``--force`` is passed.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PARENT = HERE.parent  # apps/vitrina/product/backend/recipe_videos/
BACKEND = PARENT.parent  # apps/vitrina/product/backend/
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("build_seed_pool")

SEED_FILE = PARENT / "feeds" / "seed_creators_tiktok.json"
CATALOG_FILE = PARENT / "creator_catalog.json"


def _require_yt_dlp() -> None:
    if shutil.which("yt-dlp") is None:
        raise SystemExit(
            "yt-dlp is required. Install with `pip install yt-dlp` or `brew install yt-dlp`."
        )


def list_handle_videos(handle: str, *, n: int) -> list[str]:
    """Return up to ``n`` recent TikTok video URLs for ``handle``.

    Uses ``yt-dlp --flat-playlist`` so we don't download anything yet —
    just URLs. Failures (private profiles, deleted handles, geo blocks)
    return an empty list rather than aborting the whole batch.
    """
    url = f"https://www.tiktok.com/@{handle}"
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--quiet",
                "--no-warnings",
                "--flat-playlist",
                "--playlist-end",
                str(n),
                "--print",
                "url",
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("yt-dlp failed for @%s: %s", handle, exc.stderr.strip()[:200])
        return []
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timed out for @%s", handle)
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def balanced_pick(urls_by_handle: dict[str, list[str]], target: int) -> list[tuple[str, str]]:
    """Round-robin pick across creators until we hit ``target`` videos.

    Returns ``[(handle, url), ...]`` in pick order. Creators with more
    videos than the round contributes one each round; the loop ends as
    soon as no creator has any URLs left or we hit ``target``.
    """
    pool: list[tuple[str, str]] = []
    queues = {h: list(urls) for h, urls in urls_by_handle.items() if urls}
    if not queues:
        return pool
    handles_order = list(queues.keys())
    # Stable but un-alphabetical so the first 40 isn't always the
    # alphabet's earliest creators.
    random.Random(7).shuffle(handles_order)
    while len(pool) < target and queues:
        round_consumed = False
        for h in list(handles_order):
            q = queues.get(h)
            if not q:
                queues.pop(h, None)
                continue
            pool.append((h, q.pop(0)))
            round_consumed = True
            if len(pool) >= target:
                break
        if not round_consumed:
            break
    return pool


def load_catalog_rows() -> list[dict[str, Any]]:
    """Catalog is a flat JSON array (see ``creator_catalog.json``).

    The extractor itself owns the write side via its own atomic
    ``CATALOG_PATH`` writer; this helper exists purely so the build
    script can short-circuit URLs that already extracted (idempotency).
    """
    if not CATALOG_FILE.exists():
        return []
    raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):  # legacy shape with a "rows" wrapper
        return [r for r in (raw.get("rows") or []) if isinstance(r, dict)]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=int, default=40, help="Total seed pool size")
    parser.add_argument("--per-creator", type=int, default=4, help="Latest N videos per creator")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be extracted, don't call OpenRouter")
    parser.add_argument("--force", action="store_true", help="Re-extract even if a row with the same URL already exists")
    args = parser.parse_args()

    _require_yt_dlp()

    seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    handles: list[str] = list(seed.get("handles") or [])
    if not handles:
        raise SystemExit(f"no handles in {SEED_FILE}")
    log.info("loaded %d candidate handles from %s", len(handles), SEED_FILE)

    urls_by_handle: dict[str, list[str]] = {}
    for h in handles:
        urls = list_handle_videos(h, n=args.per_creator)
        if urls:
            urls_by_handle[h] = urls
            log.info("@%s -> %d videos", h, len(urls))
        else:
            log.info("@%s -> 0 videos (skipping)", h)

    pool = balanced_pick(urls_by_handle, args.target)
    log.info("balanced pool: %d videos across %d creators", len(pool), len({h for h, _ in pool}))

    rows = load_catalog_rows()
    existing_urls = {row.get("source_url") for row in rows if row.get("source_url")}

    if args.dry_run:
        for h, u in pool:
            already = " (already in catalog)" if u in existing_urls else ""
            print(f"@{h}\t{u}{already}")
        return

    # Lazy-import to keep the dry-run path free of OpenRouter setup.
    from recipe_videos import extract_social_recipe as extractor

    extracted = 0
    skipped = 0
    failed: list[tuple[str, str, str]] = []
    for handle, url in pool:
        if url in existing_urls and not args.force:
            log.info("skip @%s (already in catalog) %s", handle, url)
            skipped += 1
            continue
        try:
            row = extractor.extract_from_url(url, title=None, added_via=f"seed_pool:@{handle}")
        except Exception as exc:  # noqa: BLE001
            log.warning("extract failed @%s url=%s err=%s", handle, url, exc)
            failed.append((handle, url, str(exc)))
            continue
        if row.get("status") != "processed":
            failed.append((handle, url, f"status={row.get('status')}"))
            continue
        existing_urls.add(url)
        extracted += 1

    log.info("done: extracted=%d skipped=%d failed=%d", extracted, skipped, len(failed))
    if failed:
        log.warning("failures (re-run later — yt-dlp / TikTok rate limits often clear up):")
        for h, u, err in failed[:10]:
            log.warning("  @%s %s :: %s", h, u, err[:200])


if __name__ == "__main__":
    main()
