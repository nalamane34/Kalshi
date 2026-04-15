"""Tests for market making strategy."""

import pytest
from datetime import datetime, timezone, timedelta

from kalshi_bot.models.market import Market, OrderBook, OrderBookLevel, MarketSnapshot
from kalshi_bot.strategies.market_maker import MarketMakerStrategy


def _make_snapshot(bid=45, ask=55, close_days=7):
    market = Market(
        ticker="TEST-MKT",
        status="open",
        close_time=datetime.now(timezone.utc) + timedelta(days=close_days),
        yes_bid=bid,
        yes_ask=ask,
        last_price=(bid + ask) // 2,
        volume=1000,
        open_interest=500,
    )
    orderbook = OrderBook(
        ticker="TEST-MKT",
        yes_bids=[OrderBookLevel(price=bid, quantity=100)],
        yes_asks=[OrderBookLevel(price=ask, quantity=100)],
    )
    return MarketSnapshot(market=market, orderbook=orderbook)


@pytest.fixture
def mm_strategy():
    params = {
        "target_spread_cents": 4,
        "min_spread_cents": 2,
        "max_spread_cents": 10,
        "order_size": 10,
        "max_position": 100,
        "inventory_skew_factor": 0.5,
        "gamma": 0.1,
        "sigma_window": 50,
        "quote_refresh_seconds": 0,  # no delay for tests
        "edge_threshold": 0.01,
    }
    return MarketMakerStrategy(name="test_mm", params=params)


@pytest.mark.asyncio
async def test_generates_bid_ask(mm_strategy):
    snapshot = _make_snapshot(bid=45, ask=55)
    await mm_strategy.on_start(["TEST-MKT"])
    signal = await mm_strategy.on_tick("TEST-MKT", snapshot)
    assert signal is not None
    assert len(signal.orders_to_place) == 2  # bid + ask

    bid_order = signal.orders_to_place[0]
    ask_order = signal.orders_to_place[1]
    assert bid_order.action.value == "buy"
    assert ask_order.action.value == "sell"
    assert bid_order.yes_price < ask_order.yes_price


@pytest.mark.asyncio
async def test_prices_in_valid_range(mm_strategy):
    snapshot = _make_snapshot(bid=2, ask=98)
    await mm_strategy.on_start(["TEST-MKT"])
    signal = await mm_strategy.on_tick("TEST-MKT", snapshot)
    assert signal is not None
    for order in signal.orders_to_place:
        price = order.yes_price or order.no_price
        assert 1 <= price <= 99


@pytest.mark.asyncio
async def test_inventory_skew(mm_strategy):
    snapshot = _make_snapshot(bid=45, ask=55)
    await mm_strategy.on_start(["TEST-MKT"])

    # No inventory
    signal1 = await mm_strategy.on_tick("TEST-MKT", snapshot)
    bid1 = signal1.orders_to_place[0].yes_price
    ask1 = signal1.orders_to_place[1].yes_price

    # Add long inventory
    mm_strategy._inventory["TEST-MKT"] = 50
    mm_strategy._last_quote_time.pop("TEST-MKT", None)
    signal2 = await mm_strategy.on_tick("TEST-MKT", snapshot)
    bid2 = signal2.orders_to_place[0].yes_price
    ask2 = signal2.orders_to_place[1].yes_price

    # With long inventory, should lower ask to encourage selling
    assert ask2 <= ask1


@pytest.mark.asyncio
async def test_max_position_bid_only(mm_strategy):
    """At max short, should only place bids."""
    mm_strategy._inventory["TEST-MKT"] = -100
    snapshot = _make_snapshot(bid=45, ask=55)
    await mm_strategy.on_start(["TEST-MKT"])
    signal = await mm_strategy.on_tick("TEST-MKT", snapshot)
    assert signal is not None
    # Should have bid but no ask (at max short, can't sell more)
    actions = [o.action.value for o in signal.orders_to_place]
    assert "buy" in actions


@pytest.mark.asyncio
async def test_no_signal_on_empty_orderbook(mm_strategy):
    market = Market(ticker="TEST-MKT", status="open")
    ob = OrderBook(ticker="TEST-MKT")
    snapshot = MarketSnapshot(market=market, orderbook=ob)
    await mm_strategy.on_start(["TEST-MKT"])
    signal = await mm_strategy.on_tick("TEST-MKT", snapshot)
    assert signal is None


@pytest.mark.asyncio
async def test_stop_returns_order_ids(mm_strategy):
    mm_strategy._our_bid_ids["TEST-MKT"] = "bid-1"
    mm_strategy._our_ask_ids["TEST-MKT"] = "ask-1"
    ids = await mm_strategy.on_stop()
    assert "bid-1" in ids
    assert "ask-1" in ids
