"""Live portfolio state tracker."""

from __future__ import annotations

import asyncio

import structlog

from kalshi_bot.client.rest import KalshiRestClient
from kalshi_bot.models.order import Order, Fill, OrderStatus, OrderAction, Side
from kalshi_bot.models.portfolio import Balance, Position
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.storage.database import Database


class PortfolioTracker:
    """Aggregates API data, fills, and local bookkeeping into live state."""

    def __init__(
        self,
        rest_client: KalshiRestClient,
        risk_manager: RiskManager,
        database: Database,
    ):
        self._rest = rest_client
        self._risk = risk_manager
        self._db = database
        self._log = structlog.get_logger("kalshi.portfolio")

        self._balance: Balance = Balance()
        self._positions: dict[str, Position] = {}
        self._open_orders: dict[str, Order] = {}  # order_id -> Order

    async def sync(self) -> None:
        """Full sync from API. Called on startup and periodically."""
        try:
            balance, positions, orders = await asyncio.gather(
                self._rest.get_balance(),
                self._rest.get_positions(),
                self._rest.get_orders(status="resting"),
            )

            self._balance = balance
            self._positions = {p.ticker: p for p in positions}
            self._open_orders = {o.order_id: o for o in orders}

            # Update risk manager
            self._risk.update_balance(balance.available_balance)
            pos_map = {p.ticker: p.position for p in positions}
            self._risk._positions = pos_map
            self._risk.set_open_order_count(len(orders))

            self._log.info(
                "portfolio_synced",
                balance=balance.available_dollars,
                positions=len(self._positions),
                open_orders=len(self._open_orders),
            )
        except Exception as e:
            self._log.error("portfolio_sync_failed", error=str(e))

    async def on_fill(self, fill: Fill) -> None:
        """Handle a fill event."""
        # Update position tracking
        if fill.action == OrderAction.BUY:
            delta = fill.count if fill.side == Side.YES else -fill.count
        else:
            delta = -fill.count if fill.side == Side.YES else fill.count

        self._risk.update_position(fill.ticker, delta)

        # Record in database
        await self._db.record_fill({
            "trade_id": fill.trade_id,
            "order_id": fill.order_id,
            "ticker": fill.ticker,
            "action": fill.action.value,
            "side": fill.side.value,
            "count": fill.count,
            "price": fill.yes_price or fill.no_price,
            "created_at": fill.created_time.isoformat() if fill.created_time else "",
        })

        self._log.info(
            "fill_recorded",
            ticker=fill.ticker,
            action=fill.action.value,
            side=fill.side.value,
            count=fill.count,
            price=fill.yes_price,
        )

    async def on_order_update(self, order: Order) -> None:
        """Handle order status change."""
        if order.status in (OrderStatus.EXECUTED, OrderStatus.CANCELED):
            self._open_orders.pop(order.order_id, None)
            self._risk.unregister_order()
        elif order.status == OrderStatus.RESTING:
            self._open_orders[order.order_id] = order

        await self._db.update_order_status(order.order_id, order.status.value)

    def get_position(self, ticker: str) -> int:
        """Net position contracts for a ticker. 0 if none."""
        pos = self._positions.get(ticker)
        if pos:
            return pos.position
        return self._risk.get_position(ticker)

    def get_open_orders(self, ticker: str) -> list[Order]:
        """All resting orders for a ticker."""
        return [o for o in self._open_orders.values() if o.ticker == ticker]

    def get_all_open_order_ids(self) -> list[str]:
        """All resting order IDs."""
        return list(self._open_orders.keys())

    @property
    def balance(self) -> Balance:
        return self._balance

    @property
    def total_exposure(self) -> int:
        """Sum of absolute position values (cents)."""
        return sum(abs(p.market_exposure) for p in self._positions.values())
