"""
The trading engine inside the web console: Start Demo starts it, Pause stops it, the kill
switch closes the trade, a restart carries on with the open trade, and it never starts
without keys or when switched off.
"""
import asyncio
import json

import pytest

from src import web  # noqa: F401  (package import check)
from src.engine.demo_engine import DemoEngine
from src.storage.state_store import StateStore
from src.web import app as web_app
from tests.sim_exchange import Clock, SimExchange


async def fast_sleep(seconds):
    await asyncio.sleep(0.005)


@pytest.fixture
def sim_engine(monkeypatch):
    """Engine switched on, talking to a simulated Bybit instead of the real one."""
    monkeypatch.setenv("BOT_ENGINE", "on")
    clock = Clock()
    sim = SimExchange(clock=clock)
    web_app.RUNTIME_STATE["bybit_demo_keys"] = {"api_key": "", "api_secret": ""}
    monkeypatch.setattr(
        web_app, "_new_engine",
        lambda: DemoEngine(sim, web_app.RUNTIME_STATE, web_app.save_console_state, web_app.record_demo_trade,
                           None, now_fn=clock.now, sleep=fast_sleep, poll_seconds=0.005, position_poll_seconds=0.005),
    )
    yield sim
    asyncio.get_event_loop_policy()  # nothing to do; engine is stopped by each test


async def wait_for(predicate, seconds=3.0):
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def save_keys():
    await web_app.save_api_keys(web_app.APIKeyPayload(account_type="demo", api_key="K", api_secret="S"), user="t")


@pytest.mark.asyncio
async def test_start_demo_without_keys_says_so_and_does_not_trade(sim_engine):
    out = await web_app.start_demo_testing(user="t")
    assert "no bybit demo api keys" in out["message"].lower()
    assert out["engine"]["running"] is False
    status = await web_app.get_system_status(user="t")
    assert "no bybit demo api keys" in status["market_radar"]["current_market_status"].lower()
    assert sim_engine.placed == []


@pytest.mark.asyncio
async def test_start_demo_with_keys_trades_and_status_shows_the_engine(sim_engine):
    await save_keys()
    await web_app.start_demo_testing(user="t")
    try:
        assert await wait_for(lambda: web_app.RUNTIME_STATE["active_position"] is not None)
        status = await web_app.get_system_status(user="t")
        assert status["engine"]["running"] is True
        assert status["market_radar"]["is_actively_scanning"] is True
        assert status["active_position"]["symbol"] == "BTCUSDT"
        assert "ENTERED" in status["market_radar"]["last_decision"]
    finally:
        await web_app._stop_engine()


@pytest.mark.asyncio
async def test_saving_keys_while_demo_is_on_starts_the_engine(sim_engine):
    await web_app.start_demo_testing(user="t")
    assert sim_engine.placed == []
    await save_keys()
    try:
        assert await wait_for(lambda: web_app.RUNTIME_STATE["active_position"] is not None)
    finally:
        await web_app._stop_engine()


@pytest.mark.asyncio
async def test_pause_stops_the_engine_and_keeps_the_open_trade(sim_engine):
    await save_keys()
    await web_app.start_demo_testing(user="t")
    assert await wait_for(lambda: web_app.RUNTIME_STATE["active_position"] is not None)
    out = await web_app.stop_demo_testing(user="t")
    assert "stays protected" in out["message"]
    assert (await web_app.get_system_status(user="t"))["engine"]["running"] is False
    assert web_app.RUNTIME_STATE["active_position"] is not None and sim_engine.position is not None


@pytest.mark.asyncio
async def test_kill_switch_closes_the_trade_on_bybit_and_halts(sim_engine):
    await save_keys()
    await web_app.start_demo_testing(user="t")
    assert await wait_for(lambda: web_app.RUNTIME_STATE["active_position"] is not None)
    out = await web_app.trigger_emergency_kill(user="t")
    assert out["status"] == "HALTED" and "closed at market" in out["message"]
    assert sim_engine.position is None and sim_engine.orders == []
    assert web_app.RUNTIME_STATE["active_position"] is None
    assert web_app.RUNTIME_STATE["kill_switch_triggered"] is True
    assert (await web_app.get_system_status(user="t"))["engine"]["running"] is False
    # a closed-by-kill-switch trade is recorded but is not counted toward the 20-trade gate
    assert web_app.RUNTIME_STATE["clean_demo_trade_count"] == 0
    assert len(web_app.STATS_TRACKER.get_recent_trades(10)) == 1


@pytest.mark.asyncio
async def test_restart_while_in_a_trade_carries_on_without_entering_again(sim_engine, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "STATE_DB_PATH", str(tmp_path / "state.db"))
    await save_keys()
    await web_app.start_demo_testing(user="t")
    assert await wait_for(lambda: web_app.RUNTIME_STATE["active_position"] is not None)
    await web_app._stop_engine()
    entry_orders = len(sim_engine.placed)

    # what reached disk is enough to rebuild the trade
    saved = json.loads(await StateStore(db_path=str(tmp_path / "state.db")).get_state("console_state"))
    assert saved["active_position"]["stop_price"] == 100685.0 and saved["engine_state"]["last_15m_eval_ts"] > 0

    # "restart": wipe the in-memory trade, load from disk, start the engine like the lifespan does
    web_app.RUNTIME_STATE["active_position"] = None
    web_app.RUNTIME_STATE["engine_state"] = {}
    await web_app.load_persisted_state()
    assert web_app.RUNTIME_STATE["active_position"] is not None
    await web_app.sync_engine()
    try:
        assert (await web_app.get_system_status(user="t"))["engine"]["running"] is True
        await asyncio.sleep(0.1)
        assert len(sim_engine.placed) == entry_orders       # no second entry
        sim_engine.move_price(web_app.RUNTIME_STATE["active_position"]["tp2_price"] + 1)
        assert await wait_for(lambda: web_app.RUNTIME_STATE["active_position"] is None)
        assert web_app.RUNTIME_STATE["clean_demo_trade_count"] == 1
    finally:
        await web_app._stop_engine()


@pytest.mark.asyncio
async def test_engine_switched_off_never_starts(sim_engine, monkeypatch):
    monkeypatch.setenv("BOT_ENGINE", "off")
    await save_keys()
    out = await web_app.start_demo_testing(user="t")
    assert out["engine"]["running"] is False and sim_engine.placed == []
    assert "switched off" in (await web_app.get_system_status(user="t"))["market_radar"]["current_market_status"]


@pytest.mark.asyncio
async def test_live_start_message_is_honest_about_live_execution():
    web_app.RUNTIME_STATE["demo_run_start_time"] = None
    with pytest.raises(Exception):
        await web_app.start_live_trading(user="t")   # still locked: no 24h / 20 trades
