#!/usr/bin/env python3
"""Batch-extract recipe JSON for Vitrina's social video catalog.

Two URL families are supported by :func:`download_video`:

* Public social URLs (TikTok, ``instagram.com/reel/…``, YouTube Shorts) →
  yt-dlp via Webshare proxy or direct.
* Meta IG Messaging CDN URLs (``lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=…``)
  served when an IG shopper *shares* a reel into our DM. These respond
  with ``Content-Type: video/mp4`` directly (no redirect, no public
  permalink — Meta does not expose one), so we stream them with httpx
  in :func:`_download_lookaside_mp4`.  Both paths produce the same
  ``(video_path, metadata, route)`` tuple so the downstream pipeline
  (frame extraction → Gemini vision) is identical.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_HERE_DIR = Path(__file__).resolve().parent
if str(_HERE_DIR) not in sys.path:
    sys.path.insert(0, str(_HERE_DIR))
import shop_matcher  # noqa: E402
try:
    from recipe_videos.recipe_schema_prompt import vision_extraction_prompt
except ImportError:
    from recipe_schema_prompt import vision_extraction_prompt  # noqa: E402

HERE = Path(__file__).resolve().parent
# Catalog is a working set, not inventory: each run upserts rows into it. Feeds
# under feeds/ describe inputs; the bootstrap one was the very first batch.
CATALOG_PATH = HERE / "creator_catalog.json"
RUNS_DIR = HERE / "runs"
THUMBS_DIR = HERE / "thumbnails"
VIDEOS_DIR = HERE / "videos"
FEEDS_DIR = HERE / "feeds"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("VITRINA_RECIPE_MODEL", "google/gemini-2.5-flash")
IMAGE_DETAIL = os.environ.get("VITRINA_RECIPE_IMAGE_DETAIL", "low")
# Frame extraction caps. We let scene-change detection drive the actual
# count (it picks the visually informative cuts), but bracket the result
# so we never feed the multimodal LLM 1 frame from a static talking-head
# nor 60 frames from a fast-cut montage.
#
# Override the cap via VITRINA_RECIPE_FRAME_CAP. The legacy
# VITRINA_RECIPE_MAX_FRAMES env var is honoured for backward compat.
VITRINA_RECIPE_FRAME_CAP = int(
    os.environ.get(
        "VITRINA_RECIPE_FRAME_CAP",
        os.environ.get("VITRINA_RECIPE_MAX_FRAMES", "20"),
    )
)
VITRINA_RECIPE_FRAME_FLOOR = int(os.environ.get("VITRINA_RECIPE_FRAME_FLOOR", "6"))
VITRINA_RECIPE_SCENE_THRESHOLD = float(
    os.environ.get("VITRINA_RECIPE_SCENE_THRESHOLD", "0.3")
)
# Back-compat alias for callers that imported the old name.
DEFAULT_MAX_FRAMES = VITRINA_RECIPE_FRAME_CAP
MATCHER_TIMEOUT_SECONDS = float(os.environ.get("VITRINA_MATCHER_TIMEOUT_SECONDS", "90"))
WEBSHARE_PROXY_HOST = os.environ.get("WEBSHARE_PROXY_HOST", "p.webshare.io")
WEBSHARE_PROXY_PORT = os.environ.get("WEBSHARE_PROXY_PORT", "80")

logger = logging.getLogger("vitrina.recipe_videos")

def _load_default_feed() -> list[dict[str, str]]:
    """Default CLI feed is empty.

    The legacy ``feeds/bootstrap_*.json`` rows were retired in favour of
    :mod:`scripts.build_seed_pool`, which pipes URLs from
    ``feeds/seed_creators_tiktok.json`` straight into :func:`extract_from_url`
    with no intermediate JSON. Direct CLI users should always pass
    ``--feed`` (or use the URL form) — this stub preserves the historical
    callsite without resurrecting a dead config file.
    """
    return []


def redact_cmd(cmd: list[str]) -> str:
    """Keep proxy credentials out of logs and run artifacts."""
    redacted: list[str] = []
    hide_next = False
    for part in cmd:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(part)
        if part in {"--proxy", "--geo-verification-proxy"}:
            hide_next = True
    return " ".join(redacted)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    logger.info("command.start cmd=%s", redact_cmd(cmd))
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        logger.error("command.failed cmd=%s details=%s", redact_cmd(cmd), details)
        raise RuntimeError(details or f"command failed: {redact_cmd(cmd)}") from exc


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"missing required binary: {name}")


def load_feed(path: Path | None) -> list[dict[str, str]]:
    if not path:
        return _load_default_feed()
    return json.loads(path.read_text(encoding="utf-8"))


def load_catalog() -> list[dict[str, Any]]:
    if not CATALOG_PATH.exists():
        return []
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def default_cookies_from_browser() -> str | None:
    """Optional yt-dlp ``--cookies-from-browser`` value (e.g. ``chrome``)."""

    return os.environ.get("VITRINA_YTDLP_COOKIES_BROWSER") or os.environ.get("YTDLP_COOKIES_FROM_BROWSER")


def webshare_proxy() -> str | None:
    """Return the Webshare rotating residential proxy URL used by yt-dlp."""
    explicit = (
        os.environ.get("VITRINA_WEBSHARE_PROXY_URL")
        or os.environ.get("WEBSHARE_PROXY_URL")
        or os.environ.get("VITRINA_YTDLP_PROXY")
    )
    if explicit:
        if "webshare" in explicit.lower() or "p.webshare.io" in explicit:
            return explicit
        logger.warning("webshare.proxy_ignored reason=non_webshare_proxy")
        return None

    username = os.environ.get("WEBSHARE_PROXY_USERNAME") or os.environ.get("WEBSHARE_USERNAME")
    password = os.environ.get("WEBSHARE_PROXY_PASSWORD") or os.environ.get("WEBSHARE_PASSWORD")
    if not (username and password):
        logger.warning("webshare.proxy_missing env=WEBSHARE_PROXY_USERNAME,WEBSHARE_PROXY_PASSWORD")
        return None
    country = os.environ.get("WEBSHARE_COUNTRY")
    if country:
        username = f"{country.upper()}-{username}"
    return f"http://{username}:{password}@{WEBSHARE_PROXY_HOST}:{WEBSHARE_PROXY_PORT}"


def write_catalog(rows: list[dict[str, Any]]) -> None:
    rows = sorted(rows, key=lambda row: int(row.get("sort", 9999)))
    CATALOG_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _upload_to_hetzner(video_id: str, source_path: Path) -> bool:
    """Push ``source_path`` to the Hetzner recipe-mp4 nginx CDN via restricted rsync.

    Production (Railway) sets ``RECIPE_VIDEO_UPLOAD_SSH`` and
    ``RECIPE_VIDEO_UPLOAD_SSH_KEY`` (an inline ed25519 private key whose
    pubkey is pinned on Hetzner's authorized_keys with a forced
    ``rrsync -wo /…/recipe_videos/videos/`` command). The Railway
    container therefore never accumulates MP4s on its own ephemeral
    disk — the file streams straight from yt-dlp's run dir to nginx,
    and the local ``videos/`` mirror is only used in dev (when the
    env vars are unset).

    Returns ``True`` when the upload succeeded. Any failure is logged
    and falls through to the local-copy code path so an extraction
    never breaks just because the CDN is unreachable for a moment.
    """

    host = os.environ.get("RECIPE_VIDEO_UPLOAD_SSH")
    key_material = os.environ.get("RECIPE_VIDEO_UPLOAD_SSH_KEY")
    if not host or not key_material:
        return False
    if shutil.which("rsync") is None:
        logger.warning("recipe_mp4.upload_skipped reason=rsync_missing id=%s", video_id)
        return False
    # Materialise the key on disk just for the duration of the call.
    # 0600 perms are required by ssh; tempfile.NamedTemporaryFile honours umask.
    import tempfile
    key_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False
        ) as fh:
            fh.write(key_material if key_material.endswith("\n") else key_material + "\n")
            key_path = Path(fh.name)
        os.chmod(key_path, 0o600)
        ssh_cmd = (
            f"ssh -i {key_path} "
            "-o StrictHostKeyChecking=accept-new "
            "-o UserKnownHostsFile=/root/.ssh/known_hosts "
            "-o ConnectTimeout=15"
        )
        # rrsync expects a *relative* destination path inside its rooted dir.
        cmd = [
            "rsync",
            "-az",
            "--timeout=60",
            "-e",
            ssh_cmd,
            str(source_path),
            f"{host}:{video_id}.mp4",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.warning(
                "recipe_mp4.upload_failed id=%s rc=%s stderr=%s",
                video_id,
                result.returncode,
                result.stderr.strip()[:400],
            )
            return False
        logger.info("recipe_mp4.upload_ok id=%s host=%s", video_id, host)
        return True
    except Exception as exc:  # pragma: no cover - network / filesystem errors
        logger.warning("recipe_mp4.upload_error id=%s err=%s", video_id, exc)
        return False
    finally:
        if key_path is not None and key_path.exists():
            try:
                key_path.unlink()
            except OSError:
                pass


def persist_video(video_id: str, source_path: Path) -> str | None:
    """Persist a downloaded MP4 to wherever Vitrina serves it from.

    Two modes, picked at runtime by env:

    * **CDN mode** (production / Railway) — when ``RECIPE_VIDEO_UPLOAD_SSH``
      is set, rsync the MP4 to the Hetzner ``recipe-mp4`` nginx CDN over
      a restricted, write-only SSH channel and skip writing a copy to
      the Railway container's ephemeral disk. The frontend reaches the
      file via ``RECIPE_VIDEO_BASE_URL`` (e.g.
      ``https://api.leo.casa/recipe-mp4``), so Railway never has to host
      MP4s. Returns the literal ``"cdn"`` so the catalog row records
      that the file is pinned remotely.

    * **Local mode** (dev / Hetzner) — copy to ``videos/<id>.mp4``, the
      same path the bind-mounted nginx alias serves locally. Returns
      the relative path inside the recipe_videos dir.

    Why self-host at all? Platform iframes are unreliable: TikTok's embed
    silently swaps in random "For You" clips when autoplay is blocked,
    Instagram's embed forbids autoplay outright. A native ``<video>``
    element pointing at our own MP4 sidesteps both. Storage budget:
    avg TikTok @ ~5 MB → 12,800 clips per 64 GB.
    """

    if not source_path.exists():
        return None

    if _upload_to_hetzner(video_id, source_path):
        # CDN-only mode: do *not* leave a copy on the Railway container.
        # The catalog row stores a sentinel so we know the file is pinned;
        # _media_payload prefers the absolute RECIPE_VIDEO_BASE_URL anyway.
        return "cdn"

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    target = VIDEOS_DIR / f"{video_id}.mp4"
    shutil.copyfile(source_path, target)
    return str(target.relative_to(HERE))


def persist_cover(video_id: str, run_dir: Path) -> str | None:
    """Copy the run's downloaded thumbnail to a stable, committed path.

    TikTok/Instagram thumbnail URLs in metadata expire (~36h on TikTok), so we
    always persist the JPG locally and serve it through the API instead of
    leaning on the upstream signed URL.
    """

    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [
        path
        for path in run_dir.glob("source.*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    if not candidates:
        return None
    target = THUMBS_DIR / f"{video_id}.jpg"
    shutil.copyfile(candidates[0], target)
    return str(target.relative_to(HERE))


def media_payload(
    metadata: dict[str, Any],
    cover_path: str | None,
    video_path: str | None = None,
) -> dict[str, Any]:
    """Distill yt-dlp metadata into the small 'media' shape the catalog stores."""

    payload: dict[str, Any] = {}
    if cover_path:
        payload["cover_path"] = cover_path
    if video_path:
        payload["video_path"] = video_path
    if metadata.get("duration"):
        payload["duration_seconds"] = float(metadata["duration"])
    if metadata.get("width") and metadata.get("height"):
        payload["width"] = int(metadata["width"])
        payload["height"] = int(metadata["height"])
    if metadata.get("view_count"):
        payload["view_count"] = int(metadata["view_count"])
    if metadata.get("uploader"):
        payload["uploader"] = metadata["uploader"]
    return payload


def _download_video_once(
    url: str,
    run_dir: Path,
    cookies_from_browser: str | None,
    proxy: str | None,
) -> tuple[Path, dict[str, Any]]:
    require_binary("yt-dlp")
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize",
        "250m",
        "--merge-output-format",
        "mp4",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        str(run_dir / "source.%(ext)s"),
        url,
    ]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    if proxy:
        cmd.extend(["--proxy", proxy])
    run(cmd)
    candidates = [
        path
        for path in run_dir.glob("source.*")
        if path.suffix.lower() not in {".json", ".description", ".srt", ".vtt", ".part"}
    ]
    if not candidates:
        raise RuntimeError("yt-dlp completed but no video file was found")
    info_path = run_dir / "source.info.json"
    metadata = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    (run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return candidates[0], metadata


def _is_lookaside_cdn_url(url: str) -> bool:
    """Detect Meta IG Messaging CDN URLs that serve raw MP4 directly.

    Verified empirically (see ``ig_reel_permalink``): these URLs respond
    with ``Content-Type: video/mp4`` and the MP4 bytes — no redirect, no
    HTML page. yt-dlp has no extractor for them, so we download directly.
    """
    low = (url or "").lower()
    return "lookaside.fbsbx.com" in low or "ig_messaging_cdn" in low


def _download_lookaside_mp4(url: str, run_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Stream a Meta IG Messaging CDN MP4 to ``run_dir/source.mp4``.

    Returns the same ``(Path, metadata)`` shape as :func:`_download_video_once`
    so the rest of the pipeline (frame extraction → Gemini) is unchanged.
    Metadata is empty ({}) — yt-dlp would give title/uploader/description,
    but Meta does not expose any of that for third-party reel shares.
    """
    import httpx  # local import — recipe_videos service may run without httpx in some tests

    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "source.mp4"
    ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )
    with httpx.stream(
        "GET",
        url,
        headers={"User-Agent": ua, "Accept": "*/*"},
        follow_redirects=True,
        timeout=60.0,
    ) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"lookaside CDN download failed status={resp.status_code}")
        ctype = (resp.headers.get("content-type") or "").lower()
        if "video" not in ctype:
            raise RuntimeError(f"lookaside CDN unexpected content-type={ctype!r}")
        with target.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                f.write(chunk)
    if not target.exists() or target.stat().st_size < 1024:
        raise RuntimeError("lookaside CDN download produced empty file")
    metadata: dict[str, Any] = {}
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return target, metadata


