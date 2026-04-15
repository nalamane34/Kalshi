"""Market data models."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Market(BaseModel):
    """A single Kalshi market."""
    ticker: str
    event_ticker: str = ""
    title: str = ""
    subtitle: str = ""
    status: str = "open"
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    yes_bid: int = 0          # cents
    yes_ask: int = 0          # cents
    no_bid: int = 0
    no_ask: int = 0
    last_price: int = 0       # cents
    volume: int = 0
    open_interest: int = 0
    liquidity: int = 0
    result: Optional[str] = None


class OrderBookLevel(BaseModel):
    """A single price level in the order book."""
    price: int     # cents (1-99)
    quantity: int  # number of contracts


class OrderBook(BaseModel):
    """Order book for a market."""
    ticker: str
    yes_bids: list[OrderBookLevel] = Field(default_factory=list)
    yes_asks: list[OrderBookLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> int | None:
        if not self.yes_bids:
            return None
        return max(level.price for level in self.yes_bids)

    @property
    def best_ask(self) -> int | None:
        if not self.yes_asks:
            return None
        return min(level.price for level in self.yes_asks)

    @property
    def mid_price(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread(self) -> int | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid is None or ask is None:
            return None
        return ask - bid

    def depth_at_price(self, price: int, side: str) -> int:
        levels = self.yes_bids if side == "bid" else self.yes_asks
        for level in levels:
            if level.price == price:
                return level.quantity
        return 0


class MarketSnapshot(BaseModel):
    """Combined market info + order book for strategy consumption."""
    market: Market
    orderbook: OrderBook
