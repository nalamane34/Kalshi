"""Order and fill models."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class OrderAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    RESTING = "resting"
    CANCELED = "canceled"
    EXECUTED = "executed"
    PENDING = "pending"


class OrderRequest(BaseModel):
    """Request to place an order."""
    ticker: str
    action: OrderAction
    side: Side
    type: OrderType = OrderType.LIMIT
    count: int = 1
    yes_price: Optional[int] = None    # cents (1-99)
    no_price: Optional[int] = None     # cents (1-99)
    expiration_ts: Optional[int] = None
    client_order_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    post_only: bool = False
    strategy_name: str = ""  # internal tracking, not sent to API

    def to_api_payload(self) -> dict:
        payload: dict = {
            "ticker": self.ticker,
            "action": self.action.value,
            "side": self.side.value,
            "count": self.count,
            "type": self.type.value,
            "client_order_id": self.client_order_id,
        }
        if self.yes_price is not None:
            payload["yes_price"] = self.yes_price
        if self.no_price is not None:
            payload["no_price"] = self.no_price
        if self.expiration_ts is not None:
            payload["expiration_ts"] = self.expiration_ts
        if self.post_only:
            payload["post_only"] = True
        return payload


class Order(BaseModel):
    """An order returned from the API."""
    order_id: str = ""
    client_order_id: str = ""
    ticker: str = ""
    action: OrderAction = OrderAction.BUY
    side: Side = Side.YES
    type: OrderType = OrderType.LIMIT
    status: OrderStatus = OrderStatus.PENDING
    yes_price: int = 0
    no_price: int = 0
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    initial_count: int = 0
    remaining_count: int = 0
    fill_count: int = 0
    strategy_name: str = ""  # internal tracking


class Fill(BaseModel):
    """A trade fill."""
    trade_id: str = ""
    order_id: str = ""
    ticker: str = ""
    action: OrderAction = OrderAction.BUY
    side: Side = Side.YES
    count: int = 0
    yes_price: int = 0
    no_price: int = 0
    created_time: Optional[datetime] = None