def download_video(
    url: str,
    run_dir: Path,
    cookies_from_browser: str | None,
    proxy: str | None,
) -> tuple[Path, dict[str, Any], str]:
    # Meta IG Messaging CDN URLs serve the MP4 directly — yt-dlp has no
    # extractor for them.  Bypass yt-dlp and stream with httpx; produce
    # the same tuple shape so the rest of the pipeline is unchanged.
    if _is_lookaside_cdn_url(url):
        try:
            video_path, metadata = _download_lookaside_mp4(url, run_dir / "lookaside")
            return video_path, metadata, "lookaside"
        except Exception as exc:
            logger.warning("download.lookaside_failed url=%s err=%s", url[:120], exc)
            raise

    downloader_proxy = proxy or webshare_proxy()
    attempts: list[tuple[str, str | None]] = []
    if downloader_proxy:
        attempts.append(("webshare", downloader_proxy))
    attempts.append(("direct", None))

    last_error: Exception | None = None
    for label, attempt_proxy in attempts:
        try:
            video_path, metadata = _download_video_once(url, run_dir / label, cookies_from_browser, attempt_proxy)
            return video_path, metadata, label
        except Exception as exc:
            last_error = exc
            logger.warning("download.attempt_failed route=%s url=%s err=%s", label, url, exc)
    raise RuntimeError(str(last_error) if last_error else "download failed")


