"""Strategy registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kalshi_bot.strategies.base import BaseStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str):
    """Decorator to register a strategy class."""
    def decorator(cls):
        STRATEGY_REGISTRY[name] = cls
        return cls
    return decorator


# Import strategies to trigger registration
from kalshi_bot.strategies.market_maker import MarketMakerStrategy  # noqa: E402, F401
from kalshi_bot.strategies.mean_reversion import MeanReversionStrategy  # noqa: E402, F401
from kalshi_bot.strategies.value import ValueStrategy  # noqa: E402, F401
