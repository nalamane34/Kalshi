"""Async token-bucket rate limiter."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Token-bucket rate limiter for async operations."""

    def __init__(self, rate: float, burst: int | None = None):
        """
        Args:
            rate: Maximum sustained requests per second.
            burst: Maximum burst size. Defaults to rate.
        """
        self._rate = rate
        self._burst = burst if burst is not None else int(rate)
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one."""
        async with self._lock:
            self._refill()
            while self._tokens < 1.0:
                wait_time = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait_time)
                self._refill()
            self._tokens -= 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._burst),
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens


class DualRateLimiter:
    """Separate rate limiters for read and write operations."""

    def __init__(self, read_rate: float, write_rate: float):
        self.read = RateLimiter(read_rate)
        self.write = RateLimiter(write_rate)

    async def acquire_read(self) -> None:
        await self.read.acquire()

    async def acquire_write(self) -> None:
        await self.write.acquire()
