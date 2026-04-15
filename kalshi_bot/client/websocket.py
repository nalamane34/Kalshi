"""Async WebSocket client for Kalshi real-time data."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import structlog
import websockets
from websockets.asyncio.client import connect as ws_connect

from kalshi_bot.client.auth import KalshiAuth


class KalshiWSClient:
    """WebSocket client with auto-reconnect and subscription management."""

    def __init__(self, ws_url: str, auth: KalshiAuth):
        self._ws_url = ws_url
        self._auth = auth
        self._ws: Any = None
        self._subscriptions: dict[str, set[str]] = {}  # channel -> {tickers}
        self._callbacks: dict[str, list[Callable]] = {}
        self._running = False
        self._cmd_id = 0
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._receive_task: asyncio.Task | None = None
        self._log = structlog.get_logger("kalshi.ws")

    async def connect(self) -> None:
        """Connect to WebSocket with authentication headers."""
        sign_path = "/trade-api/ws/v2"
        auth_headers = self._auth.sign_request("GET", sign_path)

        self._ws = await ws_connect(
            self._ws_url,
            additional_headers=auth_headers,
        )
        self._running = True
        self._reconnect_delay = 1.0
        self._log.info("ws_connected", url=self._ws_url)
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def disconnect(self) -> None:
        """Clean shutdown."""
        self._running = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._log.info("ws_disconnected")

    async def subscribe(self, channels: list[str], tickers: list[str]) -> None:
        """Subscribe to channels for specific market tickers."""
        self._cmd_id += 1
        msg = {
            "id": self._cmd_id,
            "cmd": "subscribe",
            "params": {
                "channels": channels,
                "market_tickers": tickers,
            },
        }
        if self._ws:
            await self._ws.send(json.dumps(msg))
            for ch in channels:
                if ch not in self._subscriptions:
                    self._subscriptions[ch] = set()
                self._subscriptions[ch].update(tickers)
            self._log.info("ws_subscribed", channels=channels, tickers=tickers)

    async def unsubscribe(self, channels: list[str], tickers: list[str]) -> None:
        """Unsubscribe from channels."""
        self._cmd_id += 1
        msg = {
            "id": self._cmd_id,
            "cmd": "unsubscribe",
            "params": {
                "channels": channels,
                "market_tickers": tickers,
            },
        }
        if self._ws:
            await self._ws.send(json.dumps(msg))
            for ch in channels:
                if ch in self._subscriptions:
                    self._subscriptions[ch] -= set(tickers)

    def on(self, event_type: str, callback: Callable) -> None:
        """Register a callback for a specific event type."""
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)

    async def _receive_loop(self) -> None:
        """Main receive loop with auto-reconnect."""
        while self._running:
            try:
                async for raw_msg in self._ws:
                    if not self._running:
                        break
                    try:
                        data = json.loads(raw_msg)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        self._log.warning("ws_invalid_json", raw=raw_msg[:200])
                    except Exception as e:
                        self._log.error("ws_handler_error", error=str(e))
            except websockets.exceptions.ConnectionClosed as e:
                if not self._running:
                    break
                self._log.warning("ws_connection_closed", code=e.code, reason=e.reason)
            except Exception as e:
                if not self._running:
                    break
                self._log.error("ws_error", error=str(e))

            if self._running:
                await self._reconnect()

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff and re-subscribe."""
        while self._running:
            self._log.info("ws_reconnecting", delay=self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self._max_reconnect_delay,
            )
            try:
                sign_path = "/trade-api/ws/v2"
                auth_headers = self._auth.sign_request("GET", sign_path)
                self._ws = await ws_connect(
                    self._ws_url,
                    additional_headers=auth_headers,
                )
                self._reconnect_delay = 1.0
                self._log.info("ws_reconnected")

                # Re-subscribe to all active channels
                for channel, tickers in self._subscriptions.items():
                    if tickers:
                        await self.subscribe([channel], list(tickers))
                return

            except Exception as e:
                self._log.warning("ws_reconnect_failed", error=str(e))

    async def _handle_message(self, data: dict) -> None:
        """Parse and dispatch a WebSocket message."""
        msg_type = data.get("type", "")
        sid = data.get("sid", 0)
        seq = data.get("seq", 0)
        msg = data.get("msg", data)

        # Dispatch to registered callbacks
        callbacks = self._callbacks.get(msg_type, [])
        for cb in callbacks:
            try:
                result = cb(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._log.error("ws_callback_error", type=msg_type, error=str(e))

        # Also dispatch to wildcard handlers
        for cb in self._callbacks.get("*", []):
            try:
                result = cb(data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                self._log.error("ws_wildcard_error", error=str(e))