def probe_duration(video_path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
        )
        return float(result.stdout.strip())
    except Exception as exc:
        logger.warning("ffprobe.failed path=%s err=%s", video_path, exc)
        return None


def _ffmpeg_extract(video_path: Path, frames_dir: Path, vf: str) -> list[Path]:
    """Run ffmpeg with ``vf`` and return the produced jpeg files in order."""
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-vsync",
            "vfr",
            "-q:v",
            "3",
            str(frames_dir / "frame_%03d.jpg"),
        ]
    )
    return sorted(frames_dir.glob("frame_*.jpg"))


def extract_frames(
    video_path: Path,
    run_dir: Path,
    max_frames: int,
    *,
    floor: int | None = None,
    scene_threshold: float | None = None,
) -> list[Path]:
    """Sample frames at scene cuts rather than at uniform intervals.

    Uniform sampling ("one frame every N seconds") loses the moments
    that actually carry recipe information — the *cut* where the chef
    pulls the tray out of the oven, the on-screen text card listing
    ingredients, the final plated shot. Scene-change detection
    (``ffmpeg select='gt(scene,T)'``) keeps those by definition.

    We always pin the **first** and **last** frames (intro card +
    finished dish) so they're never dropped, then trim or top up to
    stay between ``floor`` and ``max_frames`` so multimodal LLM costs
    are predictable. The scene threshold is conservative (0.3) so
    talking-head intros yield ~6 frames, while fast-cut cooking
    montages cap at ``max_frames``.
    """
    require_binary("ffmpeg")
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    floor = max(2, floor or VITRINA_RECIPE_FRAME_FLOOR)
    threshold = scene_threshold if scene_threshold is not None else VITRINA_RECIPE_SCENE_THRESHOLD

    scene_vf = (
        f"select='gt(scene,{threshold:.2f})',"
        "scale=768:-1:force_original_aspect_ratio=decrease"
    )
    frames = _ffmpeg_extract(video_path, frames_dir, scene_vf)

    # Pin first and last frames. ffmpeg's `select` skips frame 0
    # because there's no prior frame to compare against; we extract
    # them separately.
    pinned_dir = run_dir / "frames_pinned"
    pinned_dir.mkdir(exist_ok=True)
    duration = probe_duration(video_path)
    pin_first = pinned_dir / "_first.jpg"
    pin_last = pinned_dir / "_last.jpg"
    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vf",
                "select='eq(n\\,0)',scale=768:-1:force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(pin_first),
            ]
        )
    except Exception:  # noqa: BLE001
        pin_first = None  # type: ignore[assignment]
    if duration and duration > 1:
        try:
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-sseof",
                    "-0.5",
                    "-i",
                    str(video_path),
                    "-vf",
                    "scale=768:-1:force_original_aspect_ratio=decrease",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(pin_last),
                ]
            )
        except Exception:  # noqa: BLE001
            pin_last = None  # type: ignore[assignment]

    keep_paths: list[Path] = []
    if pin_first and pin_first.exists():
        keep_paths.append(pin_first)
    keep_paths.extend(frames)
    if pin_last and pin_last.exists() and (not keep_paths or keep_paths[-1] != pin_last):
        keep_paths.append(pin_last)

    # Floor: if scene detection was too quiet, top up with uniform
    # sampling so the model doesn't have to extrapolate from 1–2
    # frames. We keep the existing scene picks and add intermediate
    # ones rather than throwing them away.
    if len(keep_paths) < floor and (duration or 0) > 0:
        topup_dir = run_dir / "frames_topup"
        topup_dir.mkdir(exist_ok=True)
        interval = max(1.5, (duration or 60.0) / floor)
        topup_vf = (
            f"fps=1/{interval:.3f},scale=768:-1:force_original_aspect_ratio=decrease"
        )
        topup_frames = _ffmpeg_extract(video_path, topup_dir, topup_vf)
        # Merge by timestamp-equivalent file name; sort all together
        # at the end so frame ordering stays roughly chronological.
        existing = set(keep_paths)
        for f in topup_frames:
            if f not in existing and len(keep_paths) < floor:
                keep_paths.append(f)

    # Cap: keep first + last and evenly distributed scene cuts.
    if len(keep_paths) > max_frames:
        head = keep_paths[0]
        tail = keep_paths[-1]
        middle = keep_paths[1:-1]
        slots = max_frames - 2
        if slots <= 0:
            keep_paths = [head, tail] if head != tail else [head]
        else:
            step = len(middle) / slots
            picked = [middle[int(i * step)] for i in range(slots)]
            keep_paths = [head, *picked, tail]
        # Best-effort cleanup of frames we're not feeding the LLM.
        kept_set = set(keep_paths)
        for f in frames:
            if f not in kept_set:
                try:
                    f.unlink()
                except Exception:  # noqa: BLE001
                    pass

    if not keep_paths:
        raise RuntimeError("ffmpeg completed but no frames were extracted")
    # Stable, chronological-ish ordering: pinned-first, scene picks,
    # pinned-last. Keep first/last bookends explicit even if the
    # interior already includes them.
    return keep_paths


