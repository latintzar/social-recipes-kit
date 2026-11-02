"""PII-aware logging helpers for the voice package.

Transcript content is user-provided speech — likely PII (names, phones, addresses,
health/financial info). We log full transcripts at DEBUG by default; setting
``VOICE_LOG_CONTENT=1`` restores the pre-Wave-12 behaviour of INFO-level content
logging, useful for dev/debugging only.

All voice modules route transcript logging through these helpers so the policy
stays centralized.
"""

from __future__ import annotations

import logging

from . import config

__all__ = [
    "log_transcript",
    "log_polish",
]


def _content_level() -> int:
    """INFO if ``VOICE_LOG_CONTENT=1``, else DEBUG.

    Read fresh every call so tests can toggle via :func:`config.voice_log_content`
    monkey-patching without reimporting this module."""
    return logging.INFO if config.voice_log_content() else logging.DEBUG


def log_transcript(
    logger: logging.Logger,
    *,
    event: str,
    provider: str,
    lang: str,
    asr_seconds: float,
    raw: str,
    preview_chars: int = 80,
) -> None:
    """Log an ASR result.

    Metadata (event, provider, lang, duration, character count) always at INFO.
    Transcript preview at content level (DEBUG by default, INFO when
    ``VOICE_LOG_CONTENT=1``).
    """
    logger.info(
        "%s | provider=%s lang=%s asr=%.2fs chars=%d",
        event,
        provider,
        lang or "?",
        asr_seconds,
        len(raw or ""),
    )
    if raw:
        preview = raw[:preview_chars]
        logger.log(_content_level(), "%s preview | %s", event, preview)


def log_polish(
    logger: logging.Logger,
    *,
    event: str,
    model_id: str,
    raw_chars: int,
    out_chars: int,
    input_tokens: int | str | None = None,
    output_tokens: int | str | None = None,
    out: str = "",
    preview_chars: int = 80,
) -> None:
    """Log a polish invocation.

    Metadata (event, model, char counts, token usage) always at INFO. Polished
    content preview at content level (DEBUG by default, INFO when
    ``VOICE_LOG_CONTENT=1``).
    """
    logger.info(
        "%s | model=%s raw_chars=%d out_chars=%d in_tok=%s out_tok=%s",
        event,
        model_id,
        raw_chars,
        out_chars,
        input_tokens if input_tokens is not None else "?",
        output_tokens if output_tokens is not None else "?",
    )
    if out:
        logger.log(_content_level(), "%s preview | %s", event, out[:preview_chars])
