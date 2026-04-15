"""Tests for rate limiter."""

import asyncio
import time

import pytest

from kalshi_bot.client.rate_limiter import RateLimiter, DualRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_burst():
    """Should allow up to burst size immediately."""
    rl = RateLimiter(rate=10, burst=5)
    for _ in range(5):
        await rl.acquire()
    # All 5 should complete without significant delay


@pytest.mark.asyncio
async def test_rate_limiter_throttles():
    """Should delay when tokens exhausted."""
    rl = RateLimiter(rate=100, burst=1)
    await rl.acquire()  # use the single token

    start = time.monotonic()
    await rl.acquire()  # should wait ~10ms
    elapsed = time.monotonic() - start
    assert elapsed >= 0.005  # at least some wait


@pytest.mark.asyncio
async def test_rate_limiter_available():
    rl = RateLimiter(rate=10, burst=10)
    assert rl.available == 10.0
    await rl.acquire()
    assert rl.available < 10.0


@pytest.mark.asyncio
async def test_dual_rate_limiter():
    dl = DualRateLimiter(read_rate=20, write_rate=10)
    await dl.acquire_read()
    await dl.acquire_write()
    # Both should work independently
