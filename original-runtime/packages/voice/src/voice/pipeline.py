"""Primary entry point for voice → text.

``transcribe()`` resolves the provider, bounds concurrency via the module-level
semaphore, runs ASR, optionally applies Bedrock polish, and returns a
:class:`TranscriptResult`.

Invariants carried over from ``shared.voice.transcribe``:

- Provider precedence for ``"auto"``: Groq > HF > local. No cross-provider
  retry on HTTP failure.
- Polish is on by default (product choice), gated AND by env flag + per-call
  kwarg.
- Polish never fails the transcription path: on any Bedrock error,
  ``text == raw_text``, ``polished == False``.

New in Wave 12:

- ``VOICE_CONCURRENCY`` semaphore bounds concurrent calls.
- ``initial_prompt`` kwarg biases decoding (local + Groq only; HF ignores).
- Transcript content logs at DEBUG by default (PII hygiene). Set
  ``VOICE_LOG_CONTENT=1`` to restore the pre-Wave-12 INFO-level behaviour.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import polish as polish_module
from .logging import log_transcript
from .resilience import voice_semaphore
from .stt import get_provider, normalize_language, resolve_provider
from .types import ProviderName, TranscriptResult

logger = logging.getLogger(__name__)

__all__ = ["transcribe"]


async def transcribe(
    audio: bytes,
    *,
    content_type: str,
    language: Optional[str] = None,
    provider: ProviderName = "auto",
    polish: bool = True,
    initial_prompt: Optional[str] = None,
) -> TranscriptResult:
    """Transcribe audio bytes to text.

    See ``packages/voice/README.md`` for prose + examples. Key behaviours:

    - ``provider="auto"`` never raises — falls through to local when no cloud
      creds are present. Explicit cloud names (``"groq"``, ``"huggingface"``)
      raise :class:`ValueError` if their credentials are absent.
    - ``polish=True`` tries Bedrock; on any failure, ``text == raw_text``
      and ``polished == False``.
    - ``initial_prompt`` biases Groq + local Whisper decoders. HuggingFace
      ignores it with a DEBUG log.

    Raises:
        ValueError: on an explicit provider request missing its credentials.
        RuntimeError: on ffmpeg failure in the local path.
        httpx.HTTPStatusError: if Groq returns non-2xx (auto does NOT retry
                               across providers — same as pre-Wave-12).
    """
    resolved = resolve_provider(provider)
    lang_hint = normalize_language(language)
    prov = get_provider(resolved)

    async with voice_semaphore():
        raw = await prov.transcribe(
            audio,
            content_type=content_type,
            language=lang_hint,
            initial_prompt=initial_prompt,
        )

    log_transcript(
        logger,
        event="voice.transcribe ok",
        provider=resolved,
        lang=raw.lang,
        asr_seconds=raw.asr_seconds,
        raw=raw.text,
    )

    final = raw.text
    did_polish = False
    if polish and raw.text:
        # `polish` module's public helper is never-raises; no try/except needed.
        out = await polish_module.polish(raw.text)
        if out and out != raw.text:
            final = out
            did_polish = True

    return TranscriptResult(
        text=final,
        raw_text=raw.text,
        lang=raw.lang,
        provider=resolved,
        asr_seconds=raw.asr_seconds,
        polished=did_polish,
    )
