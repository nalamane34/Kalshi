"""WebSocket event models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WSMessageType(str, Enum):
    ORDERBOOK_SNAPSHOT = "orderbook_snapshot"
    ORDERBOOK_DELTA = "orderbook_delta"
    TRADE = "trade"
    TICKER = "ticker"
    FILL = "fill"
    ORDER_UPDATE = "order_update"
    SUBSCRIBED = "subscribed"
    ERROR = "error"


class WSMessage(BaseModel):
    """A parsed WebSocket message."""
    type: WSMessageType
    sid: int = 0
    seq: int = 0
    msg: dict[str, Any] = Field(default_factory=dict)
