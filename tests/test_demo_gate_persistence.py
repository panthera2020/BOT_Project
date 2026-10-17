"""
Integration tests: the demo clock and clean-trade count survive a restart, and live
trading unlocks through the API once 24h and 20 clean demo trades are reached.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport

import src.web.app as web
from src.storage.state_store import StateStore

AUTH = ("admin@tradingbot.local", "P@ssword123!")


@pytest.fixture(autouse=True)
def isolated_console_state(monkeypatch):
    """Give each test a clean console and put the globals back afterwards."""
    saved_state = copy.deepcopy(web.RUNTIME_STATE)
    saved_trades = list(web.STATS_TRACKER.trades)
    saved_next_id = web.STATS_TRACKER._next_id
    monkeypatch.setattr(web, "STATE_DB_PATH", "")
    web.RUNTIME_STATE["demo_run_start_time"] = None
    web.RUNTIME_STATE["clean_demo_trade_count"] = 0
    web.RUNTIME_STATE["live_trading_active"] = False
    web.RUNTIME_STATE["demo_testing_active"] = False
    web.RUNTIME_STATE["kill_switch_triggered"] = False
    yield
    web.RUNTIME_STATE.clear()
    web.RUNTIME_STATE.update(saved_state)
    web.STATS_TRACKER.trades = saved_trades
    web.STATS_TRACKER._next_id = saved_next_id


async def _record(n, clean=True):
    for _ in range(n):
        await web.record_demo_trade("BTCUSDT", "Buy", 0.002, 100000.0, 100500.0, 1.0, 1.0, clean=clean)


def _client():
    return AsyncClient(transport=ASGITransport(app=web.app), base_url="http://test")


@pytest.mark.asyncio
async def test_state_store_set_get_and_overwrite(tmp_path):
    store = StateStore(db_path=str(tmp_path / "s.db"))
    await store.init_db()
    assert await store.get_state("missing") is None
    assert await store.get_state("missing", "fallback") == "fallback"
    await store.set_state("k", "1")
    await store.set_state("k", "2")
    assert await store.get_state("k") == "2"


@pytest.mark.asyncio
async def test_clock_does_not_run_until_demo_is_started():
    async with _client() as client:
        data = (await client.get("/api/status", auth=AUTH)).json()
    assert web.RUNTIME_STATE["demo_run_start_time"] is None
    assert data["demo_qualification"]["elapsed_hours"] == 0
    assert not data["demo_qualification"]["is_live_unlocked"]


@pytest.mark.asyncio
async def test_clock_starts_on_first_demo_start_and_is_not_reset():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        first = web.RUNTIME_STATE["demo_run_start_time"]
        assert first is not None
        await client.post("/api/demo/stop", auth=AUTH)
        await client.post("/api/demo/start", auth=AUTH)
        assert web.RUNTIME_STATE["demo_run_start_time"] == first


@pytest.mark.asyncio
async def test_record_demo_trade_counts_only_clean_trades_and_feeds_stats():
    before = web.STATS_TRACKER.get_stats("DEMO")["total_trades"]
    await _record(3)
    await _record(2, clean=False)
    assert web.RUNTIME_STATE["clean_demo_trade_count"] == 3
    assert web.STATS_TRACKER.get_stats("DEMO")["total_trades"] == before + 5


@pytest.mark.asyncio
async def test_clock_and_count_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATE_DB_PATH", str(tmp_path / "bot.db"))
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
    await _record(4)
    started = web.RUNTIME_STATE["demo_run_start_time"]

    # Simulate a restart: memory is wiped, then the app's startup hook runs.
    web.RUNTIME_STATE["demo_run_start_time"] = None
    web.RUNTIME_STATE["clean_demo_trade_count"] = 0
    async with web.app.router.lifespan_context(web.app):
        pass

    assert web.RUNTIME_STATE["demo_run_start_time"] == started
    assert web.RUNTIME_STATE["clean_demo_trade_count"] == 4


@pytest.mark.asyncio
async def test_storage_failure_never_breaks_the_console(monkeypatch):
    monkeypatch.setattr(web, "STATE_DB_PATH", "/nonexistent-dir/deeper/bot.db")
    async with _client() as client:
        res = await client.post("/api/demo/start", auth=AUTH)
        assert res.status_code == 200
    await _record(2)
    assert web.RUNTIME_STATE["clean_demo_trade_count"] == 2
    await web.load_persisted_state()  # must not raise


@pytest.mark.asyncio
async def test_live_unlocks_after_24h_and_20_clean_demo_trades():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        web.RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=24, minutes=1)
        await _record(20)

        status = (await client.get("/api/status", auth=AUTH)).json()["demo_qualification"]
        assert status["is_live_unlocked"]
        assert status["clean_trades"] == 20

        res = await client.post("/api/live/start", auth=AUTH)
        assert res.status_code == 200
        assert web.RUNTIME_STATE["live_trading_active"]


@pytest.mark.asyncio
async def test_live_stays_locked_with_19_trades_or_under_24h():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)

        # 25 hours but only 19 trades
        web.RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=25)
        await _record(19)
        assert (await client.post("/api/live/start", auth=AUTH)).status_code == 403

        # 20 trades but only 23h59m
        await _record(1)
        web.RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=23, minutes=59)
        assert (await client.post("/api/live/start", auth=AUTH)).status_code == 403
        assert not web.RUNTIME_STATE["live_trading_active"]
