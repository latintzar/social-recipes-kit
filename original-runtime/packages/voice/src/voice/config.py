"""Environment-variable readers for the voice package.

Every env var the voice package reads flows through one of these thin
functions. Tests monkey-patch the function; production code never touches
``os.getenv`` directly. This is D2 from the Wave 12 design.

Why indirect? Three reasons:

1. **Test isolation.** ``monkeypatch.setattr("voice.config.groq_api_key", lambda: "fake")``
   is cleaner than ``monkeypatch.setenv`` because it doesn't leak into child
   processes or affect other modules that also read ``GROQ_API_KEY``.
2. **No ``config.settings`` dep.** ``unmigrated/backend/config.py`` isn't part
   of the new tree; voice must be self-sufficient.
3. **Single source of defaults.** Default values live here once. Callers never
   duplicate them.
"""

from __future__ import annotations

import os

__all__ = [
    "groq_api_key",
    "groq_whisper_model",
    "hf_token",
    "hf_whisper_model",
    "whisper_model_name",
    "whisper_device",
    "whisper_compute_type",
    "whisper_cpu_threads",
    "whisper_beam_size",
    "voice_concurrency",
    "polish_enabled",
    "polish_model_id",
    "polish_api_style",
    "voice_log_content",
    "aws_region",
    "elevenlabs_api_key",
    "deepgram_api_key",
]


def groq_api_key() -> str:
    return (os.getenv("GROQ_API_KEY") or "").strip()


def groq_whisper_model() -> str:
    return (os.getenv("GROQ_WHISPER_MODEL") or "whisper-large-v3-turbo").strip()


def hf_token() -> str:
    return (
        os.getenv("HF_TOKEN")
        or os.getenv("HUGGINGFACE_HUB_TOKEN")
        or ""
    ).strip()


def hf_whisper_model() -> str:
    return (os.getenv("HF_WHISPER_MODEL") or "openai/whisper-large-v3").strip()


def whisper_model_name() -> str:
    """Model ID for local faster-whisper. Default: ``distil-large-v3`` — distilled
    Whisper large-v3 tuned for fast-whisper; ~2× faster on CPU than the full
    large-v3 with similar quality for short voice notes."""
    return (os.getenv("WHISPER_MODEL") or "distil-large-v3").strip()


def whisper_device() -> str:
    """Device for faster-whisper: ``cpu`` | ``cuda``. Default ``cpu``.

    Set ``WHISPER_DEVICE=cuda`` on GPU hosts (significant speedup on long audio)."""
    return (os.getenv("WHISPER_DEVICE") or "cpu").strip().lower()


def whisper_compute_type() -> str:
    """Compute type for faster-whisper. Default ``int8`` on CPU; override to
    ``float16`` on GPU for better quality."""
    default = "float16" if whisper_device() == "cuda" else "int8"
    return (os.getenv("WHISPER_COMPUTE_TYPE") or default).strip()


def whisper_cpu_threads() -> int:
    """CPU threads for faster-whisper. ``0`` = let CTranslate2 decide. Invalid
    inputs warn-and-default to 0 (symmetric with :func:`whisper_beam_size`
    — previously only beam size warned)."""
    raw = (os.getenv("WHISPER_CPU_THREADS") or "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def whisper_beam_size() -> int:
    """Beam size for faster-whisper. Clamped to [1, 10]. Default 5.
    Invalid inputs warn-and-default to 5."""
    raw = (os.getenv("WHISPER_BEAM_SIZE") or "5").strip()
    try:
        beam = int(raw)
    except ValueError:
        beam = 5
    return max(1, min(beam, 10))


def voice_concurrency() -> int:
    """Max concurrent ``transcribe()`` calls. Default 8. Min 1."""
    raw = (os.getenv("VOICE_CONCURRENCY") or "8").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def polish_enabled() -> bool:
    """Whether the Bedrock polish step runs by default. Default ``True``.

    Per-call ``polish=`` kwarg can override for the opposite direction."""
    flag = (os.getenv("TRANSCRIPT_POLISH_ENABLED") or "true").strip().lower()
    return flag in ("1", "true", "yes", "on")


def polish_model_id() -> str:
    """Bedrock model ID for polish. Default Claude Haiku 4.5 (cross-region
    inference profile)."""
    return (
        os.getenv("TRANSCRIPT_POLISH_MODEL")
        or "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    ).strip()


def polish_api_style() -> str:
    """Force ``anthropic`` (Messages API) or ``converse`` (Bedrock Converse) routing.

    Default: ``""`` → auto-detect from the model ID namespace (D10).
    Override via ``POLISH_API_STYLE=anthropic|converse`` when a namespace-based
    inference is wrong (e.g. custom fine-tuned models)."""
    return (os.getenv("POLISH_API_STYLE") or "").strip().lower()


def voice_log_content() -> bool:
    """Whether to log the first 80 chars of transcripts at INFO (PII risk).

    Default ``False``. Set ``VOICE_LOG_CONTENT=1`` in dev to restore pre-Wave-12
    behaviour. Production should leave this off — transcripts are by definition
    user-provided speech."""
    return (os.getenv("VOICE_LOG_CONTENT") or "0").strip().lower() in ("1", "true", "yes", "on")


def aws_region() -> str:
    return (os.getenv("AWS_REGION") or "us-east-1").strip()


def elevenlabs_api_key() -> str:
    return (os.getenv("ELEVENLABS_API_KEY") or "").strip()


def deepgram_api_key() -> str:
    return (os.getenv("DEEPGRAM_API_KEY") or "").strip()
