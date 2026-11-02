"""Deepgram Nova-3 STT provider with speaker diarization.

Deepgram is the only provider in this package that returns per-utterance
speaker labels (``diarize=true``). The standard ``transcribe()`` method
returns a flat ``RawTranscript`` (pipeline-compatible). Call
``transcribe_diarized()`` directly to get the rich ``DiarizedTranscript``
with per-utterance speaker turns.

Required env var: ``DEEPGRAM_API_KEY``
Optional env vars:
  DEEPGRAM_MODEL   — default "nova-3" (best accuracy + diarization quality)
  DEEPGRAM_LANG    — per-request override; default None (auto-detect)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from core import http_client_sync

from .. import config as voice_config
from ..audio import ext_for_content_type
from ..types import RawTranscript

logger = logging.getLogger(__name__)

__all__ = ["DeepgramProvider", "DiarizedTranscript", "Utterance"]

_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


@dataclass
class Utterance:
    """One speaker turn from Deepgram diarization."""

    speaker: int
    """0-indexed speaker number assigned by Deepgram."""

    text: str
    """Transcript text for this turn."""

    start: float
    """Start time in seconds."""

    end: float
    """End time in seconds."""

    confidence: float
    """ASR confidence 0–1."""


@dataclass
class DiarizedTranscript:
    """Full diarization result from Deepgram.

    ``utterances`` is the speaker-labelled sequence.
    ``full_text`` is the plain concatenated transcript (no speaker labels).
    ``lang`` is the detected ISO-639-1 language code.
    ``asr_seconds`` is the wall-clock time for the API call.
    ``num_speakers`` is the number of distinct speakers Deepgram identified.
    """

    utterances: List[Utterance]
    full_text: str
    lang: str
    asr_seconds: float
    num_speakers: int = 0
    speaker_labels: List[str] = field(default_factory=list)
    """Optional human-readable labels per speaker index, e.g. ["Peter", "Other"].
    Populated by the enrichment layer, not by Deepgram."""


def _deepgram_api_key() -> str:
    return (voice_config.__dict__.get("deepgram_api_key", lambda: "")() or "").strip()


def _deepgram_model() -> str:
    import os
    return (os.getenv("DEEPGRAM_MODEL") or "nova-3").strip()


def _build_params(language: Optional[str]) -> dict:
    params: dict = {
        "model": _deepgram_model(),
        "diarize": "true",
        "punctuate": "true",
        "utterances": "true",
        "smart_format": "true",
    }
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"
    return params


class DeepgramProvider:
    """Deepgram Nova-3 STT with speaker diarization.

    Implements the standard ``TranscriptionProvider`` protocol (returns
    ``RawTranscript``) AND exposes ``transcribe_diarized()`` for callers
    that want per-utterance speaker turns.
    """

    name: str = "deepgram"

    def __init__(self, *, api_key: Optional[str] = None, timeout: float = 180.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def _resolve_key(self) -> str:
        key = (self._api_key or _deepgram_api_key()).strip()
        if not key:
            raise ValueError("DEEPGRAM_API_KEY is not set")
        return key

    # ------------------------------------------------------------------ #
    #  Protocol-compatible method                                           #
    # ------------------------------------------------------------------ #

    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,  # ignored — Deepgram doesn't use it
    ) -> RawTranscript:
        """Standard pipeline interface — returns flat text only."""
        result = await self.transcribe_diarized(audio, content_type=content_type, language=language)
        return RawTranscript(text=result.full_text, lang=result.lang, asr_seconds=result.asr_seconds)

    # ------------------------------------------------------------------ #
    #  Rich diarization method                                              #
    # ------------------------------------------------------------------ #

    async def transcribe_diarized(
        self,
        audio: bytes,
        *,
        content_type: str,
        language: Optional[str] = None,
    ) -> DiarizedTranscript:
        """Transcribe with full speaker diarization. Returns ``DiarizedTranscript``."""
        return await asyncio.to_thread(self._transcribe_diarized_sync, audio, content_type, language)

    def _transcribe_diarized_sync(
        self,
        audio: bytes,
        content_type: str,
        language: Optional[str],
    ) -> DiarizedTranscript:
        key = self._resolve_key()
        params = _build_params(language)

        t0 = time.time()
        mime = content_type or "audio/mpeg"

        with http_client_sync(timeout=self._timeout) as client:
            r = client.post(
                _DEEPGRAM_URL,
                params=params,
                headers={
                    "Authorization": f"Token {key}",
                    "Content-Type": mime,
                },
                content=audio,
            )
            if r.status_code >= 400:
                body = (r.text or "")[:500]
                logger.error("Deepgram STT HTTP %s | %s", r.status_code, body)
                r.raise_for_status()
            data = r.json()

        elapsed = time.time() - t0

        # Parse response
        results = (data or {}).get("results", {})
        channels = results.get("channels", [{}])
        channel = channels[0] if channels else {}
        alternatives = channel.get("alternatives", [{}])
        alt = alternatives[0] if alternatives else {}
        full_text = (alt.get("transcript") or "").strip()

        # Language detection
        metadata = (data or {}).get("metadata", {})
        detected_lang = ""
        if language:
            detected_lang = language
        else:
            model_info = metadata.get("model_info", {})
            detected_lang = next(iter(model_info.values()), {}).get("language", "") if model_info else ""

        # Build utterances from Deepgram's utterance-level results
        raw_utterances = results.get("utterances", [])
        utterances: List[Utterance] = []
        speaker_ids: set = set()

        for u in raw_utterances:
            speaker_id = int(u.get("speaker", 0))
            speaker_ids.add(speaker_id)
            utterances.append(
                Utterance(
                    speaker=speaker_id,
                    text=(u.get("transcript") or "").strip(),
                    start=float(u.get("start", 0.0)),
                    end=float(u.get("end", 0.0)),
                    confidence=float(u.get("confidence", 0.0)),
                )
            )

        # Fall back: if no utterances in response, build a single-speaker utterance
        if not utterances and full_text:
            utterances = [Utterance(speaker=0, text=full_text, start=0.0, end=0.0, confidence=1.0)]
            speaker_ids = {0}

        logger.info(
            "Deepgram diarized | %.1fs | %d utterances | %d speakers | lang=%s",
            elapsed,
            len(utterances),
            len(speaker_ids),
            detected_lang or "?",
        )

        return DiarizedTranscript(
            utterances=utterances,
            full_text=full_text,
            lang=detected_lang,
            asr_seconds=elapsed,
            num_speakers=len(speaker_ids),
        )
