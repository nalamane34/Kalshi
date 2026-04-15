"""Shared test fixtures."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from kalshi_bot.config import BotConfig, RiskConfig
from kalshi_bot.models.market import Market, OrderBook, OrderBookLevel, MarketSnapshot
from kalshi_bot.models.order import Order, Fill, OrderAction, OrderStatus, OrderType, Side
from kalshi_bot.models.portfolio import Balance, Position


@pytest.fixture
def risk_config():
    return RiskConfig(
        max_position_per_market=100,
        max_total_exposure_cents=500000,
        daily_loss_limit_pct=5.0,
        max_drawdown_pct=15.0,
        max_open_orders=50,
        min_balance_reserve_cents=10000,
    )


@pytest.fixture
def sample_balance():
    return Balance(available_balance=100_000, portfolio_value=100_000)


@pytest.fixture
def sample_market():
    return Market(
        ticker="TEST-MARKET-YES",
        event_ticker="TEST-EVENT",
        title="Test Market",
        status="open",
        close_time=datetime.now(timezone.utc) + timedelta(days=7),
        yes_bid=45,
        yes_ask=55,
        last_price=50,
        volume=1000,
        open_interest=500,
    )


@pytest.fixture
def sample_orderbook():
    return OrderBook(
        ticker="TEST-MARKET-YES",
        yes_bids=[
            OrderBookLevel(price=45, quantity=100),
            OrderBookLevel(price=44, quantity=200),
            OrderBookLevel(price=43, quantity=150),
        ],
        yes_asks=[
            OrderBookLevel(price=55, quantity=100),
            OrderBookLevel(price=56, quantity=200),
            OrderBookLevel(price=57, quantity=150),
        ],
    )


@pytest.fixture
def sample_snapshot(sample_market, sample_orderbook):
    return MarketSnapshot(market=sample_market, orderbook=sample_orderbook)


@pytest.fixture
def mock_rest_client():
    client = AsyncMock()
    client.get_balance = AsyncMock(return_value=Balance(available_balance=100_000, portfolio_value=100_000))
    client.get_positions = AsyncMock(return_value=[])
    client.get_orders = AsyncMock(return_value=[])
    client.get_fills = AsyncMock(return_value=[])
    client.place_order = AsyncMock(return_value=Order(
        order_id="test-order-1",
        ticker="TEST-MARKET-YES",
        action=OrderAction.BUY,
        side=Side.YES,
        status=OrderStatus.RESTING,
        initial_count=10,
        remaining_count=10,
    ))
    client.cancel_order = AsyncMock()
    client.batch_cancel_orders = AsyncMock(return_value=[])
    return client
