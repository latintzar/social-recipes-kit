"""Shared dataclasses & type aliases for the voice package.

These types are the stable public surface — every symbol listed here is
re-exported from ``voice.__init__``. See ``internal/refactor/packages/voice/api.py``
for the canonical API spec this file follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

__all__ = [
    "ProviderName",
    "ResolvedProvider",
    "AudioQuality",
    "RawTranscript",
    "TranscriptResult",
    "SynthesisResult",
]


ProviderName = Literal["auto", "groq", "huggingface", "hf", "local", "deepgram"]
"""Requested STT provider. `auto` picks the first available backend per
credential presence; `hf` is an alias for `huggingface`."""


ResolvedProvider = Literal["groq", "huggingface", "local", "deepgram"]
"""Concrete provider chosen by ``resolve_provider``. `auto` is never a resolved
value — it's the unresolved request form only."""


AudioQuality = Literal["fast", "premium"]
"""TTS quality tier. `fast` → Polly neural (~$16/1M chars); `premium` →
ElevenLabs multilingual v2 (~$300/1M chars) with Polly fallback."""


@dataclass
class RawTranscript:
    """Internal result returned by a ``TranscriptionProvider.transcribe()`` — pre-polish.

    The pipeline dispatcher wraps this into a :class:`TranscriptResult` after an
    optional polish pass. End-users don't see this type directly unless they're
    calling a provider's ``.transcribe()`` method directly (rare).
    """

    text: str
    """Raw ASR output, no LLM post-processing."""

    lang: str
    """Detected ISO-639-1 language code. ``''`` (empty) if the provider didn't
    detect one (HuggingFace Inference ASR behaves this way)."""

    asr_seconds: float
    """Wall-clock seconds spent on ASR alone (excludes polish + overhead)."""


@dataclass
class TranscriptResult:
    """Result of ``voice.transcribe()``. What end-callers almost always receive.

    Field semantics:

    - ``text``         — the final transcript. Polished if polish=True and polish
                         succeeded; otherwise equal to ``raw_text``.
    - ``raw_text``     — pre-polish ASR output. Always populated, even when polish=False.
    - ``lang``         — ISO-639-1 code (may be ``''`` if the provider didn't detect it).
    - ``provider``     — the actual provider used (``"groq"`` | ``"huggingface"`` | ``"local"``).
    - ``asr_seconds``  — ASR-only wall clock. Excludes polish.
    - ``polished``     — True iff polish ran AND changed the text. False when polish
                         was skipped, failed, or produced byte-identical output.
    """

    text: str
    raw_text: str
    lang: str
    provider: ResolvedProvider
    asr_seconds: float
    polished: bool = False


@dataclass
class SynthesisResult:
    """Result of ``voice.synthesize()``. Phase 3.5b: stubbed; Phase 4: implemented.

    Field semantics:

    - ``audio``             — raw audio bytes in the provider's native output format.
    - ``content_type``      — MIME type, e.g. ``"audio/mp3"``, ``"audio/pcm"``, ``"audio/ogg"``.
    - ``duration_seconds``  — estimated duration. Providers may not return this reliably;
                              fallback is a cheap client-side estimate from bytes × bitrate.
    - ``provider``          — ``"polly"`` | ``"elevenlabs"``.
    - ``voice_id``          — the canonical voice ID (e.g. ``"leo-es"``) resolved from the
                              registry. Not the provider-specific ID.
    - ``cost_cents``        — estimated cost of this synthesis call in US cents. Optional;
                              providers that don't surface cost leave it ``None``.
    """

    audio: bytes
    content_type: str
    duration_seconds: Optional[float]
    provider: Literal["polly", "elevenlabs"]
    voice_id: str
    cost_cents: Optional[float] = None
