"""Local faster-whisper (CTranslate2) provider.

Runs on CPU by default (``WHISPER_DEVICE=cpu``); ``cuda`` enables GPU inference
on hosts that have CUDA set up. The Whisper model itself is a singleton per
process via ``functools.lru_cache`` — first call downloads ~600 MB and warms
the CTranslate2 backend; subsequent calls are fast.

Audio preprocessing goes through ``voice.audio.wav_tempfile``: ffmpeg converts
any input format to 16 kHz mono WAV on disk, which is what faster-whisper
needs. Cleanup is automatic via the context manager.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any, Optional

from .. import config
from ..audio import wav_tempfile
from ..types import RawTranscript

logger = logging.getLogger(__name__)

__all__ = ["LocalWhisperProvider"]


@lru_cache(maxsize=1)
def _load_whisper_model(
    model_name: str,
    device: str,
    compute_type: str,
    cpu_threads: int,
):
    """Load and cache a faster-whisper model. One slot per process.

    The cache key is all four knobs so switching device or compute type at
    runtime (rare — typically only tests) loads a fresh model instead of
    silently returning a stale one bound to the old config.
    """
    from faster_whisper import WhisperModel

    kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
    }
    if cpu_threads > 0:
        kwargs["cpu_threads"] = cpu_threads

    logger.info(
        "Loading faster-whisper | model=%s device=%s compute_type=%s cpu_threads=%s",
        model_name,
        device,
        compute_type,
        cpu_threads if cpu_threads > 0 else "auto",
    )
    model = WhisperModel(model_name, **kwargs)
    logger.info("faster-whisper ready | model=%s", model_name)
    return model


class LocalWhisperProvider:
    """Concrete :class:`TranscriptionProvider` for local faster-whisper."""

    name: str = "local"

    def __init__(
        self,
        *,
        model_name: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
    ) -> None:
        self._model_name_override = model_name
        self._device_override = device
        self._compute_type_override = compute_type

    def _resolve_model_name(self) -> str:
        return self._model_name_override or config.whisper_model_name()

    def _resolve_device(self) -> str:
        return self._device_override or config.whisper_device()

    def _resolve_compute_type(self) -> str:
        return self._compute_type_override or config.whisper_compute_type()

    def _get_model(self):
        return _load_whisper_model(
            self._resolve_model_name(),
            self._resolve_device(),
            self._resolve_compute_type(),
            config.whisper_cpu_threads(),
        )

    def _transcribe_kwargs(self) -> dict[str, Any]:
        """Transcribe-call kwargs for faster-whisper.

        ``vad_filter=True`` trims silence (significant speedup on long inputs).
        ``condition_on_previous_text=False`` is set for distil checkpoints —
        they're trained with it off for stable fast inference.
        """
        model_name = self._resolve_model_name().lower()
        kwargs: dict[str, Any] = {
            "language": None,
            "beam_size": config.whisper_beam_size(),
            "vad_filter": True,
        }
        if "distil" in model_name:
            kwargs["condition_on_previous_text"] = False
        return kwargs

    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> RawTranscript:
        """Convert audio to WAV via ffmpeg, then run faster-whisper inference."""
        import asyncio

        return await asyncio.to_thread(
            self._transcribe_sync, audio, content_type, language, initial_prompt
        )

    def _transcribe_sync(
        self,
        audio: bytes,
        content_type: str,
        language: Optional[str],
        initial_prompt: Optional[str],
    ) -> RawTranscript:
        t0 = time.time()

        with wav_tempfile(audio, content_type=content_type) as wav_path:
            model = self._get_model()
            kwargs = self._transcribe_kwargs()
            if language:
                kwargs["language"] = language
            if initial_prompt:
                kwargs["initial_prompt"] = initial_prompt

            segments, info = model.transcribe(str(wav_path), **kwargs)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            lang = getattr(info, "language", "") or ""

        return RawTranscript(text=text, lang=lang, asr_seconds=time.time() - t0)
