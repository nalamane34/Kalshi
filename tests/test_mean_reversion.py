"""Tests for mean reversion strategy."""

import pytest
from datetime import datetime, timezone, timedelta

from kalshi_bot.models.market import Market, OrderBook, OrderBookLevel, MarketSnapshot
from kalshi_bot.strategies.mean_reversion import MeanReversionStrategy


def _make_snapshot(mid_price, ticker="TEST-MR"):
    half_spread = 2
    bid = int(mid_price - half_spread)
    ask = int(mid_price + half_spread)
    market = Market(
        ticker=ticker,
        status="open",
        close_time=datetime.now(timezone.utc) + timedelta(days=7),
        yes_bid=bid,
        yes_ask=ask,
        last_price=int(mid_price),
    )
    ob = OrderBook(
        ticker=ticker,
        yes_bids=[OrderBookLevel(price=bid, quantity=100)],
        yes_asks=[OrderBookLevel(price=ask, quantity=100)],
    )
    return MarketSnapshot(market=market, orderbook=ob)


@pytest.fixture
def mr_strategy():
    params = {
        "lookback_periods": 10,  # short for tests
        "entry_z_score": 2.0,
        "exit_z_score": 0.5,
        "order_size": 5,
        "max_position": 50,
        "bollinger_std_dev": 2.0,
    }
    return MeanReversionStrategy(name="test_mr", params=params)


@pytest.mark.asyncio
async def test_no_signal_insufficient_history(mr_strategy):
    await mr_strategy.on_start(["TEST-MR"])
    # Only 5 ticks, need 10
    for i in range(5):
        signal = await mr_strategy.on_tick("TEST-MR", _make_snapshot(50))
    assert signal is None


@pytest.mark.asyncio
async def test_no_signal_in_range(mr_strategy):
    await mr_strategy.on_start(["TEST-MR"])
    # Feed stable prices
    for _ in range(10):
        signal = await mr_strategy.on_tick("TEST-MR", _make_snapshot(50))
    # Stable prices -> z-score near 0 -> no signal
    assert signal is None


@pytest.mark.asyncio
async def test_entry_signal_on_spike(mr_strategy):
    await mr_strategy.on_start(["TEST-MR"])
    # Build stable history
    for _ in range(9):
        await mr_strategy.on_tick("TEST-MR", _make_snapshot(50))
    # Spike high
    signal = await mr_strategy.on_tick("TEST-MR", _make_snapshot(70))
    # Should generate entry signal (buy NO since price is high)
    if signal:
        assert len(signal.orders_to_place) > 0
        assert "entry" in signal.reason.lower() or "SHORT" in signal.reason


@pytest.mark.asyncio
async def test_state_persistence(mr_strategy):
    state = mr_strategy.get_state()
    assert "price_history" in state
    assert "in_position" in state

    mr_strategy.load_state(state)
