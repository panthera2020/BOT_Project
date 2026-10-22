"""Choosing a strategy in the console: each strategy keeps its own results, clock and live gate."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.core.strategy_progress import add_clean_trade, get_progress, start_clock_if_needed
from src.core.trade_stats import TradeStatsTracker
from src.storage.state_store import StateStore
from src.web import app as web_app
from tests.test_engine_wiring import save_keys, sim_engine, wait_for  # noqa: F401  (fixture re-used)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "STATE_DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(web_app, "PERSISTENCE_INFO", {"resumed": [], "notes": [], "last_saved_utc": None})
    return str(tmp_path / "bot.db")


def trade(strategy, pnl=10.0, r=1.0):
    return web_app.record_demo_trade("BTCUSDT", "Buy", 0.1, 100.0, 101.0, pnl, r, True, strategy)


# ---------------------------------------------------------------- progress helpers
def test_default_strategy_progress_uses_the_legacy_fields():
    state = {"demo_run_start_time": None, "clean_demo_trade_count": 0}
    start_clock_if_needed(state, "sweep_reversal", NOW)
    add_clean_trade(state, "sweep_reversal")
    assert state["demo_run_start_time"] == NOW and state["clean_demo_trade_count"] == 1
    assert get_progress(state, "sweep_reversal") == (NOW, 1)
    assert "strategy_progress" not in state or state["strategy_progress"] == {}


def test_other_strategies_keep_separate_progress_and_the_clock_never_resets():
    state = {"demo_run_start_time": None, "clean_demo_trade_count": 0}
    start_clock_if_needed(state, "trend_pullback", NOW)
    start_clock_if_needed(state, "trend_pullback", NOW + timedelta(hours=9))   # a second press changes nothing
    add_clean_trade(state, "trend_pullback")
    add_clean_trade(state, "trend_pullback")
    assert get_progress(state, "trend_pullback") == (NOW, 2)
    assert get_progress(state, "range_reversion") == (None, 0)
    assert get_progress(state, "sweep_reversal") == (None, 0)


# ---------------------------------------------------------------- stats per strategy
def make_tracker():
    t = TradeStatsTracker()
    for strat, pnl, r in [("a", 30, 3), ("a", -10, -1), ("a", -10, -1), ("b", 20, 2), ("b", 20, 2)]:
        t.record_completed_trade("DEMO", "BTCUSDT", "Buy", 1, 1, 1, pnl, r, strategy=strat)
    t.record_completed_trade("LIVE", "BTCUSDT", "Buy", 1, 1, 1, 5, 0.5, strategy="a")
    return t


def test_stats_are_filtered_by_strategy_and_mode():
    t = make_tracker()
    a, b = t.get_stats("DEMO", "a"), t.get_stats("DEMO", "b")
    assert (a["total_trades"], a["wins"], a["losses"], a["win_rate"]) == (3, 1, 2, 33.3)
    assert (b["total_trades"], b["win_rate"], b["total_pnl_usd"]) == (2, 100.0, 40.0)
    assert t.get_stats("DEMO")["total_trades"] == 5                      # no filter: everything
    assert t.get_stats("LIVE", "a")["total_trades"] == 1 and t.get_stats("LIVE", "b")["total_trades"] == 0


def test_expectancy_profit_factor_and_drawdown():
    t = make_tracker()
    a = t.get_stats("DEMO", "a")
    assert a["expectancy_r"] == pytest.approx(0.33, abs=0.01)             # (3 - 1 - 1) / 3
    assert a["profit_factor"] == 1.5                                      # 30 won / 20 lost
    assert a["max_drawdown_r"] == 2.0                                     # +3 peak, then -1 -1
    b = t.get_stats("DEMO", "b")
    assert b["profit_factor"] is None and b["max_drawdown_r"] == 0.0      # no loss yet
    assert t.get_stats("DEMO", "none")["total_trades"] == 0


def test_old_saved_trades_without_a_strategy_read_as_the_original():
    t = TradeStatsTracker()
    t.restore_trades([{"id": 1, "mode": "DEMO", "symbol": "BTCUSDT", "side": "Buy", "qty": 1.0, "entry_price": 1.0,
                       "exit_price": 2.0, "pnl_usd": 1.0, "pnl_r": 1.0, "is_win": True, "status": "CLOSED", "timestamp": "x"}])
    assert t.get_stats("DEMO", "sweep_reversal")["total_trades"] == 1
    assert t.get_recent_trades(1)[0]["strategy"] == "sweep_reversal"


# ---------------------------------------------------------------- the console
@pytest.mark.asyncio
async def test_catalog_endpoint_lists_all_six_with_rules():
    out = await web_app.list_strategies(user="t")
    assert out["active"] == "sweep_reversal" and len(out["strategies"]) == 6
    assert all(s["rules"] for s in out["strategies"])


@pytest.mark.asyncio
async def test_status_shows_each_strategy_with_its_own_numbers_and_gate():
    await trade("trend_pullback", 20.0, 2.0)
    await trade("trend_pullback", -10.0, -1.0)
    await trade("sweep_reversal", 5.0, 0.5)
    web_app.RUNTIME_STATE["strategy_progress"]["trend_pullback"]["start"] = datetime.now(timezone.utc) - timedelta(hours=30)
    st = web_app._build_status()
    by_id = {s["id"]: s for s in st["strategies"]}
    assert list(by_id) == ["sweep_reversal", "sweep_reversal_loose", "trend_pullback", "breakout_retest", "session_breakout", "range_reversion"]
    assert by_id["trend_pullback"]["demo"]["total_trades"] == 2 and by_id["sweep_reversal"]["demo"]["total_trades"] == 1
    assert by_id["trend_pullback"]["gate"]["clean_trades"] == 2 and by_id["trend_pullback"]["gate"]["has_24h_run"]
    assert by_id["trend_pullback"]["gate"]["is_live_unlocked"] is False                # still needs 20 trades
    assert by_id["range_reversion"]["demo"]["total_trades"] == 0 and by_id["range_reversion"]["gate"]["elapsed_hours"] == 0
    assert by_id["sweep_reversal"]["active"] and not by_id["trend_pullback"]["active"]
    assert st["stats"]["demo"]["total_trades"] == 1                                    # the big numbers = the selected strategy
    assert st["demo_qualification"]["clean_trades"] == 1


@pytest.mark.asyncio
async def test_switching_changes_the_headline_numbers_and_gate_but_keeps_every_result():
    await trade("sweep_reversal")
    await trade("breakout_retest")
    await trade("breakout_retest")
    out = await web_app.select_strategy(web_app.StrategyPayload(strategy_id="breakout_retest"), user="t")
    assert out["active"] == "breakout_retest"
    st = web_app._build_status()
    assert st["active_strategy"] == "breakout_retest" and st["stats"]["demo"]["total_trades"] == 2
    assert st["demo_qualification"]["clean_trades"] == 2
    back = await web_app.select_strategy(web_app.StrategyPayload(strategy_id="sweep_reversal"), user="t")
    assert web_app._build_status()["stats"]["demo"]["total_trades"] == 1 and back["active"] == "sweep_reversal"


@pytest.mark.asyncio
async def test_unknown_strategy_is_a_400_and_nothing_changes():
    with pytest.raises(HTTPException) as err:
        await web_app.select_strategy(web_app.StrategyPayload(strategy_id="nope"), user="t")
    assert err.value.status_code == 400 and web_app.RUNTIME_STATE["active_strategy"] == "sweep_reversal"


@pytest.mark.asyncio
async def test_cannot_switch_while_a_trade_is_open_or_live_is_running():
    web_app.RUNTIME_STATE["active_position"] = {"symbol": "BTCUSDT"}
    with pytest.raises(HTTPException) as err:
        await web_app.select_strategy(web_app.StrategyPayload(strategy_id="trend_pullback"), user="t")
    assert err.value.status_code == 409
    web_app.RUNTIME_STATE["active_position"] = None
    web_app.RUNTIME_STATE["live_trading_active"] = True
    with pytest.raises(HTTPException) as err:
        await web_app.select_strategy(web_app.StrategyPayload(strategy_id="trend_pullback"), user="t")
    assert err.value.status_code == 409 and web_app.RUNTIME_STATE["active_strategy"] == "sweep_reversal"


@pytest.mark.asyncio
async def test_each_strategys_clock_starts_when_demo_is_first_started_with_it():
    await web_app.start_demo_testing(user="t")
    assert web_app.RUNTIME_STATE["demo_run_start_time"] is not None
    assert get_progress(web_app.RUNTIME_STATE, "trend_pullback")[0] is None
    await web_app.select_strategy(web_app.StrategyPayload(strategy_id="trend_pullback"), user="t")
    assert get_progress(web_app.RUNTIME_STATE, "trend_pullback")[0] is None            # switching alone does not start it
    await web_app.start_demo_testing(user="t")
    first = get_progress(web_app.RUNTIME_STATE, "trend_pullback")[0]
    assert first is not None
    await web_app.stop_demo_testing(user="t")
    await web_app.start_demo_testing(user="t")
    assert get_progress(web_app.RUNTIME_STATE, "trend_pullback")[0] == first           # pausing never resets it


@pytest.mark.asyncio
async def test_live_unlock_is_per_strategy():
    state = web_app.RUNTIME_STATE
    state["strategy_progress"]["trend_pullback"] = {"start": NOW - timedelta(days=3), "clean": 25}
    with pytest.raises(HTTPException) as err:                                          # the original has no demo history
        await web_app.start_live_trading(user="t")
    assert err.value.status_code == 403 and "Liquidity sweep reversal" in err.value.detail
    await web_app.select_strategy(web_app.StrategyPayload(strategy_id="trend_pullback"), user="t")
    out = await web_app.start_live_trading(user="t")
    assert out["status"] == "SUCCESS" and state["live_trading_active"]
    assert web_app._build_status()["demo_qualification"]["is_live_unlocked"] is True


@pytest.mark.asyncio
async def test_a_trade_counts_only_toward_its_own_strategys_gate():
    await trade("range_reversion")
    assert get_progress(web_app.RUNTIME_STATE, "range_reversion")[1] == 1
    assert web_app.RUNTIME_STATE["clean_demo_trade_count"] == 0
    await web_app.record_demo_trade("BTCUSDT", "Buy", 0.1, 100.0, 99.0, -5.0, -1.0, False, "range_reversion")   # not clean
    assert get_progress(web_app.RUNTIME_STATE, "range_reversion")[1] == 1


# ---------------------------------------------------------------- engine follows the selection
@pytest.mark.asyncio
async def test_engine_runs_the_selected_strategy_and_restarts_on_a_switch(sim_engine, monkeypatch):
    from src.engine.demo_engine import DemoEngine
    from tests.sim_exchange import Clock, SWEEP_END_MS
    from tests.strategy_markets import flat_candles, rows
    # a quiet 15m market: the original strategy stands down, so no trade is open when we switch
    sim_engine.markets["15"] = rows(flat_candles(SWEEP_END_MS, 60), SWEEP_END_MS, 102000.0)
    clock = Clock()

    async def quick(_seconds):
        await asyncio.sleep(0.005)

    monkeypatch.setattr(web_app, "_new_engine", lambda: DemoEngine(
        sim_engine, web_app.RUNTIME_STATE, web_app.save_console_state, web_app.record_demo_trade, None,
        now_fn=clock.now, sleep=quick, poll_seconds=0.005, position_poll_seconds=0.005,
        strategy_id=web_app.RUNTIME_STATE["active_strategy"]))
    await save_keys()
    await web_app.start_demo_testing(user="t")
    try:
        assert await wait_for(lambda: web_app._ENGINE is not None and web_app._ENGINE.strategy_id == "sweep_reversal")
        first = web_app._ENGINE
        await web_app.select_strategy(web_app.StrategyPayload(strategy_id="range_reversion"), user="t")
        assert await wait_for(lambda: web_app._ENGINE is not None and web_app._ENGINE.strategy_id == "range_reversion")
        assert web_app._ENGINE is not first and web_app._engine_summary()["strategy"] == "range_reversion"
        assert web_app._engine_summary()["running"] is True
    finally:
        await web_app.stop_demo_testing(user="t")


# ---------------------------------------------------------------- saving
@pytest.mark.asyncio
async def test_selection_progress_and_trade_strategy_survive_a_restart(db):
    web_app.RUNTIME_STATE["active_strategy"] = "session_breakout"
    web_app.RUNTIME_STATE["strategy_progress"] = {"session_breakout": {"start": NOW, "clean": 4}}
    web_app.RUNTIME_STATE["demo_testing_active"] = False
    await trade("session_breakout", 12.0, 1.2)
    await web_app.save_console_state()

    web_app.RUNTIME_STATE["active_strategy"] = "sweep_reversal"
    web_app.RUNTIME_STATE["strategy_progress"] = {}
    web_app.STATS_TRACKER.restore_trades([])
    await web_app.load_persisted_state()
    assert web_app.RUNTIME_STATE["active_strategy"] == "session_breakout"
    assert get_progress(web_app.RUNTIME_STATE, "session_breakout") == (NOW, 5)         # 4 saved + the trade above
    assert web_app.STATS_TRACKER.get_stats("DEMO", "session_breakout")["total_trades"] == 1
    assert web_app.STATS_TRACKER.get_stats("DEMO", "sweep_reversal")["total_trades"] == 0


@pytest.mark.asyncio
async def test_a_state_file_from_before_the_selector_loads_with_the_original_strategy(db):
    store = StateStore(db_path=db)
    await store.init_db()
    await store.set_state("console_state", json.dumps({
        "version": 1, "flags": {"is_bot_running": True, "demo_testing_active": False, "live_trading_active": False,
                                "kill_switch_triggered": False, "current_mode": "DEMO"},
        "demo_run_start_time": NOW.isoformat(), "clean_demo_trade_count": 7, "active_position": None,
        "engine_state": {}, "telegram_chat_id": ""}))
    await web_app.load_persisted_state()
    assert web_app.RUNTIME_STATE["active_strategy"] == "sweep_reversal"
    assert get_progress(web_app.RUNTIME_STATE, "sweep_reversal") == (NOW, 7)


@pytest.mark.asyncio
async def test_a_database_made_before_the_strategy_column_is_upgraded_in_place(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE trade_history (id INTEGER PRIMARY KEY, mode TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, "
                "qty REAL NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL, pnl_usd REAL NOT NULL, pnl_r REAL NOT NULL, "
                "is_win INTEGER NOT NULL, status TEXT NOT NULL, timestamp TEXT NOT NULL)")
    con.execute("INSERT INTO trade_history VALUES (1,'DEMO','BTCUSDT','Buy',0.1,100,101,10,1,1,'CLOSED','2026-10-01 10:00:00')")
    con.commit()
    con.close()
    store = StateStore(db_path=path)
    await store.init_db()
    await store.init_db()                                    # running it twice is harmless
    rows = await store.load_trades()
    assert rows[0]["strategy"] == "sweep_reversal"
    await store.save_trade({**rows[0], "id": 2, "strategy": "trend_pullback"})
    assert [r["strategy"] for r in await store.load_trades()] == ["sweep_reversal", "trend_pullback"]


def test_damaged_selection_is_skipped_and_reported():
    from src.core.console_state import restore_plain
    state = {"telegram_config": {"chat_id": ""}}
    problems = restore_plain(state, {"active_strategy": 5, "strategy_progress": {"trend_pullback": {"clean": -2}, "zzz": "garbage"}})
    assert len(problems) == 3 and "active_strategy" not in state
    assert state["strategy_progress"] == {}


def test_progress_of_a_strategy_that_is_not_installed_is_kept():
    from src.core.console_state import restore_plain, snapshot_plain
    state = {"telegram_config": {"chat_id": ""}}
    data = {"active_strategy": "gone", "strategy_progress": {"gone": {"start": NOW.isoformat(), "clean": 9}}}
    assert restore_plain(state, data) == []
    assert state["active_strategy"] == "gone" and state["strategy_progress"]["gone"] == {"start": NOW, "clean": 9}
