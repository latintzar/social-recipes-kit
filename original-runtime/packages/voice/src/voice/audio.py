"""ffmpeg-backed audio normalization helpers.

Single source of truth for any voice code that needs audio on disk at a known
sample rate. De-duplicates what was split between ``shared/voice.py``
(``convert_audio_to_wav``) and the local-Whisper ASR path (its own inline ffmpeg
block).

Design:

- ``ext_for_content_type(ct)`` → the filename extension we give the temp source
  file. ffmpeg auto-detects codec from extension when the MIME is ambiguous.
- ``to_wav(...)`` → one-shot converter. Returns the Path to the WAV; caller owns
  cleanup.
- ``wav_tempfile(...)`` → context manager wrapping ``to_wav`` with guaranteed
  cleanup of both source and WAV on exit.

All three share a single private ``_run_ffmpeg`` helper so the ffmpeg invocation
exists in exactly one place.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "ext_for_content_type",
    "to_wav",
    "wav_tempfile",
]


# ---------------------------------------------------------------------------
# MIME → extension mapping
# ---------------------------------------------------------------------------

def ext_for_content_type(content_type: str) -> str:
    """Return a filename extension for an audio MIME type.

    - ``ogg`` → ``ogg`` (WhatsApp voice notes: ``audio/ogg; codecs=opus``)
    - ``mp4`` / ``m4a`` → ``m4a`` (iOS voice memo exports)
    - ``wav`` → ``wav`` (Desktop recorder output)
    - ``webm`` → ``webm`` (Browser ``MediaRecorder`` output)
    - unknown / empty → ``webm`` (most-forgiving fallback for OpenAI-compat audio APIs)
    """
    ct = (content_type or "").lower()
    if "ogg" in ct:
        return "ogg"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "wav" in ct:
        return "wav"
    if "webm" in ct:
        return "webm"
    return "webm"


# ---------------------------------------------------------------------------
# Core one-shot converter
# ---------------------------------------------------------------------------

def _run_ffmpeg(
    src_path: Path,
    dst_path: Path,
    *,
    sample_rate: int,
    channels: int,
    timeout: int,
) -> None:
    """Invoke ``ffmpeg`` to convert ``src_path`` to ``dst_path`` at the given
    sample rate / channel count. Raises :class:`RuntimeError` on non-zero exit.

    Stderr is captured and the first 300 chars are included in the error message
    — enough to diagnose codec errors without flooding logs with the entire
    ffmpeg output.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src_path),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-f",
            "wav",
            str(dst_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(result.stderr or '')[:300]}")


def to_wav(
    audio: bytes,
    *,
    content_type: str,
    sample_rate: int = 16000,
    channels: int = 1,
    timeout: int = 30,
) -> Path:
    """Convert audio bytes to WAV on disk, return the Path.

    The source temp file (at ``<returned_path>.with_suffix('.' + ext_for_content_type(ct))``)
    is NOT cleaned up automatically — callers own both paths. Prefer
    :func:`wav_tempfile` when you don't need the file to persist past a scope.

    Args:
        audio:         Raw audio bytes.
        content_type:  MIME type (for extension inference).
        sample_rate:   Target sample rate in Hz. Default 16000 (Whisper requirement).
        channels:      Target channel count. Default 1 (mono; Whisper requirement).
        timeout:       ffmpeg subprocess timeout in seconds. Default 30.

    Returns:
        ``pathlib.Path`` to the WAV file.

    Raises:
        RuntimeError: ffmpeg non-zero exit (message includes first 300 chars of stderr).
    """
    ext = ext_for_content_type(content_type)
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(audio)
        src_path = Path(f.name)

    wav_path = src_path.with_suffix(".wav")
    try:
        _run_ffmpeg(
            src_path,
            wav_path,
            sample_rate=sample_rate,
            channels=channels,
            timeout=timeout,
        )
    except Exception:
        # Best-effort cleanup of the source temp file on conversion failure —
        # the WAV path was never created, so nothing else to clean.
        with contextlib.suppress(Exception):
            src_path.unlink(missing_ok=True)
        raise
    return wav_path


@contextlib.contextmanager
def wav_tempfile(
    audio: bytes,
    *,
    content_type: str,
    sample_rate: int = 16000,
    channels: int = 1,
    timeout: int = 30,
) -> Iterator[Path]:
    """Context-manager version of :func:`to_wav` with guaranteed cleanup.

    Preferred in new code — both the source temp file and the converted WAV are
    unlinked on ``with`` block exit (success or exception).

    Example:
        >>> with wav_tempfile(audio_bytes, content_type="audio/ogg") as wav:
        ...     model.transcribe(str(wav))
    """
    wav_path = to_wav(
        audio,
        content_type=content_type,
        sample_rate=sample_rate,
        channels=channels,
        timeout=timeout,
    )
    src_path = wav_path.with_suffix(f".{ext_for_content_type(content_type)}")
    try:
        yield wav_path
    finally:
        for p in (src_path, wav_path):
            with contextlib.suppress(Exception):
                p.unlink(missing_ok=True)
