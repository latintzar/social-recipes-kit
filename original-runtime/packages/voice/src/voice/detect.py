"""Light-touch language detection for short transcripts.

Two-step strategy:

1. ``langdetect.detect()`` — deterministic via ``DetectorFactory.seed = 0``
   (the library is nondeterministic by default).
2. Fallback: count Spanish vs English stopwords. Tie → default.

Never raises; always returns an ISO-639-1 code (truncated to two chars). Used
by the pipeline when a provider doesn't report language (HuggingFace Inference
ASR notably doesn't) and by callers that want a cheap classification without
an extra LLM round-trip.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["detect_language"]


# Spanish and English stopword hit lists. Each word is framed with trailing
# spaces so substring matching doesn't false-positive inside longer tokens
# (e.g. "the" inside "theater"). Lightweight; not a real language identifier.
_ES_HITS = ("el ", "la ", "que ", "de ", "un ", "una ", "está ", "con ", "para ")
_EN_HITS = ("the ", "is ", "are ", "was ", "this ", "that ", "with ", "for ")


def detect_language(text: Optional[str], *, default: str = "es") -> str:
    """Return an ISO-639-1 code for ``text``.

    Empty / whitespace-only input → ``default`` (``"es"`` for Leo's primary market).
    Never raises.
    """
    if not text or not text.strip():
        return default

    try:
        from langdetect import DetectorFactory, detect

        DetectorFactory.seed = 0
        return detect(text)[:2]
    except Exception:
        pass

    t = text.lower()
    es_hits = sum(1 for w in _ES_HITS if w in t)
    en_hits = sum(1 for w in _EN_HITS if w in t)
    if es_hits > en_hits:
        return "es"
    if en_hits > es_hits:
        return "en"
    return default