def image_part(path: Path) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": IMAGE_DETAIL}}


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def call_openrouter(video: dict[str, str], metadata: dict[str, Any], frames: list[Path], model: str, run_dir: Path) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    content: list[dict[str, Any]] = [{"type": "text", "text": vision_extraction_prompt(video, metadata, len(frames))}]
    content.extend(image_part(frame) for frame in frames)
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://vitrina.bg",
            "X-Title": "Vitrina Social Recipe Extraction",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter request failed ({exc.code}): {body}") from exc
    redacted = dict(data)
    if redacted.get("choices"):
        redacted["choices"] = [
            {
                "index": choice.get("index"),
                "finish_reason": choice.get("finish_reason"),
                "message": {"role": (choice.get("message") or {}).get("role"), "content": "<redacted; see recipe.json>"},
            }
            for choice in redacted["choices"]
        ]
    (run_dir / "openrouter-response.json").write_text(json.dumps(redacted, indent=2, ensure_ascii=False), encoding="utf-8")
    text = data["choices"][0]["message"]["content"]
    recipe = parse_json_response(text)
    recipe["source_url"] = recipe.get("source_url") or video["url"]
    return recipe


def process_video(
    video: dict[str, str],
    *,
    sort: int,
    model: str,
    max_frames: int,
    cookies_from_browser: str | None,
    proxy: str | None,
) -> dict[str, Any]:
    run_dir = RUNS_DIR / video["id"] / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        video_path, metadata, download_route = download_video(video["url"], run_dir, cookies_from_browser, proxy)
        frames = extract_frames(video_path, run_dir, max_frames)
        recipe = call_openrouter(video, metadata, frames, model, run_dir)
        (run_dir / "recipe.json").write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
        # Pre-bake the universal shop match so the recipe is buyable the
        # moment it lands in Discover. Per-user resolve happens at request
        # time in the API. Failures here don't block extraction — the row
        # still ships, just without a pre-baked match (catalog falls back
        # to legacy keyword matching until the next backfill).
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
        except Exception as exc:
            logger.warning("matcher.universal_failed id=%s err=%s", video["id"], exc)
        cover = persist_cover(video["id"], run_dir / download_route)
        video_local = persist_video(video["id"], video_path)
        return {
            "id": video["id"],
            "sort": sort,
            "source": video["source"],
            "source_id": video["source_id"],
            "source_url": video["url"],
            "creator": video.get("creator") or metadata.get("uploader") or "@unknown",
            "title": video["title"],
            "summary": recipe.get("description") or "",
            "status": "processed",
            "recipe": recipe,
            "match_universal": match_universal,
            "media": media_payload(metadata, cover, video_local),
            "extraction": {"run_dir": str(run_dir.relative_to(HERE)), "model": model, "frames": len(frames), "download_route": download_route},
        }
    except Exception as exc:
        logger.error("video.extract.failed id=%s url=%s err=%s", video["id"], video["url"], exc)
        return {
            "id": video["id"],
            "sort": sort,
            "source": video["source"],
            "source_id": video["source_id"],
            "source_url": video["url"],
            "creator": video.get("creator") or "unknown",
            "title": video["title"],
            "summary": "",
            "status": "failed",
            "recipe": {},
            "extraction": {"run_dir": str(run_dir.relative_to(HERE)), "model": model, "error": str(exc)[:600]},
        }


