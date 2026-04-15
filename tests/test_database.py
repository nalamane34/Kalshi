"""Tests for SQLite database layer."""

import pytest
import json

from kalshi_bot.storage.database import Database


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_record_and_get_order(db):
    order = {
        "order_id": "ord-1",
        "client_order_id": "client-1",
        "ticker": "TEST",
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "price": 50,
        "count": 10,
        "status": "resting",
        "strategy_name": "market_maker",
    }
    await db.record_order(order)
    orders = await db.get_orders(ticker="TEST")
    assert len(orders) == 1
    assert orders[0]["order_id"] == "ord-1"
    assert orders[0]["ticker"] == "TEST"


@pytest.mark.asyncio
async def test_update_order_status(db):
    await db.record_order({
        "order_id": "ord-2",
        "client_order_id": "client-2",
        "ticker": "TEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "status": "resting",
    })
    await db.update_order_status("ord-2", "executed")
    orders = await db.get_orders(status="executed")
    assert len(orders) == 1
    assert orders[0]["status"] == "executed"


@pytest.mark.asyncio
async def test_record_and_get_fill(db):
    fill = {
        "trade_id": "fill-1",
        "order_id": "ord-1",
        "ticker": "TEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price": 50,
    }
    await db.record_fill(fill)
    fills = await db.get_fills(ticker="TEST")
    assert len(fills) == 1
    assert fills[0]["trade_id"] == "fill-1"


@pytest.mark.asyncio
async def test_pnl_snapshot(db):
    await db.record_pnl_snapshot(
        balance=100_000,
        portfolio_value=100_000,
        realized_pnl=500,
        total_positions=3,
    )
    # Should have at least one snapshot
    from datetime import datetime, timezone
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshots = await db.get_daily_pnl(date)
    assert len(snapshots) >= 1
    assert snapshots[0]["balance_cents"] == 100_000


@pytest.mark.asyncio
async def test_strategy_state_roundtrip(db):
    state = json.dumps({"inventory": {"TEST": 5}})
    await db.save_strategy_state("market_maker", "TEST", state)
    loaded = await db.load_strategy_state("market_maker", "TEST")
    assert loaded == state
    parsed = json.loads(loaded)
    assert parsed["inventory"]["TEST"] == 5


@pytest.mark.asyncio
async def test_strategy_state_not_found(db):
    result = await db.load_strategy_state("nonexistent", "NONE")
    assert result is None


@pytest.mark.asyncio
async def test_duplicate_fill_ignored(db):
    fill = {
        "trade_id": "fill-dup",
        "order_id": "ord-1",
        "ticker": "TEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price": 50,
    }
    await db.record_fill(fill)
    await db.record_fill(fill)  # duplicate
    fills = await db.get_fills()
    fill_ids = [f["trade_id"] for f in fills if f["trade_id"] == "fill-dup"]
    assert len(fill_ids) == 1
