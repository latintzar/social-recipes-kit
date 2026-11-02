"""HuggingFace Inference API (Whisper) provider.

Uses ``huggingface_hub.InferenceClient.automatic_speech_recognition``. Unlike
Groq, HF doesn't report the detected language — we return ``lang=""`` and let
the pipeline fall back to :func:`voice.detect.detect_language` on the raw text
if a language was required.

Language hint threading: today's ``shared.stt_providers.transcribe_hf_sync``
drops the ``language`` kwarg entirely. Wave 12 adds it — we try passing
``language=`` to the HF client; if the installed version rejects it as unknown,
we retry without. This preserves parity for older installs while picking up the
hint where supported.

``initial_prompt`` is ignored with a DEBUG log — HF's ASR endpoint doesn't
expose the OpenAI ``prompt`` bias parameter.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .. import config
from ..types import RawTranscript

logger = logging.getLogger(__name__)

__all__ = ["HuggingFaceProvider"]


class HuggingFaceProvider:
    """Concrete :class:`TranscriptionProvider` for HF Inference ASR.

    HF client is constructed lazily on first call (the import cost isn't
    negligible). Instances are cheap.
    """

    name: str = "huggingface"

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        model_name: Optional[str] = None,
    ) -> None:
        self._token = token
        self._model_name = model_name
        self._client = None  # populated on first call

    def _resolve_token(self) -> str:
        t = (self._token or config.hf_token() or "").strip()
        if not t:
            raise ValueError("HF_TOKEN or HUGGINGFACE_HUB_TOKEN is not set")
        return t

    def _resolve_model(self) -> str:
        return (self._model_name or config.hf_whisper_model() or "openai/whisper-large-v3").strip()

    def _get_client(self):
        if self._client is None:
            from huggingface_hub import InferenceClient

            self._client = InferenceClient(token=self._resolve_token())
        return self._client

    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> RawTranscript:
        """Run ASR via HF Inference. See module docstring for the language /
        initial_prompt handling quirks."""
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
        # Exercise the credential check before creating the client so callers
        # get a clear error even when `token=""` is explicitly passed.
        self._resolve_token()
        if initial_prompt:
            logger.debug("HuggingFace ignores initial_prompt (len=%d)", len(initial_prompt))

        model = self._resolve_model()
        client = self._get_client()
        t0 = time.time()

        # HF ASR's `automatic_speech_recognition` signature varies by version.
        # Try to pass `language=` if supported; fall back to omitting it.
        out = None
        if language:
            try:
                out = client.automatic_speech_recognition(audio, model=model, language=language)
            except TypeError:
                logger.debug("HF client rejected language kwarg; retrying without")
                out = None
            except Exception as exc:
                # If the HF backend returns a model-level error about the
                # language hint, retry without the hint. Unknown errors bubble.
                if "language" in str(exc).lower():
                    logger.debug("HF ASR language hint rejected: %s", exc)
                    out = None
                else:
                    raise

        if out is None:
            out = client.automatic_speech_recognition(audio, model=model)

        text = (getattr(out, "text", "") or "").strip()
        return RawTranscript(text=text, lang="", asr_seconds=time.time() - t0)
