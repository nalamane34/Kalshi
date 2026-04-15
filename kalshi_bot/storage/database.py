"""SQLite persistence for trades, PnL, and strategy state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog


class Database:
    """Async SQLite database for trade logging and state recovery."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._log = structlog.get_logger("kalshi.db")

    async def initialize(self) -> None:
        """Open connection and create tables."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()
        self._log.info("database_initialized", path=self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL,
                type TEXT NOT NULL,
                price INTEGER,
                count INTEGER NOT NULL,
                status TEXT NOT NULL,
                strategy_name TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fills (
                trade_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                price INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pnl_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance_cents INTEGER NOT NULL,
                portfolio_value_cents INTEGER NOT NULL,
                realized_pnl_cents INTEGER NOT NULL,
                total_positions INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_state (
                strategy_name TEXT NOT NULL,
                ticker TEXT NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (strategy_name, ticker)
            );

            CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_fills_ticker ON fills(ticker);
            CREATE INDEX IF NOT EXISTS idx_pnl_timestamp ON pnl_snapshots(timestamp);
        """)

    # ─── Orders ───────────────────────────────────────────────────

    async def record_order(self, order: dict) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO orders
               (order_id, client_order_id, ticker, action, side, type, price, count, status, strategy_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.get("order_id", ""),
                order.get("client_order_id", ""),
                order.get("ticker", ""),
                order.get("action", ""),
                order.get("side", ""),
                order.get("type", "limit"),
                order.get("price", 0),
                order.get("count", 0),
                order.get("status", "pending"),
                order.get("strategy_name", ""),
                order.get("created_at", now),
                now,
            ),
        )
        await self._db.commit()

    async def update_order_status(self, order_id: str, status: str) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
            (status, now, order_id),
        )
        await self._db.commit()

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        assert self._db is not None
        query = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        async with self._db.execute(query, params) as cursor:
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) async for row in cursor]

    # ─── Fills ────────────────────────────────────────────────────

    async def record_fill(self, fill: dict) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR IGNORE INTO fills
               (trade_id, order_id, ticker, action, side, count, price, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fill.get("trade_id", ""),
                fill.get("order_id", ""),
                fill.get("ticker", ""),
                fill.get("action", ""),
                fill.get("side", ""),
                fill.get("count", 0),
                fill.get("price", 0),
                fill.get("created_at", now),
            ),
        )
        await self._db.commit()

    async def get_fills(
        self,
        ticker: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        assert self._db is not None
        query = "SELECT * FROM fills WHERE 1=1"
        params: list[Any] = []
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC"
        async with self._db.execute(query, params) as cursor:
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) async for row in cursor]

    # ─── PnL Snapshots ───────────────────────────────────────────

    async def record_pnl_snapshot(
        self,
        balance: int,
        portfolio_value: int,
        realized_pnl: int,
        total_positions: int,
    ) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO pnl_snapshots
               (timestamp, balance_cents, portfolio_value_cents, realized_pnl_cents, total_positions)
               VALUES (?, ?, ?, ?, ?)""",
            (now, balance, portfolio_value, realized_pnl, total_positions),
        )
        await self._db.commit()

    async def get_daily_pnl(self, date: str) -> list[dict]:
        """Get PnL snapshots for a specific date (YYYY-MM-DD)."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM pnl_snapshots WHERE timestamp LIKE ? ORDER BY timestamp",
            (f"{date}%",),
        ) as cursor:
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) async for row in cursor]

    # ─── Strategy State ──────────────────────────────────────────

    async def save_strategy_state(self, name: str, ticker: str, state_json: str) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO strategy_state
               (strategy_name, ticker, state_json, updated_at)
               VALUES (?, ?, ?, ?)""",
            (name, ticker, state_json, now),
        )
        await self._db.commit()

    async def load_strategy_state(self, name: str, ticker: str) -> str | None:
        assert self._db is not None
        async with self._db.execute(
            "SELECT state_json FROM strategy_state WHERE strategy_name = ? AND ticker = ?",
            (name, ticker),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None
