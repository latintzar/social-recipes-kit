"""AWS Polly TTS provider — stub for Phase 3.5b.

API shape locked; real implementation lands in Phase 4. See
``internal/refactor/packages/voice/design.md`` §N3.
"""

from __future__ import annotations

from typing import Optional

from ..types import SynthesisResult

__all__ = ["PollyProvider"]


class PollyProvider:
    """AWS Polly ``SynthesizeSpeech`` wrapper. Phase 3.5b: stub."""

    name: str = "polly"

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        language: Optional[str] = None,
        output_format: str = "mp3",
    ) -> SynthesisResult:
        raise NotImplementedError(
            "voice.tts.polly.PollyProvider stubbed in Phase 3.5b — "
            "real impl via core.aws.leo_boto3_client in Phase 4."
        )
