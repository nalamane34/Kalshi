"""Tests for risk manager."""

import pytest

from kalshi_bot.config import RiskConfig
from kalshi_bot.models.order import OrderRequest, OrderAction, OrderType, Side
from kalshi_bot.risk.manager import RiskManager


@pytest.fixture
def risk_mgr(risk_config):
    rm = RiskManager(risk_config)
    rm.initialize(balance=100_000, positions={})
    return rm


def _make_buy_order(ticker="TEST", count=10, price=50):
    return OrderRequest(
        ticker=ticker,
        action=OrderAction.BUY,
        side=Side.YES,
        type=OrderType.LIMIT,
        count=count,
        yes_price=price,
    )


class TestRiskChecks:
    def test_approve_simple_order(self, risk_mgr):
        result = risk_mgr.check_order(_make_buy_order())
        assert result.approved

    def test_reject_when_halted(self, risk_mgr):
        risk_mgr._halt_trading("test halt")
        result = risk_mgr.check_order(_make_buy_order())
        assert not result.approved
        assert "halted" in result.reason.lower()

    def test_position_limit(self, risk_mgr):
        # Fill up position to max
        risk_mgr._positions["TEST"] = 95
        result = risk_mgr.check_order(_make_buy_order(count=10))
        # Should be approved with adjusted size
        assert result.approved
        assert result.adjusted_size == 5

    def test_position_limit_at_max(self, risk_mgr):
        risk_mgr._positions["TEST"] = 100
        result = risk_mgr.check_order(_make_buy_order(count=10))
        assert not result.approved

    def test_balance_reserve(self, risk_mgr):
        risk_mgr._current_balance = 10_500  # $105
        # Order costing $50 would leave $55, below $100 reserve
        result = risk_mgr.check_order(_make_buy_order(count=100, price=50))
        assert not result.approved

    def test_open_order_limit(self, risk_mgr):
        risk_mgr._open_order_count = 50
        result = risk_mgr.check_order(_make_buy_order())
        assert not result.approved


class TestCircuitBreakers:
    def test_daily_loss_halt(self, risk_mgr):
        risk_mgr._starting_balance = 100_000
        risk_mgr.update_balance(94_000)  # -6%
        assert risk_mgr.is_halted
        assert "daily loss" in risk_mgr.halt_reason.lower()

    def test_max_drawdown_halt(self, risk_mgr):
        risk_mgr._peak_balance = 200_000
        risk_mgr.update_balance(168_000)  # -16% from peak
        assert risk_mgr.is_halted
        assert "drawdown" in risk_mgr.halt_reason.lower()

    def test_no_halt_within_limits(self, risk_mgr):
        risk_mgr.update_balance(96_000)  # -4%, within 5% limit
        assert not risk_mgr.is_halted


class TestPositionTracking:
    def test_update_position(self, risk_mgr):
        risk_mgr.update_position("TEST", 10)
        assert risk_mgr.get_position("TEST") == 10
        risk_mgr.update_position("TEST", -5)
        assert risk_mgr.get_position("TEST") == 5

    def test_position_removed_at_zero(self, risk_mgr):
        risk_mgr.update_position("TEST", 10)
        risk_mgr.update_position("TEST", -10)
        assert risk_mgr.get_position("TEST") == 0

    def test_status_report(self, risk_mgr):
        status = risk_mgr.get_status()
        assert "halted" in status
        assert "balance_dollars" in status
        assert status["balance_dollars"] == 1000.0
