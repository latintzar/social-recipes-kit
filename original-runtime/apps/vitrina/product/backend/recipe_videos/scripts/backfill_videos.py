"""Mirror every catalog row's MP4 into ``videos/<id>.mp4`` (or the CDN).

Why this exists:

* The Discover frontend renders a self-hosted ``<video>`` whenever the
  catalog row exposes a ``media.video_url``; on 404 it falls through to
  the platform iframe (which is unreliable: TikTok's embed silently swaps
  in random "For You" clips when autoplay is blocked, Instagram's embed
  forbids autoplay outright). So every row that *should* play in our own
  player needs an MP4 on the Hetzner recipe-mp4 nginx CDN.
* The extract pipeline (``persist_video``) now uploads straight to the
  CDN whenever the ``RECIPE_VIDEO_UPLOAD_SSH`` env vars are set. This
  script handles two cases the runtime path doesn't cover:

  1. **Legacy rows** with a stashed ``runs/<id>/<route>/source.mp4`` from
     a previous local extraction — copy the existing file into place,
     no network needed.
  2. **Pure backfill** rows that were imported (e.g. via the seed pool
     builder) without ever running through the full extract pipeline.
     For these we re-download from the row's ``source_url`` using the
     same ``download_video`` helper the runtime uses, then persist it.

Run from the repo root:

    uv run python apps/vitrina/product/backend/recipe_videos/scripts/backfill_videos.py

Pass ``--dry-run`` to see what would happen without touching anything.
Pass ``--limit N`` to cap re-downloads (handy when iterating).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RUNS_DIR = HERE / "runs"
VIDEOS_DIR = HERE / "videos"
CATALOG_PATH = HERE / "creator_catalog.json"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# extract_social_recipe sits next to this script's parent. Importing it
# gives us the same yt-dlp + Webshare proxy plumbing the runtime uses,
# so the backfill behaves identically to a live extraction.
from extract_social_recipe import (  # noqa: E402
    download_video,
    persist_video,
    webshare_proxy,
)

logger = logging.getLogger("backfill_videos")


def _find_existing_source_mp4(video_id: str) -> Path | None:
    """Return the most recent ``source.mp4`` under ``runs/<id>/<ts>/<route>/``."""
    base = RUNS_DIR / video_id
    if not base.exists():
        return None
    candidates = sorted(base.glob("*/*/source.mp4"), reverse=True)
    if candidates:
        return candidates[0]
    flat = sorted(base.glob("*/source.mp4"), reverse=True)
    return flat[0] if flat else None


_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _pick_video_file(run_dir: Path, fallback: Path) -> Path:
    """Return the actual video file inside ``run_dir`` if there is one.

    ``download_video`` has a long-standing glob bug: when yt-dlp also
    persists a thumbnail (``source.jpg``) it can return the JPEG as the
    "video" path. The video itself is on disk regardless, so we re-glob
    here and prefer real video extensions before falling back to
    whatever the runtime returned.
    """
    for path in sorted(run_dir.rglob("source.*")):
        if path.suffix.lower() in _VIDEO_SUFFIXES:
            return path
    return fallback


def _redownload(video_id: str, source_url: str) -> Path | None:
    run_dir = RUNS_DIR / video_id / "backfill"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        video_path, _metadata, route = download_video(
            source_url,
            run_dir,
            cookies_from_browser=None,
            proxy=webshare_proxy(),
        )
    except Exception as exc:
        logger.warning("download_failed id=%s err=%s", video_id, exc)
        return None
    real = _pick_video_file(run_dir, video_path)
    logger.info("downloaded id=%s route=%s path=%s", video_id, route, real)
    return real


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on re-download attempts. Local copies are unlimited.",
    )
    parser.add_argument(
        "--ids",
        nargs="*",
        default=None,
        help="Only operate on these catalog ids (default: every row missing a mirror).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    copied = uploaded = skipped_have = skipped_no_source = downloaded = 0
    redownload_attempts = 0
    catalog_dirty = False

    for row in rows:
        video_id = row["id"]
        if args.ids and video_id not in args.ids:
            continue

        media = row.setdefault("media", {})
        local_target = VIDEOS_DIR / f"{video_id}.mp4"

        # Already pinned and the file (or CDN sentinel) is good — nothing to do.
        if media.get("video_path") and (
            media["video_path"] == "cdn"
            or local_target.exists()
        ):
            skipped_have += 1
            continue

        # Try a stashed run dir first (free, offline).
        source = _find_existing_source_mp4(video_id)

        # Fall back to re-downloading from the public source_url.
        if source is None:
            if args.limit is not None and redownload_attempts >= args.limit:
                logger.info("limit_reached skipping id=%s", video_id)
                skipped_no_source += 1
                continue
            src_url = row.get("source_url")
            if not src_url:
                logger.warning("no_source_url id=%s", video_id)
                skipped_no_source += 1
                continue
            if args.dry_run:
                logger.info("would_redownload id=%s url=%s", video_id, src_url)
                redownload_attempts += 1
                continue
            redownload_attempts += 1
            source = _redownload(video_id, src_url)
            if source is None:
                skipped_no_source += 1
                continue
            downloaded += 1

        if args.dry_run:
            logger.info("would_persist id=%s from=%s", video_id, source)
            continue

        # persist_video uploads to the CDN if env is configured, otherwise
        # writes the local mirror. Either path produces a valid catalog
        # video_path value (the literal "cdn" or the relative path).
        persisted = persist_video(video_id, source)
        if persisted is None:
            logger.warning("persist_failed id=%s", video_id)
            skipped_no_source += 1
            continue
        if persisted == "cdn":
            uploaded += 1
        else:
            copied += 1
        media["video_path"] = persisted
        catalog_dirty = True

    if catalog_dirty and not args.dry_run:
        CATALOG_PATH.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    logger.info(
        "summary copied=%d uploaded=%d downloaded=%d already_pinned=%d skipped=%d dry_run=%s",
        copied,
        uploaded,
        downloaded,
        skipped_have,
        skipped_no_source,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
