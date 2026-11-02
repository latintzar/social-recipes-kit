"""
llm.providers.fallback — named chain resolution + retry-with-fallback runner
=============================================================================

D5 realisation: callers pass ``fallback=[model_id, ...]`` to
:func:`llm.chat`, or a named chain string that resolves here.

Chain semantics:
- Try the primary.
- On :exc:`~llm.errors.ProviderUnavailable` OR
  :exc:`~llm.errors.RateLimitError`, advance to the next model.
- On :exc:`~llm.errors.ContextWindowExceeded`, advance only if the next
  model has a larger context window (otherwise re-raise — retrying in
  a smaller window is pointless).
- On :exc:`~llm.errors.InvalidResponseShape` or
  :exc:`~llm.errors.ModelNotFound`, re-raise immediately (not
  retryable).

Returns a :class:`~llm.providers.base.ChatResponse` with
``used_fallback`` set and ``attempt_count`` reflecting the number of
providers tried.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional

from llm.errors import (
    ContextWindowExceeded,
    InvalidResponseShape,
    ModelNotFound,
    ProviderUnavailable,
    RateLimitError,
)
from llm.models.registry import resolve_model
from llm.models.tiers import (
    DEFAULT_FAST,
    DEFAULT_LONG_CTX,
    DEFAULT_REASONING,
    DEFAULT_SMART,
    DEFAULT_VISION,
)
from llm.providers.base import ChatResponse

_log = logging.getLogger(__name__)


def resolve_chain(name_or_list: str | List[str]) -> List[str]:
    """Resolve a named chain or raw list into an ordered model id list.

    Named chains::

        "chat.balanced"    → [smart, fast, long_ctx]
        "chat.cheap"       → [fast, cheap]
        "chat.reasoning"   → [reasoning, smart]
        "vision.primary"   → [vision, long_ctx]

    A list passed through is validated: every element must be a known
    model id or alias. Raises :exc:`~llm.errors.ModelNotFound` on
    invalid entries.
    """
    if isinstance(name_or_list, list):
        return [resolve_model(m).id for m in name_or_list]

    NAMED: dict[str, List[str]] = {
        "chat.balanced": [DEFAULT_SMART, DEFAULT_FAST, DEFAULT_LONG_CTX],
        "chat.cheap": [DEFAULT_FAST],
        "chat.reasoning": [DEFAULT_REASONING, DEFAULT_SMART],
        "vision.primary": [DEFAULT_VISION, DEFAULT_LONG_CTX],
    }
    chain = NAMED.get(name_or_list)
    if chain is None:
        raise ModelNotFound(
            f"unknown named chain: {name_or_list!r}. "
            f"Known: {sorted(NAMED.keys())}",
            model=name_or_list,
        )
    return chain


async def run_with_fallback(
    primary: str,
    fallback: List[str],
    *,
    invoke: Callable[[str], Awaitable[ChatResponse]],
) -> ChatResponse:
    """Run *invoke(model_id)* against *primary*, falling through
    *fallback* on retryable errors.

    *invoke* is the provider-dispatching call — typically a closure
    over messages/tools/format captured by :func:`llm.chat`.

    Returns the first successful :class:`~llm.providers.base.ChatResponse`,
    annotated with ``used_fallback`` / ``fallback_reason`` / ``attempt_count``.
    """
    chain: List[str] = [primary, *fallback]
    last_exc: Optional[Exception] = None
    attempt_count = 0

    for idx, model_id in enumerate(chain):
        attempt_count += 1
        try:
            resp = await invoke(model_id)
            if idx > 0:
                return ChatResponse(
                    text=resp.text,
                    tool_calls=resp.tool_calls,
                    usage=resp.usage,
                    finish_reason=resp.finish_reason,
                    model=resp.model,
                    used_fallback=True,
                    fallback_reason=type(last_exc).__name__ if last_exc else None,
                    attempt_count=attempt_count,
                    raw=resp.raw,
                )
            return resp
        except (ProviderUnavailable, RateLimitError) as exc:
            _log.warning(
                "llm.fallback: %s on %s, advancing", type(exc).__name__, model_id
            )
            last_exc = exc
            continue
        except ContextWindowExceeded as exc:
            last_exc = exc
            next_id = chain[idx + 1] if idx + 1 < len(chain) else None
            if next_id is None:
                raise
            curr_ctx = resolve_model(model_id).context_window
            next_ctx = resolve_model(next_id).context_window
            if next_ctx <= curr_ctx:
                raise
            _log.warning(
                "llm.fallback: ContextWindowExceeded on %s, advancing to larger %s",
                model_id,
                next_id,
            )
            continue
        except (InvalidResponseShape, ModelNotFound):
            raise

    assert last_exc is not None
    raise last_exc


__all__ = ["resolve_chain", "run_with_fallback"]
