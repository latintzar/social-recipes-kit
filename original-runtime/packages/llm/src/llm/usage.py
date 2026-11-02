"""
llm.usage — observer registry for cost / latency / audit
=========================================================

D8 + D16 realisation: every :func:`llm.chat` / :func:`llm.chat_sync`
call emits its :class:`~llm.providers.base.ChatResponse` through
:func:`record_usage`. Observers subscribed via :func:`register_observer`
receive the response object.

Default observer logs model, tokens, and est cost via :mod:`logging`.
Downstream services can:
- replace the default with a Datadog/PostHog/Sentry sink; or
- keep the default and add their own in parallel.

Observers are called in registration order. Exceptions are caught and
logged — one bad observer doesn't prevent the others from running.
"""

from __future__ import annotations

import logging
from typing import Callable, List

from llm.providers.base import ChatResponse

_log = logging.getLogger(__name__)

Observer = Callable[[ChatResponse], None]

_observers: List[Observer] = []


def _default_log_observer(resp: ChatResponse) -> None:
    """Log model, tokens, est cost at INFO. Installed by default."""
    if not resp.usage:
        _log.info("llm.usage: model=%s (no usage reported)", resp.model)
        return
    u = resp.usage
    cost = u.cost_usd
    _log.info(
        "llm.usage: model=%s in=%d out=%d total=%d est_cost_usd=%.6f%s",
        resp.model or u.model,
        u.input_tokens,
        u.output_tokens,
        u.total_tokens,
        cost,
        " (fallback)" if resp.used_fallback else "",
    )


_observers.append(_default_log_observer)


def register_observer(observer: Observer) -> None:
    """Subscribe *observer* to every :func:`record_usage` event.

    Observers are called in registration order. Idempotent — registering
    the same function twice results in two invocations per event.
    """
    _observers.append(observer)


def unregister_observer(observer: Observer) -> None:
    """Remove *observer* from the subscription list.

    Silent no-op if *observer* isn't registered.
    """
    try:
        _observers.remove(observer)
    except ValueError:
        pass


def record_usage(resp: ChatResponse) -> None:
    """Fan *resp* out to every registered observer.

    Exceptions inside observers are caught + logged — never propagated.
    """
    for obs in list(_observers):
        try:
            obs(resp)
        except Exception:  # noqa: BLE001 — one bad observer can't wreck others
            _log.exception("llm.usage: observer %r raised", obs)


__all__ = ["Observer", "record_usage", "register_observer", "unregister_observer"]
