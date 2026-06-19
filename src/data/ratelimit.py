"""Shared rate-limiting + retry primitives for outbound HTTP data sources.

Two free-tier APIs in this project (Finnhub, Stocktwits) throttle us when
the scanner fans out across ~30 tickers concurrently. They misbehave in
different ways:

  - Finnhub returns a clean ``429 Too Many Requests`` (no Retry-After).
  - Stocktwits silently stops responding — requests hang until the client
    times out (observed: 10 of 30 concurrent calls ReadTimeout).

Both are handled by the same two-layer defense:

  1. ``SlidingWindowLimiter`` — caps outgoing calls per rolling window so
     we *prevent* overload rather than react to it. Process-wide and
     concurrency-safe; calls are spaced, not bursted-and-blocked.
  2. ``retry_async`` — retries a coroutine factory on a configurable set
     of exceptions / predicates with exponential backoff.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SlidingWindowLimiter:
    """Allow at most ``rate`` calls per ``window_seconds`` rolling window.

    Process-wide and concurrency-safe. Each ``acquire()`` either returns
    immediately or sleeps just long enough that the call falls inside the
    budget.
    """

    def __init__(self, rate: int, window_seconds: float) -> None:
        self._rate = rate
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                # Drop timestamps that fall outside the window.
                while self._timestamps and now - self._timestamps[0] >= self._window:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._rate:
                    self._timestamps.append(now)
                    return
                # Sleep until the oldest call ages out.
                wait = self._window - (now - self._timestamps[0]) + 0.01
            await asyncio.sleep(wait)


async def retry_async(
    factory: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "request",
) -> T:
    """Run ``factory()`` with exponential-backoff retry on ``retry_on``.

    ``factory`` must be a zero-arg coroutine *factory* (called fresh each
    attempt), not a coroutine — coroutines can't be awaited twice.

    Re-raises the last exception if all attempts fail. Exceptions not in
    ``retry_on`` propagate immediately (no retry).
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await factory()
        except retry_on as e:
            last_exc = e
            if attempt + 1 >= max_attempts:
                break
            delay = base_delay * (2**attempt)
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                label,
                attempt + 1,
                max_attempts,
                type(e).__name__,
                delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


__all__ = ["SlidingWindowLimiter", "retry_async"]
