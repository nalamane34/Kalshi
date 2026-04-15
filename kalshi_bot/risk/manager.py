"""Centralized risk management — every order must pass through here."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from kalshi_bot.config import RiskConfig
from kalshi_bot.models.order import OrderRequest, OrderAction, Side


@dataclass
class RiskCheckResult:
    """Result of a pre-trade risk check."""
    approved: bool
    reason: str = ""
    adjusted_size: int | None = None


class RiskManager:
    """5-gate risk system for all order flow."""

    def __init__(self, config: RiskConfig):
        self._config = config
        self._log = structlog.get_logger("kalshi.risk")

        # Live state
        self._positions: dict[str, int] = {}            # ticker -> net contracts (+ = YES, - = NO)
        self._open_order_count: int = 0
        self._starting_balance: int = 0                  # daily start (cents)
        self._current_balance: int = 0
        self._peak_balance: int = 0
        self._trading_halted: bool = False
        self._halt_reason: str = ""

    def initialize(self, balance: int, positions: dict[str, int]) -> None:
        """Set opening state from API data."""
        self._current_balance = balance
        self._starting_balance = balance
        self._peak_balance = balance
        self._positions = dict(positions)
        self._log.info(
            "risk_initialized",
            balance_dollars=balance / 100,
            num_positions=len(positions),
        )

    # ─── Pre-trade Check ──────────────────────────────────────────

    def check_order(self, request: OrderRequest) -> RiskCheckResult:
        """Pre-trade risk check. Must be called before every order submission.

        Gates (in order):
        1. Trading not halted
        2. Position limit per market
        3. Total portfolio exposure
        4. Sufficient balance (with reserve)
        5. Open order count
        """
        # Gate 1: Circuit breaker
        if self._trading_halted:
            return RiskCheckResult(
                approved=False,
                reason=f"Trading halted: {self._halt_reason}",
            )

        ticker = request.ticker
        current_pos = self._positions.get(ticker, 0)
        size = request.count

        # Determine position direction change
        if request.action == OrderAction.BUY:
            if request.side == Side.YES:
                new_pos = current_pos + size
            else:
                new_pos = current_pos - size
        else:  # SELL
            if request.side == Side.YES:
                new_pos = current_pos - size
            else:
                new_pos = current_pos + size

        # Gate 2: Position limit per market
        max_pos = self._config.max_position_per_market
        if abs(new_pos) > max_pos:
            # Try to adjust size
            allowed = max_pos - abs(current_pos)
            if allowed <= 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Position limit reached for {ticker}: {current_pos}/{max_pos}",
                )
            return RiskCheckResult(
                approved=True,
                reason=f"Size reduced from {size} to {allowed} (position limit)",
                adjusted_size=allowed,
            )

        # Gate 3: Total exposure
        total_exposure = sum(abs(p) for p in self._positions.values())
        price = request.yes_price or request.no_price or 50
        order_exposure = size * price  # cents
        if total_exposure * 50 + order_exposure > self._config.max_total_exposure_cents:
            return RiskCheckResult(
                approved=False,
                reason=f"Total exposure limit would be exceeded",
            )

        # Gate 4: Balance reserve
        cost = size * price  # rough cost in cents
        if request.action == OrderAction.BUY:
            available_after = self._current_balance - cost
            if available_after < self._config.min_balance_reserve_cents:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Insufficient balance (reserve: ${self._config.min_balance_reserve_cents/100:.2f})",
                )

        # Gate 5: Open order count
        if self._open_order_count >= self._config.max_open_orders:
            return RiskCheckResult(
                approved=False,
                reason=f"Max open orders reached: {self._open_order_count}/{self._config.max_open_orders}",
            )

        return RiskCheckResult(approved=True)

    # ─── State Updates ────────────────────────────────────────────

    def update_position(self, ticker: str, delta: int) -> None:
        """Called on fill to update position tracking."""
        old = self._positions.get(ticker, 0)
        new = old + delta
        if new == 0:
            self._positions.pop(ticker, None)
        else:
            self._positions[ticker] = new
        self._log.debug("position_updated", ticker=ticker, old=old, new=new)

    def update_balance(self, new_balance: int) -> None:
        """Called on balance change. Triggers circuit breakers if needed."""
        self._current_balance = new_balance
        if new_balance > self._peak_balance:
            self._peak_balance = new_balance

        # Check daily loss limit
        if self._starting_balance > 0:
            daily_pnl_pct = (
                (new_balance - self._starting_balance) / self._starting_balance * 100
            )
            if daily_pnl_pct < -self._config.daily_loss_limit_pct:
                self._halt_trading(
                    f"Daily loss limit breached: {daily_pnl_pct:.1f}% "
                    f"(limit: -{self._config.daily_loss_limit_pct}%)"
                )

        # Check max drawdown
        if self._peak_balance > 0:
            drawdown_pct = (
                (self._peak_balance - new_balance) / self._peak_balance * 100
            )
            if drawdown_pct > self._config.max_drawdown_pct:
                self._halt_trading(
                    f"Max drawdown breached: {drawdown_pct:.1f}% "
                    f"(limit: {self._config.max_drawdown_pct}%)"
                )

    def register_order(self) -> None:
        self._open_order_count += 1

    def unregister_order(self) -> None:
        self._open_order_count = max(0, self._open_order_count - 1)

    def set_open_order_count(self, count: int) -> None:
        self._open_order_count = count

    def _halt_trading(self, reason: str) -> None:
        if not self._trading_halted:
            self._trading_halted = True
            self._halt_reason = reason
            self._log.critical("TRADING_HALTED", reason=reason)

    # ─── Properties ───────────────────────────────────────────────

    @property
    def is_halted(self) -> bool:
        return self._trading_halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def daily_pnl_pct(self) -> float:
        if self._starting_balance == 0:
            return 0.0
        return (self._current_balance - self._starting_balance) / self._starting_balance * 100

    @property
    def drawdown_pct(self) -> float:
        if self._peak_balance == 0:
            return 0.0
        return (self._peak_balance - self._current_balance) / self._peak_balance * 100

    def get_position(self, ticker: str) -> int:
        return self._positions.get(ticker, 0)

    def get_status(self) -> dict:
        return {
            "halted": self._trading_halted,
            "halt_reason": self._halt_reason,
            "balance_dollars": self._current_balance / 100,
            "daily_pnl_pct": round(self.daily_pnl_pct, 2),
            "drawdown_pct": round(self.drawdown_pct, 2),
            "positions": dict(self._positions),
            "open_orders": self._open_order_count,
        }
