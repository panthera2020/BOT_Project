"""
Unit tests for the state store pieces that keep the console's state across restarts:
all-or-nothing saves and the trade history table.
"""
import pytest
import pytest_asyncio
import aiosqlite

from src.core.trade_stats import TradeStatsTracker
from src.storage.state_store import StateStore


def _trade(i, mode="DEMO", win=True):
    return {
        "id": i, "mode": mode, "symbol": "BTCUSDT", "side": "Buy", "qty": 0.002,
        "entry_price": 100000.0, "exit_price": 100500.0, "pnl_usd": 1.0 if win else -1.0,
        "pnl_r": 1.0 if win else -1.0, "is_win": win, "status": "CLOSED",
        "timestamp": "2026-10-17 12:00:00",
    }


@pytest_asyncio.fixture
async def store(tmp_path):
    s = StateStore(db_path=str(tmp_path / "s.db"))
    await s.init_db()
    return s


@pytest.mark.asyncio
async def test_set_many_saves_all_keys_and_overwrites(store):
    await store.set_many({"a": "1", "b": "2"})
    await store.set_many({"a": "10"})
    assert await store.get_state("a") == "10"
    assert await store.get_state("b") == "2"


@pytest.mark.asyncio
async def test_set_many_is_all_or_nothing(store):
    # A non-text value makes the second statement fail; nothing may be saved.
    with pytest.raises(Exception):
        await store.set_many({"good": "1", "bad": object()})
    assert await store.get_state("good") is None


@pytest.mark.asyncio
async def test_delete_state(store):
    await store.set_state("k", "v")
    await store.delete_state("k")
    assert await store.get_state("k") is None


@pytest.mark.asyncio
async def test_save_trade_and_state_together_and_load_in_order(store):
    await store.save_trade(_trade(2, win=False), {"clean": "2"})
    await store.save_trade(_trade(1), {"clean": "1"})
    trades = await store.load_trades()
    assert [t["id"] for t in trades] == [1, 2]
    assert trades[0]["is_win"] is True and trades[1]["is_win"] is False
    assert await store.get_state("clean") == "1"  # last write wins


@pytest.mark.asyncio
async def test_save_trade_is_all_or_nothing(store):
    with pytest.raises(Exception):
        await store.save_trade(_trade(1), {"clean": object()})
    assert await store.load_trades() == []


@pytest.mark.asyncio
async def test_saving_the_same_trade_id_twice_keeps_one_row(store):
    await store.save_trade(_trade(5))
    await store.save_trade(_trade(5))
    assert len(await store.load_trades()) == 1


@pytest.mark.asyncio
async def test_old_database_without_trade_history_is_upgraded(tmp_path):
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("INSERT INTO system_state (key, value) VALUES ('keep', 'me')")
        await db.commit()
    s = StateStore(db_path=path)
    await s.init_db()
    assert await s.get_state("keep") == "me"
    assert await s.load_trades() == []


def test_tracker_restore_orders_newest_first_and_continues_ids():
    tracker = TradeStatsTracker()
    tracker.restore_trades([_trade(1), _trade(3), _trade(2)])
    assert [t.id for t in tracker.trades] == [3, 2, 1]
    new = tracker.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.002, 1.0, 2.0, 1.0, 1.0)
    assert new.id == 4
    assert tracker.get_stats("DEMO")["total_trades"] == 4


def test_tracker_restore_empty_resets_ids():
    tracker = TradeStatsTracker()
    tracker.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.002, 1.0, 2.0, 1.0, 1.0)
    tracker.restore_trades([])
    assert tracker.trades == []
    assert tracker.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.002, 1.0, 2.0, 1.0, 1.0).id == 1
