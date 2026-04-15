"""Portfolio models."""

from __future__ import annotations

from pydantic import BaseModel


class Balance(BaseModel):
    """Account balance."""
    available_balance: int = 0    # cents
    portfolio_value: int = 0      # cents

    @property
    def available_dollars(self) -> float:
        return self.available_balance / 100.0

    @property
    def portfolio_dollars(self) -> float:
        return self.portfolio_value / 100.0


class Position(BaseModel):
    """A position in a market."""
    ticker: str = ""
    position: int = 0                   # positive=YES, negative=NO
    market_exposure: int = 0            # cents
    realized_pnl: int = 0              # cents
    total_traded: int = 0              # cents
    resting_orders_count: int = 0
    fees_paid: int = 0                 # cents
