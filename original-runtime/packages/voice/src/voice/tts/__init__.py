"""Text-to-speech dispatcher + Protocol + stubs.

Phase 3.5b (this wave): API shape is locked, all runtime calls raise
:class:`NotImplementedError` with a clear pointer to Phase 4. This lets
callers write against the stable shape today.

Phase 4: AWS Polly + ElevenLabs providers fill in without API churn. Dispatch
rules:

- ``quality="fast"`` → Polly neural (cheap, adequate for transactional replies).
- ``quality="premium"`` → ElevenLabs multilingual v2 (brand/emotional replies),
  with Polly fallback on ElevenLabs quota/failure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from ..types import AudioQuality, SynthesisResult
from .voices import VOICE_REGISTRY, VoiceMapping

__all__ = [
    "synthesize",
    "SynthesisProvider",
    "SynthesisResult",
    "list_voices",
    "VOICE_REGISTRY",
    "VoiceMapping",
]


@runtime_checkable
class SynthesisProvider(Protocol):
    """Contract every TTS backend satisfies.

    Phase 3.5b: implementations raise :class:`NotImplementedError`. Phase 4
    fills them in without the Protocol changing.
    """

    name: str
    """Provider name — ``"polly"`` | ``"elevenlabs"``."""

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        language: Optional[str] = None,
        output_format: str = "mp3",
    ) -> SynthesisResult:
        """Synthesize ``text`` into audio bytes."""
        ...


async def synthesize(
    text: str,
    *,
    voice_id: str = "leo-es",
    quality: AudioQuality = "fast",
    language: Optional[str] = None,
    output_format: str = "mp3",
) -> SynthesisResult:
    """Synthesize text to audio. Phase 3.5b stub — raises NotImplementedError."""
    raise NotImplementedError(
        "voice.synthesize is stubbed in Phase 3.5b. API shape is locked; "
        "AWS Polly + ElevenLabs implementations land in Phase 4. "
        "See internal/refactor/packages/voice/design.md §N3."
    )


def list_voices(language: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return registered canonical voices, optionally filtered by language."""
    out: List[Dict[str, Any]] = []
    for canonical, mapping in VOICE_REGISTRY.items():
        if language and mapping.language != language:
            continue
        out.append(
            {
                "canonical": canonical,
                "language": mapping.language,
                "polly_voice_id": mapping.polly_voice_id,
                "polly_engine": mapping.polly_engine,
                "elevenlabs_voice_id": mapping.elevenlabs_voice_id,
                "description": mapping.description,
            }
        )
    return out
