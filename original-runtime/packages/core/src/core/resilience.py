"""
core.resilience — retries, rate limits, timed cache, async↔sync bridge
=======================================================================

Stdlib-only primitives for building resilient workflows. Banned-DIY
patterns in ``.cursor/hooks/banned-diy-patterns.txt`` point callers here
instead of reinventing retry/cache logic inline.

Symbols
-------
- :func:`retry` — sync retry decorator with exponential backoff + jitter.
- :func:`async_retry` — async counterpart.
- :class:`RateLimiter` — sliding-window rate limiter, thread-safe.
- :func:`timed_cache` — ``lru_cache`` with TTL.
- :func:`shared_thread_pool` — process-wide :class:`ThreadPoolExecutor`.
- :func:`run_sync` — run an awaitable from sync code, handling the
  nested-loop case via the shared pool.
- :func:`shutdown_thread_pool` — teardown helper (tests + FastAPI shutdown).
- :data:`as_completed` — re-exported from :mod:`concurrent.futures` for
  caller convenience.

See ``internal/refactor/packages/core/design.md`` decision D7 for the
deferral of ``circuit_breaker`` (zero callers today).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import random
import threading
import time
from concurrent.futures import as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "retry",
    "async_retry",
    "RateLimiter",
    "timed_cache",
    "shared_thread_pool",
    "shutdown_thread_pool",
    "run_sync",
    "as_completed",
]


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.2,
    retry_on: tuple = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Sync retry decorator with exponential backoff + jitter.

    Re-raises the last exception after ``attempts`` failures.
    """
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if i == attempts - 1:
                        break
                    delay = min(max_delay, base_delay * (2 ** i))
                    delay += random.uniform(0, delay * jitter)
                    if on_retry:
                        on_retry(i + 1, exc)
                    logger.info(
                        "retry %d/%d after %.2fs: %s",
                        i + 1, attempts, delay, exc,
                    )
                    time.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return deco


def async_retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.2,
    retry_on: tuple = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> Callable[
    [Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]
]:
    """Async retry decorator with exponential backoff + jitter."""

    def deco(
        fn: Callable[..., Awaitable[T]],
    ) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for i in range(attempts):
                try:
                    return await fn(*args, **kwargs)
                except retry_on as exc:
                    last_exc = exc
                    if i == attempts - 1:
                        break
                    delay = min(max_delay, base_delay * (2 ** i))
                    delay += random.uniform(0, delay * jitter)
                    if on_retry:
                        on_retry(i + 1, exc)
                    logger.info(
                        "async retry %d/%d after %.2fs: %s",
                        i + 1, attempts, delay, exc,
                    )
                    await asyncio.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return deco


# ---------------------------------------------------------------------------
# Rate limiter — sliding-window, thread-safe
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window rate limiter. ``acquire()`` blocks until a slot frees.

    Separate from a leaky-bucket (which smooths request rate). Use when
    ``N per window`` is the business constraint — e.g. "send at most 80
    outreach messages per day".
    """

    def __init__(self, *, max_per_window: int, window_seconds: float) -> None:
        if max_per_window <= 0 or window_seconds <= 0:
            raise ValueError("RateLimiter requires positive bounds")
        self.max = max_per_window
        self.window = window_seconds
        self._events: list[float] = []
        self._lock = threading.Lock()

    def acquire(self, *, block: bool = True) -> bool:
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window
                self._events = [t for t in self._events if t > cutoff]
                if len(self._events) < self.max:
                    self._events.append(now)
                    return True
                wait = self.window - (now - self._events[0])
            if not block:
                return False
            time.sleep(max(0.01, wait))

    async def aacquire(self, *, block: bool = True) -> bool:
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window
                self._events = [t for t in self._events if t > cutoff]
                if len(self._events) < self.max:
                    self._events.append(now)
                    return True
                wait = self.window - (now - self._events[0])
            if not block:
                return False
            await asyncio.sleep(max(0.01, wait))


# ---------------------------------------------------------------------------
# Timed cache — lru_cache with TTL
# ---------------------------------------------------------------------------


def timed_cache(
    ttl: timedelta,
    *,
    maxsize: int = 128,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """lru_cache with TTL. Thread-safe. Per-decorated-fn closure dict.

    Replaces the ``@lru_cache + timedelta`` DIY pattern flagged in
    banned-diy.
    """

    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        cache: dict[tuple, tuple[datetime, T]] = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            key = (args, tuple(sorted(kwargs.items())))
            now = datetime.now(timezone.utc)
            with lock:
                if key in cache:
                    ts, val = cache[key]
                    if now - ts < ttl:
                        return val
            val = fn(*args, **kwargs)
            with lock:
                if len(cache) >= maxsize:
                    oldest_key = min(cache, key=lambda k: cache[k][0])
                    cache.pop(oldest_key, None)
                cache[key] = (now, val)
            return val

        def _invalidate() -> None:
            with lock:
                cache.clear()

        wrapper.cache_clear = _invalidate  # type: ignore[attr-defined]
        return wrapper

    return deco


# ---------------------------------------------------------------------------
# Shared thread pool
# ---------------------------------------------------------------------------

_POOL_LOCK = threading.Lock()
_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def shared_thread_pool(
    *, max_workers: int = 8
) -> concurrent.futures.ThreadPoolExecutor:
    """One process-wide :class:`ThreadPoolExecutor`.

    Avoids proliferating ad-hoc executors for CPU-bound or blocking-IO
    offload from async code. Prefix ``"leo-shared"`` on thread names —
    useful in stack traces.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="leo-shared",
            )
        return _POOL


def shutdown_thread_pool(wait: bool = True) -> None:
    """Shut down the shared pool (tests + FastAPI shutdown)."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.shutdown(wait=wait)
            _POOL = None


# ---------------------------------------------------------------------------
# Async ↔ sync bridge
# ---------------------------------------------------------------------------


def run_sync(coro: Awaitable[T], *, timeout: float | None = None) -> T:
    """Run an awaitable from sync code. Handles nested-loop case.

    - No loop running in this thread → :func:`asyncio.run`.
    - Loop already running in this thread → offload to the shared pool
      and wait for its future.

    Raises :class:`concurrent.futures.TimeoutError` if ``timeout`` elapses.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(_as_coro(coro))

    pool = shared_thread_pool()
    fut = pool.submit(lambda: asyncio.run(_as_coro(coro)))
    return fut.result(timeout=timeout)


async def _as_coro(awaitable: Awaitable[T]) -> T:
    return await awaitable
