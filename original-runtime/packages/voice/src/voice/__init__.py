"""Leo voice — transcription, polish, language detection, audio, TTS.

Single public surface curated against
``internal/refactor/packages/voice/api.py``. Every symbol listed in
``__all__`` is either (a) a top-level API function, (b) a dataclass/type
callers rely on, or (c) a Protocol for advanced dispatcher use.

Top-level usage:

    from voice import transcribe, TranscriptResult, polish_sync

    result = await transcribe(audio_bytes, content_type="audio/ogg")
    cleaned = polish_sync(result.raw_text)

See the package ``README.md`` for three worked examples covering the
transcribe, synthesize, and wav-tempfile paths.
"""

from __future__ import annotations

from .audio import ext_for_content_type, to_wav, wav_tempfile
from .detect import detect_language
from .pipeline import transcribe
from .polish import POLISH_SYSTEM, polish, polish_sync
from .resilience import voice_semaphore
from .stt import (
    RawTranscript,
    TranscriptionProvider,
    normalize_language,
    resolve_provider,
)
from .stt.deepgram import DeepgramProvider, DiarizedTranscript, Utterance
from .tts import SynthesisProvider, list_voices, synthesize
from .types import (
    AudioQuality,
    ProviderName,
    ResolvedProvider,
    SynthesisResult,
    TranscriptResult,
)

__all__ = [
    "transcribe",
    "TranscriptResult",
    "polish",
    "polish_sync",
    "POLISH_SYSTEM",
    "to_wav",
    "wav_tempfile",
    "ext_for_content_type",
    "detect_language",
    "TranscriptionProvider",
    "RawTranscript",
    "resolve_provider",
    "normalize_language",
    "synthesize",
    "SynthesisResult",
    "SynthesisProvider",
    "list_voices",
    "voice_semaphore",
    "ProviderName",
    "ResolvedProvider",
    "AudioQuality",
    "DeepgramProvider",
    "DiarizedTranscript",
    "Utterance",
]
