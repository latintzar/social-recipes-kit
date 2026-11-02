"""Concurrency control for the voice package.

One module-level :class:`asyncio.Semaphore` bounds concurrent ``transcribe()``
calls. Created lazily on first use so module import doesn't require a running
event loop.

Sizing defaults to 8 (override via ``VOICE_CONCURRENCY``). ASR + polish dominate
the voice hot path — beyond ~8 concurrent calls on a single CPU host we saw
ffmpeg + CTranslate2 thrash; beyond ~16 on the cloud-provider path we saw Groq
rate limits trip. 8 is a comfortable cross-provider default.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from . import config

__all__ = ["voice_semaphore", "reset_semaphore_for_tests"]


_SEMAPHORE: Optional[asyncio.Semaphore] = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_LOCK = threading.Lock()


def voice_semaphore() -> asyncio.Semaphore:
    """Return the process-wide ``asyncio.Semaphore`` for voice concurrency.

    Size is ``VOICE_CONCURRENCY`` (default 8, min 1). Lazy creation — the first
    call inside an event loop creates the semaphore bound to that loop; later
    calls return the same instance.

    If the loop identity changes (e.g. tests spin up a new loop), a fresh
    semaphore is created for the new loop — otherwise we'd get
    ``RuntimeError: <Semaphore [...] > is bound to a different event loop``.
    """
    global _SEMAPHORE, _LOOP
    loop = asyncio.get_event_loop()
    with _LOCK:
        if _SEMAPHORE is None or _LOOP is not loop:
            size = max(1, config.voice_concurrency())
            _SEMAPHORE = asyncio.Semaphore(size)
            _LOOP = loop
        return _SEMAPHORE


def reset_semaphore_for_tests() -> None:
    """Drop the cached semaphore. Test-only helper.

    Not in ``__all__`` wrt the main public surface; exported only so per-test
    isolation fixtures can call it between event-loop lifecycles."""
    global _SEMAPHORE, _LOOP
    with _LOCK:
        _SEMAPHORE = None
        _LOOP = None
