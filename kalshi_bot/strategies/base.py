"""Abstract base class for all trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import structlog

from kalshi_bot.models.market import MarketSnapshot
from kalshi_bot.models.order import OrderRequest, Fill


@dataclass
class StrategySignal:
    """What a strategy wants the engine to execute."""
    ticker: str
    orders_to_place: list[OrderRequest] = field(default_factory=list)
    orders_to_cancel: list[str] = field(default_factory=list)  # order_ids
    reason: str = ""


class BaseStrategy(ABC):
    """Base class that all strategies must implement."""

    def __init__(self, name: str, params: dict):
        self.name = name
        self.params = params
        self._log = structlog.get_logger("kalshi.strategy", strategy=name)
        self._active_tickers: set[str] = set()

    @abstractmethod
    async def on_tick(self, ticker: str, snapshot: MarketSnapshot) -> StrategySignal | None:
        """Called every tick with latest market data. Return signal or None."""

    async def on_fill(self, fill: Fill) -> None:
        """Called when one of this strategy's orders fills."""

    async def on_start(self, tickers: list[str]) -> None:
        """Called once on startup."""
        self._active_tickers = set(tickers)
        self._log.info("strategy_started", tickers=tickers)

    async def on_stop(self) -> list[str]:
        """Called on shutdown. Return order_ids to cancel."""
        return []

    def get_state(self) -> dict:
        """Serialize state for persistence."""
        return {}

    def load_state(self, state: dict) -> None:
        """Restore state from persistence."""
