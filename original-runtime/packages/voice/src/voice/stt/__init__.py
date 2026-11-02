"""STT provider registry + Protocol + dispatch helpers.

Every concrete STT backend (``voice.stt.groq.GroqProvider``,
``voice.stt.huggingface.HuggingFaceProvider``, ``voice.stt.local.LocalWhisperProvider``)
implements :class:`TranscriptionProvider`. The pipeline in ``voice.pipeline``
resolves ``"auto"`` via :func:`resolve_provider` and dispatches through
:data:`TRANSCRIPTION_PROVIDERS`.

Provider precedence for ``"auto"``:

1. Groq (if ``GROQ_API_KEY`` set) — cheapest + fastest.
2. HuggingFace (if ``HF_TOKEN`` / ``HUGGINGFACE_HUB_TOKEN`` set) — fallback cloud.
3. Local (always available) — runs CPU faster-whisper.

Explicit names (``"groq"``, ``"huggingface"``, ``"hf"``, ``"local"``) resolve to
themselves; cloud providers raise :class:`ValueError` if their credentials are
absent. ``"auto"`` never raises — it falls through to ``"local"``.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Protocol, runtime_checkable

from .. import config
from ..types import ProviderName, RawTranscript, ResolvedProvider

__all__ = [
    "TranscriptionProvider",
    "RawTranscript",
    "resolve_provider",
    "normalize_language",
    "TRANSCRIPTION_PROVIDERS",
    "get_provider",
]


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Contract every STT backend satisfies.

    Concrete implementations live in ``voice.stt.{groq,huggingface,local}``.
    The pipeline dispatcher wraps sync implementations in ``asyncio.to_thread``
    before calling; async-native implementations (e.g. Groq via
    ``httpx.AsyncClient``) may skip that step.
    """

    name: str
    """Provider name, matches the registry key ("groq" | "huggingface" | "local")."""

    async def transcribe(
        self,
        audio: bytes,
        *,
        content_type: str,
        language: Optional[str] = None,
        initial_prompt: Optional[str] = None,
    ) -> RawTranscript:
        """Run ASR on the given audio bytes. Pre-polish result."""
        ...


def normalize_language(hint: Optional[str]) -> Optional[str]:
    """Map language hint variants to a normalized form providers accept.

    - ``None``, ``"auto"``, ``"multilingual"``, ``"detect"``, ``"any"``, ``""``
      → ``None`` (multilingual / auto-detect).
    - Anything else → ``lowercase``, capped at 8 chars (Whisper hints are
      ISO-639-1, 2 chars; 8 leaves headroom for locale tags like ``"es-mx"``
      without allowing pathologically long inputs).
    """
    if hint is None:
        return None
    s = str(hint).strip().lower()
    if not s or s in ("auto", "multilingual", "detect", "any"):
        return None
    return s[:8]


def resolve_provider(requested: ProviderName) -> ResolvedProvider:
    """Resolve a requested provider name to a concrete backend.

    ``"auto"``:
        Groq if ``GROQ_API_KEY`` is set, else HuggingFace if ``HF_TOKEN``
        (or ``HUGGINGFACE_HUB_TOKEN``) is set, else ``"local"``. Never raises.

    ``"hf"`` / ``"huggingface"``:
        Returns ``"huggingface"``. Raises :class:`ValueError` if no HF token is set.

    ``"groq"``:
        Raises :class:`ValueError` if ``GROQ_API_KEY`` is not set.

    ``"local"``:
        Returns ``"local"`` (faster-whisper is always available, no creds needed).

    Raises:
        ValueError: unknown provider name, or explicit cloud provider without creds.
    """
    r = (requested or "auto").lower().strip()
    if r in ("hf", "huggingface"):
        if not config.hf_token():
            raise ValueError(
                "Hugging Face STT requested but HF_TOKEN / HUGGINGFACE_HUB_TOKEN is not set"
            )
        return "huggingface"
    if r == "groq":
        if not config.groq_api_key():
            raise ValueError("Groq STT requested but GROQ_API_KEY is not set")
        return "groq"
    if r == "local":
        return "local"
    if r == "deepgram":
        if not config.deepgram_api_key():
            raise ValueError("Deepgram STT requested but DEEPGRAM_API_KEY is not set")
        return "deepgram"
    if r == "auto":
        if config.deepgram_api_key():
            return "deepgram"
        if config.groq_api_key():
            return "groq"
        if config.hf_token():
            return "huggingface"
        return "local"
    raise ValueError(
        f"provider must be auto, local, groq, huggingface (hf), or deepgram; got {requested!r}"
    )


# Provider factory registry. Lazy-loaded so importing voice.stt doesn't pull in
# faster-whisper or huggingface_hub unless we actually need them.
TRANSCRIPTION_PROVIDERS: Dict[ResolvedProvider, Callable[[], TranscriptionProvider]] = {}


def _register(name: ResolvedProvider, factory: Callable[[], TranscriptionProvider]) -> None:
    TRANSCRIPTION_PROVIDERS[name] = factory


def _groq_factory() -> TranscriptionProvider:
    from .groq import GroqProvider

    return GroqProvider()


def _hf_factory() -> TranscriptionProvider:
    from .huggingface import HuggingFaceProvider

    return HuggingFaceProvider()


def _local_factory() -> TranscriptionProvider:
    from .local import LocalWhisperProvider

    return LocalWhisperProvider()


def _deepgram_factory() -> TranscriptionProvider:
    from .deepgram import DeepgramProvider

    return DeepgramProvider()


_register("groq", _groq_factory)
_register("huggingface", _hf_factory)
_register("local", _local_factory)
_register("deepgram", _deepgram_factory)


def get_provider(name: ResolvedProvider) -> TranscriptionProvider:
    """Instantiate the provider registered under ``name``.

    Provider instances are *not* cached here — callers that want singleton
    behaviour should cache at a higher level. Local provider caches its
    Whisper model via ``@lru_cache`` so reinstantiation is cheap.
    """
    factory = TRANSCRIPTION_PROVIDERS.get(name)
    if factory is None:
        raise ValueError(f"No TranscriptionProvider registered for {name!r}")
    return factory()
