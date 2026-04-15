"""Tests for data models."""

import pytest
from kalshi_bot.models.market import OrderBook, OrderBookLevel
from kalshi_bot.models.order import OrderRequest, OrderAction, OrderType, Side


class TestOrderBook:
    def test_best_bid(self, sample_orderbook):
        assert sample_orderbook.best_bid == 45

    def test_best_ask(self, sample_orderbook):
        assert sample_orderbook.best_ask == 55

    def test_mid_price(self, sample_orderbook):
        assert sample_orderbook.mid_price == 50.0

    def test_spread(self, sample_orderbook):
        assert sample_orderbook.spread == 10

    def test_empty_orderbook(self):
        ob = OrderBook(ticker="EMPTY")
        assert ob.best_bid is None
        assert ob.best_ask is None
        assert ob.mid_price is None
        assert ob.spread is None

    def test_depth_at_price(self, sample_orderbook):
        assert sample_orderbook.depth_at_price(45, "bid") == 100
        assert sample_orderbook.depth_at_price(55, "ask") == 100
        assert sample_orderbook.depth_at_price(50, "bid") == 0


class TestOrderRequest:
    def test_to_api_payload(self):
        req = OrderRequest(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=Side.YES,
            type=OrderType.LIMIT,
            count=10,
            yes_price=50,
            client_order_id="test-123",
        )
        payload = req.to_api_payload()
        assert payload["ticker"] == "TEST-MKT"
        assert payload["action"] == "buy"
        assert payload["side"] == "yes"
        assert payload["count"] == 10
        assert payload["yes_price"] == 50
        assert payload["client_order_id"] == "test-123"

    def test_market_order_payload(self):
        req = OrderRequest(
            ticker="TEST-MKT",
            action=OrderAction.BUY,
            side=Side.YES,
            type=OrderType.MARKET,
            count=5,
        )
        payload = req.to_api_payload()
        assert "yes_price" not in payload
        assert "no_price" not in payload

    def test_auto_client_order_id(self):
        req1 = OrderRequest(ticker="T", action=OrderAction.BUY, side=Side.YES)
        req2 = OrderRequest(ticker="T", action=OrderAction.BUY, side=Side.YES)
        assert req1.client_order_id != req2.client_order_id
