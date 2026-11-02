"""ElevenLabs TTS provider — stub for Phase 3.5b.

Phase 4 fills in the real implementation via the ``elevenlabs`` SDK, reading
``ELEVENLABS_API_KEY`` from :func:`voice.config.elevenlabs_api_key`.
"""

from __future__ import annotations

from typing import Optional

from ..types import SynthesisResult

__all__ = ["ElevenLabsProvider"]


class ElevenLabsProvider:
    """ElevenLabs multilingual v2 wrapper. Phase 3.5b: stub."""

    name: str = "elevenlabs"

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        language: Optional[str] = None,
        output_format: str = "mp3",
    ) -> SynthesisResult:
        raise NotImplementedError(
            "voice.tts.elevenlabs.ElevenLabsProvider stubbed in Phase 3.5b — "
            "real impl via the elevenlabs SDK in Phase 4."
        )
