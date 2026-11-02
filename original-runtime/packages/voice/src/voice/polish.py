"""LLM post-polish over raw ASR output.

Runs a Claude Haiku-tier "voice fidelity" prompt via :func:`llm.chat` to
turn run-on ASR output into readable text while preserving the speaker's
exact phrasing. Never raises — on any failure, returns the raw text
unchanged.

Phase 5 Wave 7 recalibration: this module used to call Bedrock directly
(``invoke_model`` / ``converse``). Now it routes through
:func:`llm.chat`, which means:

* Same Leo-billing-discipline as every other Bedrock call (via
  :mod:`core.aws`).
* Model dispatch is provider-agnostic — swap ``BEDROCK_CLAUDE_HAIKU``
  for an OpenRouter or Gemini model ID and ``llm.chat`` handles the
  routing.
* No more hand-rolled Anthropic-vs-Converse API branching here; that
  lives in :mod:`llm.providers.bedrock` once, for everyone.

The ``POLISH_SYSTEM`` prompt is product-specified — the "voice fidelity"
variant matches Wispr-Flow-style dictation polish. Exposed as a module
attribute for A/B testing (pass a custom system via ``system_prompt=``);
not designed for per-env configurability (D14 from the Wave 12 design).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from llm import Message, chat_sync

from . import config, logging as voice_logging

logger = logging.getLogger(__name__)

__all__ = [
    "POLISH_SYSTEM",
    "polish",
    "polish_sync",
]


POLISH_SYSTEM = """You turn raw speech-to-text into readable text the speaker will recognize instantly when they read it back.

Voice fidelity (critical — same trick as best dictation products):
- People have short-term memory of the *exact words* they just said. Keep their phrasing, word order, and informal tone whenever it still makes sense. Do not rewrite into stiff or "essay" prose.
- Prefer light-touch edits: punctuation, sentence breaks, obvious typos/homophones from ASR. If a phrase is colloquial, emotional, or quirky but clear, leave it.
- Do not swap synonyms for "better" writing unless the original word is clearly a transcription error. Same meaning + same vibe > literary polish.

Formatting:
- Use full sentences and proper punctuation (periods, commas, question marks) so it reads cleanly at a glance.
- Remove filler (um, uh, you know) only when it adds no meaning; keep emphasis or hedges if they matter ("I think", "maybe", "honestly").
- Preserve every fact, number, measurement, name, place, trade term, and intent. Do not add or infer information.
- Keep the same language(s) as the source (including code-switching).

Output only the cleaned transcript. No title, no markdown fences, no commentary."""


def polish_sync(
    raw: str,
    *,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """Return lightly-edited transcript from the polish model.

    Never raises. On any failure (LLM unreachable, model unavailable,
    empty output, env flag off, empty input) returns ``raw`` unchanged.
    """
    raw = (raw or "").strip()
    if not raw:
        return raw

    if not config.polish_enabled():
        logger.info("Transcript polish skipped (TRANSCRIPT_POLISH_ENABLED off)")
        return raw

    resolved_model = (model_id or config.polish_model_id()).strip()
    if not resolved_model:
        return raw

    resolved_system = system_prompt if system_prompt is not None else POLISH_SYSTEM
    user_text = "Raw automatic transcription (from Whisper):\n\n" + raw.strip()

    messages = [
        Message(role="system", content=resolved_system),
        Message(role="user", content=user_text),
    ]

    try:
        response = chat_sync(
            messages,
            model=resolved_model,
            temperature=0.15,
            max_tokens=4096,
        )
    except Exception as exc:
        logger.warning(
            "Transcript polish failed; using raw ASR | model=%s err=%s",
            resolved_model,
            exc,
            exc_info=True,
        )
        return raw

    out = (response.text or "").strip()
    usage = response.usage
    voice_logging.log_polish(
        logger,
        event="polish ok",
        model_id=resolved_model,
        raw_chars=len(raw),
        out_chars=len(out),
        input_tokens=getattr(usage, "input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None),
        out=out,
    )

    if not out:
        logger.warning("Transcript polish returned empty; using raw ASR")
        return raw
    return out


async def polish(
    raw: str,
    *,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """Async counterpart to :func:`polish_sync`.

    Runs :func:`polish_sync` in a thread so a slow LLM round-trip
    doesn't block the event loop. Same never-raises contract.
    """
    return await asyncio.to_thread(polish_sync, raw, model_id=model_id, system_prompt=system_prompt)
