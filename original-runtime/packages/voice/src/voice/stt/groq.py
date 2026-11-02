"""Groq OpenAI-compatible audio transcriptions provider.

Uses ``core.http.http_client_sync`` + ``/openai/v1/audio/transcriptions``. Groq
accepts the ``prompt`` form field as a vocabulary bias (the same semantics as
OpenAI's Whisper API), so ``initial_prompt`` flows through.

Timeout is generous (120s) — large WhatsApp voice notes can take 30+s on Groq's
slower regions. Provider never retries on HTTP failure; the pipeline surface
is single-attempt per voice note, matching the original ``shared.voice``
invariant (no cross-provider retry).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from core import http_client_sync

from .. import config
from ..audio import ext_for_content_type
from ..types import RawTranscript

logger = logging.getLogger(__name__)

__all__ = ["GroqProvider"]

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _filename_for_audio(content_type: str) -> str:
    """Groq infers codec from filename extension (Whisper API convention).

    Use our canonical MIME → extension mapping, then prefix with
    ``"recording."`` to give the file a stable name the API accepts.
    """
    return f"recording.{ext_for_content_type(content_type)}"


class GroqProvider:
    """Concrete :class:`TranscriptionProvider` for Groq's Whisper endpoint.

    Constructor is dependency-injection-friendly — pass explicit ``api_key`` /
    ``model_name`` / ``timeout`` for testing; defaults read from env on first
    use. Instances are cheap (no warmup).
    """

    name: str = "groq"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._timeout = timeout

    def _resolve_api_key(self) -> str:
        key = (self._api_key or config.groq_api_key() or "").strip()
        if not key:
            raise ValueError("GROQ_API_KEY is not set")
        return key

    def _resolve_model(self) -> str:
        return (self._model_name or config.groq_whisper_model() or "whisper-large-v3-turbo").strip()

    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> RawTranscript:
        """POST audio bytes to Groq and parse the response.

        ``language`` is passed as the ``language`` form field (already normalized
        by the caller). ``initial_prompt`` is passed as the ``prompt`` form field.

        Runs the HTTP call in a thread via ``asyncio.to_thread`` so the outer
        pipeline doesn't block the event loop on a slow cloud round-trip.
        """
        import asyncio

        return await asyncio.to_thread(self._transcribe_sync, audio, content_type, language, initial_prompt)

    def _transcribe_sync(
        self,
        audio: bytes,
        content_type: str,
        language: Optional[str],
        initial_prompt: Optional[str],
    ) -> RawTranscript:
        key = self._resolve_api_key()
        model = self._resolve_model()

        t0 = time.time()
        name = _filename_for_audio(content_type)
        mime = content_type or "application/octet-stream"

        data: dict[str, str] = {"model": model}
        if language:
            data["language"] = language
        if initial_prompt:
            # Groq accepts `prompt` as the OpenAI-compatible vocabulary bias.
            data["prompt"] = initial_prompt

        with http_client_sync(timeout=self._timeout) as client:
            r = client.post(
                _GROQ_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (name, audio, mime)},
                data=data,
            )
            if r.status_code >= 400:
                body = (r.text or "")[:500]
                logger.error("Groq STT HTTP %s | %s", r.status_code, body)
                r.raise_for_status()
            body = r.json()
            text = ((body or {}).get("text") or "").strip()
            lang_raw = (body or {}).get("language")
            lang = lang_raw.strip() if isinstance(lang_raw, str) else ""

        return RawTranscript(text=text, lang=lang, asr_seconds=time.time() - t0)
