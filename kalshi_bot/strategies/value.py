"""Value strategy: Kelly criterion sizing when market diverges from fair value."""

from __future__ import annotations

import time
from collections import deque

import numpy as np

from kalshi_bot.models.market import MarketSnapshot
from kalshi_bot.models.order import OrderRequest, OrderAction, OrderType, Side, Fill
from kalshi_bot.strategies import register_strategy
from kalshi_bot.strategies.base import BaseStrategy, StrategySignal


@register_strategy("value")
class ValueStrategy(BaseStrategy):
    """Detect mispriced markets and size using fractional Kelly criterion.

    Fair value is estimated from a weighted moving average of recent prices,
    but can be overridden externally for markets with fundamental models.
    """

    def __init__(self, name: str = "value", params: dict | None = None):
        super().__init__(name, params or {})
        self._fair_values: dict[str, float] = {}        # ticker -> probability (0-1)
        self._price_history: dict[str, deque] = {}
        self._current_positions: dict[str, int] = {}    # ticker -> net contracts
        self._last_rebalance: dict[str, float] = {}
        self._available_capital: int = 100_00            # cents, updated externally

    async def on_tick(self, ticker: str, snapshot: MarketSnapshot) -> StrategySignal | None:
        mid = snapshot.orderbook.mid_price
        if mid is None:
            return None

        # Track price history for fair value estimation
        if ticker not in self._price_history:
            self._price_history[ticker] = deque(maxlen=100)
        self._price_history[ticker].append(mid)

        # Estimate fair value
        fair_prob = self._estimate_fair_value(ticker)
        if fair_prob is None:
            return None

        # Market price as probability
        market_prob = mid / 100.0
        edge = fair_prob - market_prob  # positive = market underpriced YES

        edge_threshold = self.params.get("edge_threshold", 0.05)
        if abs(edge) < edge_threshold:
            return None

        # Rate limit rebalancing
        now = time.monotonic()
        interval = self.params.get("rebalance_interval", 60)
        if now - self._last_rebalance.get(ticker, 0) < interval:
            return None

        # Kelly sizing
        current_pos = self._current_positions.get(ticker, 0)
        max_pos = self.params.get("max_position", 50)

        if edge > 0:
            # YES is underpriced -> buy YES
            target = self._kelly_size(fair_prob, int(round(mid)))
            target = min(target, max_pos)
            delta = target - max(current_pos, 0)
            if delta <= 0:
                return None
            order = OrderRequest(
                ticker=ticker,
                action=OrderAction.BUY,
                side=Side.YES,
                type=OrderType.LIMIT,
                count=delta,
                yes_price=int(round(mid)),
                strategy_name=self.name,
            )
        else:
            # NO is underpriced -> buy NO
            no_fair = 1.0 - fair_prob
            no_price = 100 - int(round(mid))
            target = self._kelly_size(no_fair, no_price)
            target = min(target, max_pos)
            delta = target - max(-current_pos, 0)
            if delta <= 0:
                return None
            order = OrderRequest(
                ticker=ticker,
                action=OrderAction.BUY,
                side=Side.NO,
                type=OrderType.LIMIT,
                count=delta,
                no_price=no_price,
                strategy_name=self.name,
            )

        self._last_rebalance[ticker] = now

        return StrategySignal(
            ticker=ticker,
            orders_to_place=[order],
            reason=f"Value: edge={edge:+.3f}, fair={fair_prob:.3f}, market={market_prob:.3f}, size={delta}",
        )

    def _estimate_fair_value(self, ticker: str) -> float | None:
        """Estimate fair probability. Uses external override or EWMA."""
        # External override takes priority
        if ticker in self._fair_values:
            return self._fair_values[ticker]

        # EWMA with recency bias
        history = self._price_history.get(ticker, deque())
        if len(history) < 20:
            return None

        prices = np.array(list(history))
        # Exponentially weighted mean
        weights = np.exp(np.linspace(-1, 0, len(prices)))
        weights /= weights.sum()
        ewma = float(np.dot(prices, weights))
        return ewma / 100.0

    def _kelly_size(self, fair_prob: float, market_price_cents: int) -> int:
        """Fractional Kelly for binary contracts.

        For YES at price p cents:
          payout if win: (100 - p) cents
          loss if lose: p cents
          odds b = (100 - p) / p
          f* = fair_prob - (1 - fair_prob) / b
             = fair_prob - (1 - fair_prob) * p / (100 - p)
        """
        p = max(1, min(99, market_price_cents))
        b = (100 - p) / p  # payout odds

        kelly = fair_prob - (1 - fair_prob) / b
        if kelly <= 0:
            return 0

        fraction = self.params.get("kelly_fraction", 0.25)
        adjusted = kelly * fraction

        # Convert to contracts based on available capital
        cost_per_contract = p  # cents
        max_contracts = int(self._available_capital * adjusted / cost_per_contract)
        return max(0, max_contracts)

    def set_fair_value(self, ticker: str, fair_prob: float) -> None:
        """Allow external systems to inject fair value estimates."""
        self._fair_values[ticker] = max(0.01, min(0.99, fair_prob))

    def set_capital(self, available_cents: int) -> None:
        """Update available capital for Kelly sizing."""
        self._available_capital = available_cents

    async def on_fill(self, fill: Fill) -> None:
        if fill.action == OrderAction.BUY:
            delta = fill.count if fill.side == Side.YES else -fill.count
        else:
            delta = -fill.count if fill.side == Side.YES else fill.count
        self._current_positions[fill.ticker] = (
            self._current_positions.get(fill.ticker, 0) + delta
        )
        self._log.info("value_fill", ticker=fill.ticker, delta=delta)

    def get_state(self) -> dict:
        return {
            "fair_values": dict(self._fair_values),
            "positions": dict(self._current_positions),
            "price_history": {t: list(h) for t, h in self._price_history.items()},
        }

    def load_state(self, state: dict) -> None:
        self._fair_values = state.get("fair_values", {})
        self._current_positions = state.get("positions", {})
        for t, h in state.get("price_history", {}).items():
            self._price_history[t] = deque(h, maxlen=100)
