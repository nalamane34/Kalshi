"""Tests for portfolio tracker."""

import pytest

from kalshi_bot.models.order import Fill, OrderAction, Side, Order, OrderStatus
from kalshi_bot.models.portfolio import Balance
from kalshi_bot.portfolio import PortfolioTracker
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.config import RiskConfig
from kalshi_bot.storage.database import Database


@pytest.fixture
async def portfolio_deps():
    db = Database(":memory:")
    await db.initialize()
    risk = RiskManager(RiskConfig())
    risk.initialize(balance=100_000, positions={})
    yield db, risk
    await db.close()


@pytest.mark.asyncio
async def test_sync(mock_rest_client, portfolio_deps):
    db, risk = portfolio_deps
    pt = PortfolioTracker(mock_rest_client, risk, db)
    await pt.sync()
    assert pt.balance.available_balance == 100_000


@pytest.mark.asyncio
async def test_on_fill_updates_risk(mock_rest_client, portfolio_deps):
    db, risk = portfolio_deps
    pt = PortfolioTracker(mock_rest_client, risk, db)
    fill = Fill(
        trade_id="f1",
        order_id="o1",
        ticker="TEST",
        action=OrderAction.BUY,
        side=Side.YES,
        count=10,
        yes_price=50,
    )
    await pt.on_fill(fill)
    assert risk.get_position("TEST") == 10


@pytest.mark.asyncio
async def test_get_position_default(mock_rest_client, portfolio_deps):
    db, risk = portfolio_deps
    pt = PortfolioTracker(mock_rest_client, risk, db)
    assert pt.get_position("UNKNOWN") == 0


@pytest.mark.asyncio
async def test_on_order_update(mock_rest_client, portfolio_deps):
    db, risk = portfolio_deps
    pt = PortfolioTracker(mock_rest_client, risk, db)
    order = Order(
        order_id="o1",
        ticker="TEST",
        status=OrderStatus.RESTING,
    )
    await pt.on_order_update(order)
    assert "o1" in pt._open_orders

    order.status = OrderStatus.EXECUTED
    await pt.on_order_update(order)
    assert "o1" not in pt._open_orders
