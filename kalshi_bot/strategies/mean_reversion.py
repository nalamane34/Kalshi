"""Mean reversion strategy using Bollinger Bands and z-score."""

from __future__ import annotations

from collections import deque

import numpy as np

from kalshi_bot.models.market import MarketSnapshot
from kalshi_bot.models.order import OrderRequest, OrderAction, OrderType, Side, Fill
from kalshi_bot.strategies import register_strategy
from kalshi_bot.strategies.base import BaseStrategy, StrategySignal


@register_strategy("mean_reversion")
class MeanReversionStrategy(BaseStrategy):
    """Enter when price deviates from mean, exit on reversion."""

    def __init__(self, name: str = "mean_reversion", params: dict | None = None):
        super().__init__(name, params or {})
        self._price_history: dict[str, deque] = {}
        self._in_position: dict[str, dict] = {}  # ticker -> {"side": Side, "entry_price": int, "entry_z": float}

    async def on_tick(self, ticker: str, snapshot: MarketSnapshot) -> StrategySignal | None:
        mid = snapshot.orderbook.mid_price
        if mid is None:
            return None

        lookback = self.params.get("lookback_periods", 50)
        if ticker not in self._price_history:
            self._price_history[ticker] = deque(maxlen=lookback)
        self._price_history[ticker].append(mid)

        # Need sufficient history
        if len(self._price_history[ticker]) < lookback:
            return None

        prices = np.array(list(self._price_history[ticker]))
        mean = float(np.mean(prices))
        std = float(np.std(prices))

        if std < 0.5:
            return None  # Too stable, no opportunity

        z_score = (mid - mean) / std

        entry_z = self.params.get("entry_z_score", 2.0)
        exit_z = self.params.get("exit_z_score", 0.5)
        order_size = self.params.get("order_size", 5)
        max_pos = self.params.get("max_position", 50)

        # Check for exit first
        if ticker in self._in_position:
            pos = self._in_position[ticker]
            if abs(z_score) < exit_z:
                # Exit: sell what we bought (or buy back what we shorted)
                if pos["side"] == Side.YES:
                    order = OrderRequest(
                        ticker=ticker,
                        action=OrderAction.SELL,
                        side=Side.YES,
                        type=OrderType.LIMIT,
                        count=pos.get("size", order_size),
                        yes_price=int(round(mid)),
                        strategy_name=self.name,
                    )
                else:
                    order = OrderRequest(
                        ticker=ticker,
                        action=OrderAction.SELL,
                        side=Side.NO,
                        type=OrderType.LIMIT,
                        count=pos.get("size", order_size),
                        no_price=int(round(100 - mid)),
                        strategy_name=self.name,
                    )
                del self._in_position[ticker]
                return StrategySignal(
                    ticker=ticker,
                    orders_to_place=[order],
                    reason=f"MR exit: z={z_score:.2f}, mean={mean:.1f}",
                )
            return None

        # Check for entry
        if z_score > entry_z:
            # Price too high -> expect reversion down -> buy NO
            entry_price = int(round(100 - mid))
            order = OrderRequest(
                ticker=ticker,
                action=OrderAction.BUY,
                side=Side.NO,
                type=OrderType.LIMIT,
                count=order_size,
                no_price=entry_price,
                strategy_name=self.name,
            )
            self._in_position[ticker] = {
                "side": Side.NO,
                "entry_price": entry_price,
                "entry_z": z_score,
                "size": order_size,
            }
            return StrategySignal(
                ticker=ticker,
                orders_to_place=[order],
                reason=f"MR entry SHORT: z={z_score:.2f}, mean={mean:.1f}, price={mid:.1f}",
            )

        elif z_score < -entry_z:
            # Price too low -> expect reversion up -> buy YES
            entry_price = int(round(mid))
            order = OrderRequest(
                ticker=ticker,
                action=OrderAction.BUY,
                side=Side.YES,
                type=OrderType.LIMIT,
                count=order_size,
                yes_price=entry_price,
                strategy_name=self.name,
            )
            self._in_position[ticker] = {
                "side": Side.YES,
                "entry_price": entry_price,
                "entry_z": z_score,
                "size": order_size,
            }
            return StrategySignal(
                ticker=ticker,
                orders_to_place=[order],
                reason=f"MR entry LONG: z={z_score:.2f}, mean={mean:.1f}, price={mid:.1f}",
            )

        return None

    async def on_fill(self, fill: Fill) -> None:
        self._log.info(
            "mr_fill",
            ticker=fill.ticker,
            side=fill.side.value,
            action=fill.action.value,
            count=fill.count,
        )

    def get_state(self) -> dict:
        return {
            "price_history": {t: list(h) for t, h in self._price_history.items()},
            "in_position": {
                t: {**p, "side": p["side"].value}
                for t, p in self._in_position.items()
            },
        }

    def load_state(self, state: dict) -> None:
        lookback = self.params.get("lookback_periods", 50)
        for t, h in state.get("price_history", {}).items():
            self._price_history[t] = deque(h, maxlen=lookback)
        for t, p in state.get("in_position", {}).items():
            self._in_position[t] = {**p, "side": Side(p["side"])}