_TIKTOK_RE = re.compile(
    r"tiktok\.com/(?:@[^/]+/(?:video|photo)/(?P<tiktok_id>\d+)|v/(?P<tiktok_short>\d+))",
    re.IGNORECASE,
)
_INSTAGRAM_RE = re.compile(
    r"instagram\.com/(?:[^/]+/)?(?:reel|p)/(?P<ig_id>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
# YouTube canonical (watch?v=, /shorts/, youtu.be/)
_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^ ]*?v=|shorts/|embed/|v/)(?P<yt_id>[A-Za-z0-9_-]{6,})"
    r"|youtu\.be/(?P<yt_short>[A-Za-z0-9_-]{6,}))",
    re.IGNORECASE,
)
# Facebook reels, watch links, and permalink videos
_FACEBOOK_RE = re.compile(
    r"facebook\.com/(?:reel/(?P<fb_reel>\d+)|watch/?\?v=(?P<fb_watch>\d+)|[^/]+/videos/(?P<fb_video>\d+))",
    re.IGNORECASE,
)


def _slug_from_url(url: str) -> str:
    """Stable lowercase slug for hosts we don't recognise; used as id suffix."""
    cleaned = re.sub(r"^https?://(?:www\.|m\.)?", "", url, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-").lower()
    return cleaned[:80] or "recipe"


def _ig_messaging_cdn_asset_id(url: str) -> str | None:
    """Parse numeric ``asset_id`` from ``lookaside.fbsbx.com/ig_messaging_cdn`` URLs."""
    low = (url or "").lower()
    if "lookaside.fbsbx.com" not in low and "ig_messaging_cdn" not in low:
        return None
    q = parse_qs(urlparse(url.strip()).query)
    raw = (q.get("asset_id") or [None])[0]
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s.isdigit() else None


def feed_row_from_url(url: str, *, title: str | None = None, added_via: str = "user_share") -> dict[str, str]:
    """Build a minimal feed row for an arbitrary public recipe URL.

    Recognises TikTok, Instagram, YouTube, and Facebook video URLs. Any
    other http(s) URL is accepted as a generic ``Web`` source and passed
    through to ``process_video``, which lets yt-dlp / the page scraper
    decide what to do with it. The chat path uses this so a user pasting
    a URL produces the same row shape the bootstrap feed uses.
    """

    if not url:
        raise ValueError("url is required")
    cleaned = url.split("?", 1)[0].rstrip("/")
    raw = url.strip()
    tiktok = _TIKTOK_RE.search(cleaned)
    if tiktok:
        source_id = tiktok.group("tiktok_id") or tiktok.group("tiktok_short")
        return {
            "id": f"tiktok-{source_id}",
            "source": "TikTok",
            "source_id": source_id,
            "title": title or "Shared TikTok recipe",
            "url": cleaned,
            "added_via": added_via,
        }
    instagram = _INSTAGRAM_RE.search(cleaned)
    if instagram:
        source_id = instagram.group("ig_id")
        return {
            "id": f"instagram-{source_id}",
            "source": "Instagram",
            "source_id": source_id,
            "title": title or "Shared Instagram recipe",
            "url": cleaned,
            "added_via": added_via,
        }
    youtube = _YOUTUBE_RE.search(raw)
    if youtube:
        source_id = youtube.group("yt_id") or youtube.group("yt_short")
        return {
            "id": f"youtube-{source_id}",
            "source": "YouTube",
            "source_id": source_id,
            "title": title or "Shared YouTube recipe",
            "url": raw.split("&", 1)[0],
            "added_via": added_via,
        }
    facebook = _FACEBOOK_RE.search(cleaned)
    if facebook:
        source_id = (
            facebook.group("fb_reel")
            or facebook.group("fb_watch")
            or facebook.group("fb_video")
        )
        return {
            "id": f"facebook-{source_id}",
            "source": "Facebook",
            "source_id": source_id,
            "title": title or "Shared Facebook recipe",
            "url": cleaned,
            "added_via": added_via,
        }
    # Meta IG Messaging CDN (DM reel share) — one stable id per ``asset_id`` only.
    if _is_lookaside_cdn_url(raw):
        cdn_asset = _ig_messaging_cdn_asset_id(raw)
        if not cdn_asset:
            raise ValueError(
                "Instagram Messaging CDN URL needs a numeric asset_id "
                "(…/ig_messaging_cdn?asset_id=…). Use the resolved instagram.com/reel/… "
                "permalink for extraction instead."
            )
        return {
            "id": f"instagram-cdn-{cdn_asset}",
            "source": "Instagram",
            "source_id": cdn_asset,
            "title": title or "Shared Instagram recipe",
            "url": raw.strip(),
            "added_via": added_via,
        }
    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        raise ValueError(f"unsupported recipe video host: {url}")
    slug = _slug_from_url(cleaned)
    return {
        "id": f"web-{slug}",
        "source": "Web",
        "source_id": slug,
        "title": title or "Shared recipe",
        "url": cleaned,
        "added_via": added_via,
    }


def _reject_instagram_dm_cdn_url(url: str) -> None:
    """Reject Instagram CDN URLs we genuinely cannot download.

    The big distinction is between:

    - ``lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=…`` — Meta IG
      Messaging CDN MP4. **This IS fetchable** by
      :func:`_download_lookaside_mp4` (responds with
      ``Content-Type: video/mp4`` directly).  Do NOT reject these.
    - ``cdninstagram.com`` / ``fbcdn.net`` / ``instagram.f*.fna.fbcdn.net`` —
      signed image/video CDN URLs that are session-bound and short-lived;
      yt-dlp has no extractor and direct fetch returns 403.  Reject early
      so the LLM sees a clear error instead of the model hanging.

    History: ``fbsbx.com`` was on the reject list and shadowed the working
    lookaside downloader path in :func:`download_video`. Removed
    2026-04-29 — see PR description for the original DM trace.
    """
    u = (url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return
    # Lookaside is downloadable — never reject.
    if "lookaside.fbsbx.com" in u or "ig_messaging_cdn" in u:
        return
    if any(x in u for x in ("cdninstagram.com", "fbcdn.net", "instagram.f")):
        raise ValueError(
            "That link is a signed Instagram image/video CDN URL "
            "(cdninstagram.com / fbcdn.net) which is session-bound and "
            "cannot be fetched server-side. The DM adapter should have "
            "resolved ``reel_video_id`` to an ``instagram.com/reel/…`` "
            "permalink first; if you see this in production, check Graph "
            "API permissions and webhook payload fields."
        )


def extract_from_url(
    url: str,
    *,
    title: str | None = None,
    added_via: str = "user_share",
    model: str = DEFAULT_MODEL,
    max_frames: int = DEFAULT_MAX_FRAMES,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    """Synchronous one-shot extractor for the chat path.

    Treats the URL like a one-row feed: derives a feed row, runs the same
    ``process_video`` pipeline used by the bootstrap batch, upserts the
    catalog cache, and registers the creator. Returns the new catalog row.
    """

    _reject_instagram_dm_cdn_url(url)
    feed_row = feed_row_from_url(url, title=title, added_via=added_via)
    catalog = {row["id"]: row for row in load_catalog()}
    cached = catalog.get(feed_row["id"])
    if cached and cached.get("status") == "processed":
        logger.info("extract.cache_hit id=%s url=%s", feed_row["id"], url)
        return cached
    effective_cookies = cookies_from_browser if cookies_from_browser is not None else default_cookies_from_browser()
    sort = (max((row.get("sort") or 0) for row in catalog.values()) + 1) if catalog else 1
    new_row = process_video(
        feed_row,
        sort=sort,
        model=model,
        max_frames=max_frames,
        cookies_from_browser=effective_cookies,
        proxy=proxy,
    )
    catalog[new_row["id"]] = new_row
    write_catalog(list(catalog.values()))
    return new_row


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Vitrina social recipe catalog.")
    parser.add_argument(
        "--feed",
        type=Path,
        help=(
            "JSON feed of {id, source, source_id, title, url} rows to extract. "
            "Empty by default — production seeding uses scripts/build_seed_pool.py "
            "(pipes URLs from feeds/seed_creators_tiktok.json straight into "
            "extract_from_url). The catalog is a growing cache — extracted rows "
            "are upserted, never thrown away."
        ),
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--proxy", help="Optional Webshare proxy URL for yt-dlp. Defaults to WEBSHARE_PROXY_URL or WEBSHARE_PROXY_USERNAME/PASSWORD.")
    parser.add_argument("--skip-processed", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(levelname)s %(message)s")
    feed = load_feed(args.feed)
    current = {row["id"]: row for row in load_catalog()}
    selected = feed[args.offset : args.offset + args.limit]
    for idx, video in enumerate(selected, start=args.offset):
        if args.skip_processed and current.get(video["id"], {}).get("status") == "processed":
            logger.info("video.skip id=%s status=processed", video["id"])
            continue
        current[video["id"]] = process_video(
            video,
            sort=idx + 1,
            model=args.model,
            max_frames=args.max_frames,
            cookies_from_browser=args.cookies_from_browser,
            proxy=args.proxy,
        )
        write_catalog(list(current.values()))
    logger.info("catalog.updated path=%s count=%s", CATALOG_PATH, len(current))
    return 0


if __name__ == "__main__":
    sys.exit(main())
