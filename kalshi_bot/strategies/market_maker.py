"""Avellaneda-Stoikov inspired market making strategy."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone

import numpy as np

from kalshi_bot.models.market import MarketSnapshot
from kalshi_bot.models.order import OrderRequest, OrderAction, OrderType, Side, Fill
from kalshi_bot.strategies import register_strategy
from kalshi_bot.strategies.base import BaseStrategy, StrategySignal


@register_strategy("market_maker")
class MarketMakerStrategy(BaseStrategy):
    """Market making: place bid/ask around fair value, capture the spread.

    Uses Avellaneda-Stoikov reservation price model with inventory skew.
    """

    def __init__(self, name: str = "market_maker", params: dict | None = None):
        super().__init__(name, params or {})
        self._price_history: dict[str, deque] = {}
        self._last_quote_time: dict[str, float] = {}
        self._our_bid_ids: dict[str, str] = {}   # ticker -> order_id
        self._our_ask_ids: dict[str, str] = {}   # ticker -> order_id
        self._inventory: dict[str, int] = {}      # ticker -> net position

    async def on_tick(self, ticker: str, snapshot: MarketSnapshot) -> StrategySignal | None:
        ob = snapshot.orderbook
        mid = ob.mid_price
        if mid is None:
            return None

        # Track price history
        if ticker not in self._price_history:
            self._price_history[ticker] = deque(maxlen=self.params.get("sigma_window", 50))
        self._price_history[ticker].append(mid)

        # Check if it's time to refresh quotes
        now = time.monotonic()
        refresh_interval = self.params.get("quote_refresh_seconds", 5.0)
        last_quote = self._last_quote_time.get(ticker, 0)
        if now - last_quote < refresh_interval:
            return None

        # Calculate strategy parameters
        inventory = self._inventory.get(ticker, 0)
        sigma = self._estimate_volatility(ticker)
        time_to_close = self._time_to_close(snapshot)

        # Reservation price (Avellaneda-Stoikov)
        gamma = self.params.get("gamma", 0.1)
        reservation = mid - inventory * gamma * (sigma ** 2) * time_to_close
        reservation *= (1 - self.params.get("inventory_skew_factor", 0.5) * inventory / max(self.params.get("max_position", 100), 1))

        # Spread calculation
        spread = self._calculate_spread(sigma, time_to_close)

        bid_price = int(round(reservation - spread / 2))
        ask_price = int(round(reservation + spread / 2))

        # Clamp to valid range
        bid_price = max(1, min(98, bid_price))
        ask_price = max(2, min(99, ask_price))

        # Ensure ask > bid
        if ask_price <= bid_price:
            ask_price = bid_price + 1
            if ask_price > 99:
                ask_price = 99
                bid_price = 98

        # Edge check
        edge_threshold = self.params.get("edge_threshold", 0.02)
        if spread / 100.0 < edge_threshold:
            return None

        # Build signal
        max_pos = self.params.get("max_position", 100)
        order_size = self.params.get("order_size", 10)

        orders_to_place: list[OrderRequest] = []
        orders_to_cancel: list[str] = []

        # Cancel existing orders
        if ticker in self._our_bid_ids:
            orders_to_cancel.append(self._our_bid_ids[ticker])
        if ticker in self._our_ask_ids:
            orders_to_cancel.append(self._our_ask_ids[ticker])

        # Place new bid (only if not at max long)
        if inventory < max_pos:
            bid_size = min(order_size, max_pos - inventory)
            orders_to_place.append(OrderRequest(
                ticker=ticker,
                action=OrderAction.BUY,
                side=Side.YES,
                type=OrderType.LIMIT,
                count=bid_size,
                yes_price=bid_price,
                post_only=True,
                strategy_name=self.name,
            ))

        # Place new ask (only if not at max short)
        if inventory > -max_pos:
            ask_size = min(order_size, max_pos + inventory)
            if ask_size > 0:
                orders_to_place.append(OrderRequest(
                    ticker=ticker,
                    action=OrderAction.SELL,
                    side=Side.YES,
                    type=OrderType.LIMIT,
                    count=ask_size,
                    yes_price=ask_price,
                    post_only=True,
                    strategy_name=self.name,
                ))

        self._last_quote_time[ticker] = now

        if orders_to_place or orders_to_cancel:
            return StrategySignal(
                ticker=ticker,
                orders_to_place=orders_to_place,
                orders_to_cancel=orders_to_cancel,
                reason=f"MM quote: bid={bid_price} ask={ask_price} inv={inventory} σ={sigma:.2f}",
            )
        return None

    async def on_fill(self, fill: Fill) -> None:
        """Update inventory on fill."""
        ticker = fill.ticker
        if fill.action == OrderAction.BUY:
            delta = fill.count if fill.side == Side.YES else -fill.count
        else:
            delta = -fill.count if fill.side == Side.YES else fill.count
        self._inventory[ticker] = self._inventory.get(ticker, 0) + delta
        self._log.info(
            "mm_fill",
            ticker=ticker,
            delta=delta,
            new_inventory=self._inventory[ticker],
        )
        # Force immediate re-quote by resetting quote timer
        self._last_quote_time.pop(ticker, None)

    def track_order_id(self, ticker: str, order_id: str, is_bid: bool) -> None:
        """Track our order IDs for cancel/replace."""
        if is_bid:
            self._our_bid_ids[ticker] = order_id
        else:
            self._our_ask_ids[ticker] = order_id

    def _estimate_volatility(self, ticker: str) -> float:
        """Rolling std dev of mid price changes in cents."""
        history = self._price_history.get(ticker, deque())
        if len(history) < 5:
            return self.params.get("target_spread_cents", 4) / 2.0
        prices = np.array(list(history))
        returns = np.diff(prices)
        vol = float(np.std(returns))
        return max(vol, 0.5)  # floor at 0.5 cents

    def _calculate_spread(self, sigma: float, time_to_close: float) -> float:
        """Calculate optimal spread."""
        gamma = self.params.get("gamma", 0.1)
        base = gamma * (sigma ** 2) * time_to_close
        min_spread = self.params.get("min_spread_cents", 2)
        max_spread = self.params.get("max_spread_cents", 10)
        return max(min_spread, min(max_spread, base + min_spread))

    @staticmethod
    def _time_to_close(snapshot: MarketSnapshot) -> float:
        """Normalized time to market close. 1.0 = 1 day."""
        close_time = snapshot.market.close_time
        if close_time is None:
            return 1.0
        now = datetime.now(timezone.utc)
        remaining = (close_time - now).total_seconds()
        return max(0.01, remaining / 86400.0)

    async def on_stop(self) -> list[str]:
        """Return all our order IDs for cancellation."""
        ids = []
        ids.extend(self._our_bid_ids.values())
        ids.extend(self._our_ask_ids.values())
        return ids

    def get_state(self) -> dict:
        return {
            "inventory": dict(self._inventory),
            "price_history": {
                t: list(h) for t, h in self._price_history.items()
            },
        }

    def load_state(self, state: dict) -> None:
        self._inventory = state.get("inventory", {})
        for t, h in state.get("price_history", {}).items():
            self._price_history[t] = deque(h, maxlen=self.params.get("sigma_window", 50))
