"""Async REST client for the Kalshi API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import aiohttp
import structlog

from kalshi_bot.client.auth import KalshiAuth
from kalshi_bot.client.rate_limiter import DualRateLimiter
from kalshi_bot.models.market import Market, OrderBook, OrderBookLevel, MarketSnapshot
from kalshi_bot.models.order import (
    Fill, Order, OrderAction, OrderRequest, OrderStatus, OrderType, Side,
)
from kalshi_bot.models.portfolio import Balance, Position


class KalshiAPIError(Exception):
    """Base API error."""
    def __init__(self, status: int, message: str, detail: str = ""):
        self.status = status
        self.message = message
        self.detail = detail
        super().__init__(f"HTTP {status}: {message} - {detail}")


class KalshiAuthError(KalshiAPIError):
    pass


class KalshiRateLimitError(KalshiAPIError):
    pass


class KalshiNotFoundError(KalshiAPIError):
    pass


class KalshiRestClient:
    """Full async REST client for the Kalshi trading API."""

    def __init__(
        self,
        base_url: str,
        auth: KalshiAuth,
        read_rate_limit: int = 20,
        write_rate_limit: int = 10,
        request_timeout: int = 10,
        max_retries: int = 3,
        retry_backoff_base: float = 1.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._rate_limiter = DualRateLimiter(read_rate_limit, write_rate_limit)
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_base
        self._session: aiohttp.ClientSession | None = None
        self._log = structlog.get_logger("kalshi.rest")

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        self._log.info("rest_client_started", base_url=self._base_url)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
            self._log.info("rest_client_closed")

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Core request with auth, rate limiting, retries, error handling."""
        if not self._session:
            raise RuntimeError("Client not started. Call start() first.")

        # Rate limit
        is_write = method.upper() in ("POST", "PUT", "PATCH", "DELETE")
        if is_write:
            await self._rate_limiter.acquire_write()
        else:
            await self._rate_limiter.acquire_read()

        url = f"{self._base_url}{path}"
        # Auth: sign with full API path (includes /trade-api/v2 prefix)
        parsed = urlparse(self._base_url)
        sign_path = parsed.path + path

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            auth_headers = self._auth.sign_request(method.upper(), sign_path)
            headers = {
                **auth_headers,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            try:
                async with self._session.request(
                    method, url, headers=headers, params=params, json=json_body,
                ) as resp:
                    body = await resp.json() if resp.content_length != 0 else {}

                    if resp.status == 200 or resp.status == 201:
                        return body

                    error_msg = body.get("message", body.get("error", "Unknown error"))
                    error_detail = body.get("code", "")

                    if resp.status == 401:
                        raise KalshiAuthError(401, error_msg, error_detail)
                    if resp.status == 404:
                        raise KalshiNotFoundError(404, error_msg, error_detail)
                    if resp.status == 429:
                        last_error = KalshiRateLimitError(429, error_msg, error_detail)
                        wait = self._retry_backoff * (2 ** attempt)
                        self._log.warning("rate_limited", attempt=attempt, wait=wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500:
                        last_error = KalshiAPIError(resp.status, error_msg, error_detail)
                        wait = self._retry_backoff * (2 ** attempt)
                        self._log.warning("server_error", status=resp.status, attempt=attempt, wait=wait)
                        await asyncio.sleep(wait)
                        continue

                    raise KalshiAPIError(resp.status, error_msg, error_detail)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < self._max_retries:
                    wait = self._retry_backoff * (2 ** attempt)
                    self._log.warning("request_error", error=str(e), attempt=attempt, wait=wait)
                    await asyncio.sleep(wait)

        raise last_error or KalshiAPIError(0, "Max retries exceeded")

    # ─── Market Data ─────────────────────────────────────────────

    async def get_markets(
        self,
        limit: int = 200,
        cursor: str | None = None,
        status: str | None = "open",
        **filters: Any,
    ) -> tuple[list[Market], str | None]:
        """Fetch markets. Returns (markets, next_cursor)."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if status:
            params["status"] = status
        params.update(filters)

        data = await self._request("GET", "/markets", params=params)
        markets = [self._parse_market(m) for m in data.get("markets", [])]
        next_cursor = data.get("cursor") or None
        return markets, next_cursor

    async def get_market(self, ticker: str) -> Market:
        """Fetch a single market."""
        data = await self._request("GET", f"/markets/{ticker}")
        return self._parse_market(data.get("market", data))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> OrderBook:
        """Fetch the order book for a market."""
        params = {"depth": depth}
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return self._parse_orderbook(ticker, data)

    async def get_trades(self, ticker: str, limit: int = 100) -> list[dict]:
        """Fetch recent trades for a market."""
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        data = await self._request("GET", "/markets/trades", params=params)
        return data.get("trades", [])

    async def get_snapshot(self, ticker: str) -> MarketSnapshot:
        """Fetch market + orderbook as a combined snapshot."""
        market, orderbook = await asyncio.gather(
            self.get_market(ticker),
            self.get_orderbook(ticker),
        )
        return MarketSnapshot(market=market, orderbook=orderbook)

    # ─── Portfolio ────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        """Fetch account balance."""
        data = await self._request("GET", "/portfolio/balance")
        return Balance(
            available_balance=data.get("balance", 0),
            portfolio_value=data.get("portfolio_value", 0),
        )

    async def get_positions(
        self,
        limit: int = 200,
        cursor: str | None = None,
        settlement_status: str = "unsettled",
    ) -> list[Position]:
        """Fetch open positions."""
        params: dict[str, Any] = {"limit": limit, "settlement_status": settlement_status}
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/positions", params=params)
        positions = []
        for mp in data.get("market_positions", []):
            positions.append(Position(
                ticker=mp.get("ticker", ""),
                position=int(float(mp.get("position_fp", "0"))),
                market_exposure=mp.get("market_exposure_dollars", mp.get("market_exposure", 0)),
                realized_pnl=mp.get("realized_pnl_dollars", mp.get("realized_pnl", 0)),
                total_traded=mp.get("total_traded_dollars", mp.get("total_traded", 0)),
                resting_orders_count=mp.get("resting_orders_count", 0),
                fees_paid=mp.get("fees_paid_dollars", mp.get("fees_paid", 0)),
            ))
        return positions

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[Order]:
        """Fetch orders."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [self._parse_order(o) for o in data.get("orders", [])]

    async def get_fills(
        self,
        ticker: str | None = None,
        limit: int = 100,
    ) -> list[Fill]:
        """Fetch fill history."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = await self._request("GET", "/portfolio/fills", params=params)
        return [self._parse_fill(f) for f in data.get("fills", [])]

    # ─── Trading ──────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> Order:
        """Place a new order."""
        payload = request.to_api_payload()
        self._log.info(
            "placing_order",
            ticker=request.ticker,
            side=request.side.value,
            action=request.action.value,
            price=request.yes_price or request.no_price,
            count=request.count,
        )
        data = await self._request("POST", "/portfolio/orders", json_body=payload)
        order_data = data.get("order", data)
        order = self._parse_order(order_data)
        order.strategy_name = request.strategy_name
        return order

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting order."""
        self._log.info("canceling_order", order_id=order_id)
        await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def batch_cancel_orders(self, order_ids: list[str]) -> list[str]:
        """Cancel multiple orders. Returns IDs that failed to cancel."""
        failed = []
        for order_id in order_ids:
            try:
                await self.cancel_order(order_id)
            except KalshiNotFoundError:
                # Already filled or cancelled
                pass
            except KalshiAPIError as e:
                self._log.warning("cancel_failed", order_id=order_id, error=str(e))
                failed.append(order_id)
        return failed

    async def amend_order(
        self,
        order_id: str,
        price: int | None = None,
        count: int | None = None,
    ) -> Order:
        """Amend a resting order (cancel + replace if PATCH not supported)."""
        body: dict[str, Any] = {}
        if price is not None:
            body["price"] = price
        if count is not None:
            body["count"] = count
        data = await self._request("PATCH", f"/portfolio/orders/{order_id}", json_body=body)
        return self._parse_order(data.get("order", data))

    # ─── Parsers ──────────────────────────────────────────────────

    @staticmethod
    def _parse_market(raw: dict) -> Market:
        return Market(
            ticker=raw.get("ticker", ""),
            event_ticker=raw.get("event_ticker", ""),
            title=raw.get("title", raw.get("yes_sub_title", "")),
            subtitle=raw.get("subtitle", raw.get("no_sub_title", "")),
            status=raw.get("status", "open"),
            open_time=_parse_ts(raw.get("open_time")),
            close_time=_parse_ts(raw.get("close_time")),
            yes_bid=_cents(raw.get("yes_bid", raw.get("yes_bid_dollars"))),
            yes_ask=_cents(raw.get("yes_ask", raw.get("yes_ask_dollars"))),
            no_bid=_cents(raw.get("no_bid", raw.get("no_bid_dollars"))),
            no_ask=_cents(raw.get("no_ask", raw.get("no_ask_dollars"))),
            last_price=_cents(raw.get("last_price", raw.get("last_price_dollars"))),
            volume=int(float(raw.get("volume", raw.get("volume_fp", 0)))),
            open_interest=int(float(raw.get("open_interest", raw.get("open_interest_fp", 0)))),
            liquidity=_cents(raw.get("liquidity", raw.get("liquidity_dollars"))),
            result=raw.get("result"),
        )

    @staticmethod
    def _parse_orderbook(ticker: str, raw: dict) -> OrderBook:
        ob = raw.get("orderbook", raw.get("orderbook_fp", raw))
        yes_bids = []
        yes_asks = []

        # Parse yes side bids
        for entry in ob.get("yes", ob.get("yes_dollars", [])):
            if isinstance(entry, list) and len(entry) >= 2:
                price = int(round(float(entry[0]) * 100)) if float(entry[0]) < 1.0 else int(float(entry[0]))
                qty = int(float(entry[1]))
                yes_bids.append(OrderBookLevel(price=price, quantity=qty))

        # Parse no side as yes_asks (a NO bid at X = YES ask at 100-X)
        for entry in ob.get("no", ob.get("no_dollars", [])):
            if isinstance(entry, list) and len(entry) >= 2:
                price_raw = float(entry[0])
                price = int(round(price_raw * 100)) if price_raw < 1.0 else int(price_raw)
                yes_ask_price = 100 - price
                qty = int(float(entry[1]))
                yes_asks.append(OrderBookLevel(price=yes_ask_price, quantity=qty))

        # Sort: bids descending, asks ascending
        yes_bids.sort(key=lambda x: x.price, reverse=True)
        yes_asks.sort(key=lambda x: x.price)

        return OrderBook(ticker=ticker, yes_bids=yes_bids, yes_asks=yes_asks)

    @staticmethod
    def _parse_order(raw: dict) -> Order:
        return Order(
            order_id=raw.get("order_id", ""),
            client_order_id=raw.get("client_order_id", ""),
            ticker=raw.get("ticker", ""),
            action=OrderAction(raw.get("action", "buy")),
            side=Side(raw.get("side", "yes")),
            type=OrderType(raw.get("type", "limit")),
            status=OrderStatus(raw.get("status", "pending")),
            yes_price=_cents(raw.get("yes_price", raw.get("yes_price_dollars"))),
            no_price=_cents(raw.get("no_price", raw.get("no_price_dollars"))),
            created_time=_parse_ts(raw.get("created_time")),
            updated_time=_parse_ts(raw.get("updated_time")),
            expiration_time=_parse_ts(raw.get("expiration_time")),
            initial_count=int(float(raw.get("initial_count", raw.get("initial_count_fp", "0")))),
            remaining_count=int(float(raw.get("remaining_count", raw.get("remaining_count_fp", "0")))),
            fill_count=int(float(raw.get("fill_count", raw.get("fill_count_fp", "0")))),
        )

    @staticmethod
    def _parse_fill(raw: dict) -> Fill:
        return Fill(
            trade_id=raw.get("trade_id", ""),
            order_id=raw.get("order_id", ""),
            ticker=raw.get("ticker", ""),
            action=OrderAction(raw.get("action", "buy")),
            side=Side(raw.get("side", "yes")),
            count=int(float(raw.get("count", raw.get("count_fp", "0")))),
            yes_price=_cents(raw.get("yes_price", raw.get("yes_price_dollars"))),
            no_price=_cents(raw.get("no_price", raw.get("no_price_dollars"))),
            created_time=_parse_ts(raw.get("created_time")),
        )


def _parse_ts(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # Millisecond timestamp
        if val > 1e12:
            return datetime.fromtimestamp(val / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(val, tz=timezone.utc)
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _cents(val: Any) -> int:
    """Convert various price formats to integer cents."""
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        f = float(val)
        # If looks like dollars (0.xx), convert to cents
        if 0 < f < 1.0:
            return int(round(f * 100))
        return int(round(f))
    if isinstance(val, float):
        if 0 < val < 1.0:
            return int(round(val * 100))
        return int(round(val))
    return 0
