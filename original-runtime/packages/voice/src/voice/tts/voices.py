"""Canonical voice-ID registry for TTS dispatch.

Each canonical name (e.g. ``"leo-es"``) maps to both a Polly voice ID and —
when Leo's voice clone is trained in Phase 4+ — an ElevenLabs voice ID.
Callers always pass canonical names; the dispatcher resolves them per-provider.

Passing a provider-specific ID (e.g. ``"Lupe"``, ``"EXAVITQu4vr4xnSDxMaL"``)
directly bypasses the registry with a DEBUG log, for escape-hatch use.

Phase 3.5b: registry seeded with one Spanish + one English canonical, both
Polly. ElevenLabs voice IDs are ``None`` until Leo's clone trains.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

__all__ = ["VoiceMapping", "VOICE_REGISTRY"]


@dataclass(frozen=True)
class VoiceMapping:
    """Canonical-name → provider-ID resolution with metadata."""

    language: str
    """ISO-639-1 code."""

    polly_voice_id: str
    """AWS Polly voice ID. Always populated; Polly is the Phase-3.5b-always-available backend."""

    polly_engine: str
    """Polly engine. ``"neural"`` for near-human quality; ``"standard"`` legacy."""

    elevenlabs_voice_id: Optional[str]
    """ElevenLabs voice ID. ``None`` until the Leo voice clone trains in Phase 4+."""

    description: str
    """Short human description, e.g. "Leo's canonical Spanish voice. Warm, friendly, 30-something."""


VOICE_REGISTRY: Dict[str, VoiceMapping] = {
    "leo-es": VoiceMapping(
        language="es",
        polly_voice_id="Lupe",
        polly_engine="neural",
        elevenlabs_voice_id=None,
        description="Leo's canonical Spanish voice. Warm, friendly, 30-something.",
    ),
    "leo-en": VoiceMapping(
        language="en",
        polly_voice_id="Joanna",
        polly_engine="neural",
        elevenlabs_voice_id=None,
        description="Leo's canonical English voice. Warm, friendly, 30-something.",
    ),
}
