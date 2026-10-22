"""
Live trading for every strategy (same engine, real account, 2 trades a day, its own unlock) and
the plug-and-play promise: strategies can be added, removed or disabled at any time without
breaking the console, and nothing saved for them is lost.
"""
import asyncio
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.core.daily_cap import get_trading_day_id
from src.core.strategy_progress import get_progress
from src.engine import strategies as strat_pkg
from src.engine.demo_engine import DemoEngine
from src.engine.strategies import REGISTRY, default_strategy_id, discover, reload_strategies, strategy_catalog
from src.web import app as web_app
from tests.sim_exchange import Clock, SimExchange, T0
from tests.test_demo_engine import Harness
from tests.test_engine_wiring import wait_for

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
STATE = web_app.RUNTIME_STATE


# ====================================================================== live engine (unit)
@pytest.mark.asyncio
async def test_live_engine_trades_like_demo_but_on_the_live_account_and_says_so():
    h = Harness("Buy", mode="LIVE", balance=300.0)
    bodies = []

    async def notify(title, body, level):
        bodies.append((title, body))
    h.engine.notify = notify
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["mode"] == "LIVE" and h.engine.mode == "LIVE"
    assert rec["effective_r"] == 3.5                       # risk comes from the $300 account, not a demo balance
    assert bodies and all(b.startswith("[LIVE] ") for _, b in bodies)
    assert h.state["engine_state"]["live_cap"]["count"] == 1


@pytest.mark.asyncio
async def test_demo_engine_is_unchanged_and_has_no_daily_cap():
    h = Harness("Buy")
    h.state["engine_state"].update({"live_cap": {"day": get_trading_day_id(h.clock.now()), "count": 99}})
    await h.engine.tick()
    assert h.state["active_position"] and h.state["active_position"]["mode"] == "DEMO"
    assert h.state["engine_state"]["live_cap"]["count"] == 99     # demo never touches the live counter


@pytest.mark.asyncio
async def test_live_daily_cap_stops_the_third_trade_of_the_day():
    h = Harness("Buy", mode="LIVE")
    h.state["engine_state"].update({"live_cap": {"day": get_trading_day_id(h.clock.now()), "count": 2}})
    await h.engine.tick()
    assert h.state["active_position"] is None and h.sim.placed == []
    assert "daily cap reached (2/2" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_live_daily_cap_is_per_trading_day_and_configurable():
    h = Harness("Buy", mode="LIVE")
    yesterday = get_trading_day_id(h.clock.now() - timedelta(days=1))
    h.state["engine_state"].update({"live_cap": {"day": yesterday, "count": 2}})
    await h.engine.tick()
    assert h.state["active_position"] and h.state["engine_state"]["live_cap"] == {"day": get_trading_day_id(h.clock.now()), "count": 1}
    one = Harness("Buy", mode="LIVE", max_daily_trades=1)
    one.state["engine_state"].update({"live_cap": {"day": get_trading_day_id(one.clock.now()), "count": 1}})
    await one.engine.tick()
    assert one.state["active_position"] is None


@pytest.mark.asyncio
async def test_a_rejected_live_order_does_not_use_up_a_trade():
    h = Harness("Buy", mode="LIVE")
    h.sim.reject_market = True
    await h.engine.tick()
    assert h.state["active_position"] is None and "live_cap" not in h.state["engine_state"]


@pytest.mark.asyncio
async def test_live_never_sizes_from_the_demo_accounts_starting_capital():
    h = Harness("Buy", mode="LIVE", balance=300.0)
    h.state["engine_state"].update({"day": get_trading_day_id(h.clock.now()), "day_start_capital": 50000.0, "capital_mode": "DEMO"})
    await h.engine.tick()
    assert h.state["engine_state"]["day_start_capital"] == 300.0 and h.state["engine_state"]["capital_mode"] == "LIVE"
    assert h.state["active_position"]["effective_r"] == 3.5


@pytest.mark.asyncio
async def test_old_saved_state_without_a_capital_mode_counts_as_demo():
    h = Harness("Buy")
    h.state["engine_state"].update({"day": get_trading_day_id(h.clock.now()), "day_start_capital": 1234.0})
    await h.engine.tick()
    assert h.state["engine_state"]["day_start_capital"] == 1234.0          # kept: it was a demo run


@pytest.mark.asyncio
async def test_live_foreign_position_blocks_trading():
    h = Harness("Buy", mode="LIVE")
    h.sim.position = {"side": "Buy", "size": 0.01, "avg": 100000.0, "stop": None}
    await h.engine.tick()
    assert h.state["active_position"] is None and "did not open" in h.engine.status["text"]
    assert h.sim.placed == []


def test_unknown_engine_mode_is_refused():
    with pytest.raises(ValueError):
        Harness("Buy", mode="PAPER")


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_id", ["sweep_reversal", "sweep_reversal_loose"])
async def test_live_works_for_the_sweep_strategies_and_records_the_strategy(strategy_id):
    h = Harness("Buy", mode="LIVE", strategy_id=strategy_id)
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec["mode"] == "LIVE" and rec["strategy"] == strategy_id


# ====================================================================== live in the console
@pytest.fixture
def world(monkeypatch):
    """Console with the engine on, and a separate simulated Bybit for the demo and the live account."""
    monkeypatch.setenv("BOT_ENGINE", "on")
    clock = Clock()
    sims = {"DEMO": SimExchange(clock=clock), "LIVE": SimExchange(balance=300.0, clock=clock)}

    async def quick(_seconds):
        await asyncio.sleep(0.005)

    def make():
        mode = web_app._engine_mode()
        return DemoEngine(
            sims[mode], STATE, web_app.save_console_state,
            web_app.record_live_trade if mode == "LIVE" else web_app.record_demo_trade, None,
            now_fn=clock.now, sleep=quick, poll_seconds=0.005, position_poll_seconds=0.005,
            strategy_id=web_app._active_strategy(), mode=mode)

    monkeypatch.setattr(web_app, "_new_engine", make)
    STATE["bybit_demo_keys"] = {"api_key": "K", "api_secret": "S"}
    STATE["bybit_live_keys"] = {"api_key": "LK", "api_secret": "LS"}
    yield sims
    # leave nothing running for the next test
    asyncio.get_event_loop_policy()


def unlock(strategy="sweep_reversal"):
    if strategy == "sweep_reversal":
        STATE["demo_run_start_time"], STATE["clean_demo_trade_count"] = NOW - timedelta(days=3), 25
    else:
        STATE["strategy_progress"][strategy] = {"start": NOW - timedelta(days=3), "clean": 25}


async def stop_everything():
    await web_app.stop_live_trading(user="t")
    await web_app.stop_demo_testing(user="t")


@pytest.mark.asyncio
async def test_live_trade_runs_end_to_end_on_the_live_account_and_is_recorded_as_live(world):
    unlock()
    try:
        out = await web_app.start_live_trading(user="t")
        assert out["status"] == "SUCCESS" and "LIVE trading started" in out["message"]
        assert STATE["live_trading_active"] and not STATE["demo_testing_active"] and STATE["current_mode"] == "LIVE"
        assert await wait_for(lambda: STATE["active_position"] is not None)
        assert STATE["active_position"]["mode"] == "LIVE"
        assert world["LIVE"].position and world["DEMO"].position is None and world["DEMO"].placed == []
        assert web_app._engine_summary()["mode"] == "LIVE" and web_app._build_status()["engine_mode"] == "LIVE"
        rec = STATE["active_position"]
        world["LIVE"].move_price(rec["tp2_price"] + 1)                       # both take-profits fill on Bybit
        assert await wait_for(lambda: STATE["active_position"] is None)
        live = web_app.STATS_TRACKER.get_stats("LIVE", "sweep_reversal")
        assert live["total_trades"] == 1 and web_app.STATS_TRACKER.get_stats("DEMO")["total_trades"] == 0
        assert STATE["clean_demo_trade_count"] == 25                         # live trades never touch the demo gate
        assert web_app.STATS_TRACKER.get_recent_trades(1)[0]["mode"] == "LIVE"
    finally:
        await stop_everything()


@pytest.mark.asyncio
async def test_live_uses_the_live_keys_and_the_real_host():
    STATE["live_trading_active"] = True
    STATE["bybit_live_keys"] = {"api_key": "LK", "api_secret": "LS"}
    STATE["bybit_demo_keys"] = {"api_key": "DK", "api_secret": "DS"}
    live = web_app._new_engine()
    assert live.mode == "LIVE" and live.exchange.api_key == "LK" and live.exchange.is_demo is False
    assert live.exchange.base_url == "https://api.bybit.com"
    STATE["live_trading_active"] = False
    demo = web_app._new_engine()
    assert demo.mode == "DEMO" and demo.exchange.api_key == "DK" and demo.exchange.base_url == "https://api-demo.bybit.com"


@pytest.mark.asyncio
async def test_live_start_without_live_keys_says_so_and_does_not_trade(world):
    unlock()
    STATE["bybit_live_keys"] = {"api_key": "", "api_secret": ""}
    out = await web_app.start_live_trading(user="t")
    try:
        assert "No Bybit LIVE API keys" in out["message"] and out["engine"]["running"] is False
        assert "no Bybit LIVE API keys" in web_app._build_status()["market_radar"]["current_market_status"]
        assert world["LIVE"].placed == []
    finally:
        await stop_everything()


@pytest.mark.asyncio
async def test_live_is_still_locked_without_the_strategys_own_demo_history(world):
    with pytest.raises(HTTPException) as err:
        await web_app.start_live_trading(user="t")
    assert err.value.status_code == 403 and STATE["live_trading_active"] is False and world["LIVE"].placed == []


@pytest.mark.asyncio
async def test_live_runs_for_any_strategy_once_that_strategy_is_unlocked(world):
    unlock("sweep_reversal_loose")
    await web_app.select_strategy(web_app.StrategyPayload(strategy_id="sweep_reversal_loose"), user="t")
    try:
        await web_app.start_live_trading(user="t")
        assert await wait_for(lambda: STATE["active_position"] is not None)
        assert STATE["active_position"]["strategy"] == "sweep_reversal_loose" and STATE["active_position"]["mode"] == "LIVE"
    finally:
        await stop_everything()


@pytest.mark.asyncio
async def test_the_engine_will_not_run_live_if_the_unlock_is_lost(world):
    unlock()
    STATE["live_trading_active"] = True
    assert web_app._engine_wanted() is True
    STATE["demo_run_start_time"] = None                                      # e.g. damaged state
    assert web_app._engine_wanted() is False
    assert "locked" in web_app._engine_idle_reason()


@pytest.mark.asyncio
async def test_demo_and_live_never_run_together(world):
    unlock()
    await web_app.start_demo_testing(user="t")
    try:
        assert await wait_for(lambda: STATE["active_position"] is not None)
        with pytest.raises(HTTPException) as err:                            # a demo trade is open
            await web_app.start_live_trading(user="t")
        assert err.value.status_code == 409 and not STATE["live_trading_active"]
    finally:
        await stop_everything()
    STATE["active_position"], world["DEMO"].position = None, None
    STATE["live_trading_active"] = True
    with pytest.raises(HTTPException) as err:
        await web_app.start_demo_testing(user="t")
    assert err.value.status_code == 409
    STATE["live_trading_active"] = False
    STATE["active_position"] = {"mode": "LIVE"}
    with pytest.raises(HTTPException) as err:
        await web_app.start_demo_testing(user="t")
    assert err.value.status_code == 409 and "live trade is still open" in err.value.detail


@pytest.mark.asyncio
async def test_starting_live_pauses_demo_and_forgets_half_built_setups(world):
    unlock()
    STATE["demo_testing_active"] = True
    STATE["engine_state"] = {"setup": {"x": 1}, "strategies": {"trend_pullback": {"setup": {"y": 2}, "keep": 1}}}
    world["LIVE"].markets = {"15": [], "60": [], "1": []}                     # nothing to trade; we only look at the state
    out = await web_app.start_live_trading(user="t")
    try:
        assert "Demo testing was paused" in out["message"] and STATE["demo_testing_active"] is False
        assert STATE["engine_state"]["setup"] is None
        assert STATE["engine_state"]["strategies"]["trend_pullback"] == {"setup": None, "keep": 1}
    finally:
        await stop_everything()


@pytest.mark.asyncio
async def test_pausing_live_leaves_the_open_trade_protected_and_the_engine_stopped(world):
    unlock()
    await web_app.start_live_trading(user="t")
    assert await wait_for(lambda: STATE["active_position"] is not None)
    out = await web_app.stop_live_trading(user="t")
    assert "protected by its stop" in out["message"] and STATE["live_trading_active"] is False
    assert web_app._engine_summary()["running"] is False and world["LIVE"].position is not None
    assert "paused" in web_app._build_status()["market_radar"]["current_market_status"].lower()
    with pytest.raises(HTTPException):                                       # strategy can't change under an open trade
        await web_app.select_strategy(web_app.StrategyPayload(strategy_id="trend_pullback"), user="t")
    world["LIVE"].position = None
    STATE["active_position"] = None


@pytest.mark.asyncio
async def test_kill_switch_closes_a_live_trade_on_the_live_account(world):
    unlock()
    await web_app.start_live_trading(user="t")
    assert await wait_for(lambda: STATE["active_position"] is not None)
    out = await web_app.trigger_emergency_kill(user="t")
    assert out["status"] == "HALTED" and "closed at market" in out["message"]
    assert world["LIVE"].position is None and STATE["active_position"] is None
    assert STATE["live_trading_active"] is False and STATE["kill_switch_triggered"] is True
    assert web_app.STATS_TRACKER.get_stats("LIVE")["total_trades"] == 1 and world["DEMO"].placed == []
    assert STATE["clean_demo_trade_count"] == 25


@pytest.mark.asyncio
async def test_live_does_not_resume_by_itself_after_a_restart(world, tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "STATE_DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(web_app, "PERSISTENCE_INFO", {"resumed": [], "notes": [], "last_saved_utc": None})
    monkeypatch.delenv("AUTO_RESUME_LIVE", raising=False)
    unlock()
    STATE["live_trading_active"] = True
    STATE["current_mode"] = "LIVE"
    await web_app.save_console_state()
    STATE["live_trading_active"], STATE["current_mode"] = False, "DEMO"
    await web_app.load_persisted_state()
    assert STATE["live_trading_active"] is False and web_app._engine_wanted() is False
    assert any("has NOT been resumed" in n for n in web_app.PERSISTENCE_INFO["notes"])


@pytest.mark.asyncio
async def test_record_live_trade_ignores_clean_and_keeps_the_demo_count(world):
    await web_app.record_live_trade("BTCUSDT", "Buy", 0.1, 100.0, 101.0, 5.0, 1.0, True, "trend_pullback")
    assert STATE["clean_demo_trade_count"] == 0 and get_progress(STATE, "trend_pullback")[1] == 0
    assert web_app.STATS_TRACKER.get_stats("LIVE", "trend_pullback")["total_trades"] == 1


# ====================================================================== plug and play
@pytest.fixture
def plugin_file():
    """Drop strategy files into the strategies folder for a test, and always take them out."""
    folder = os.path.dirname(strat_pkg.__file__)
    made = []

    def write(name, source):
        path = os.path.join(folder, f"{name}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(source))
        made.append((name, path))

    yield write
    for name, path in made:
        os.remove(path)
        sys.modules.pop(f"src.engine.strategies.{name}", None)
    reload_strategies()


GOOD = '''
    from src.engine.strategies.base import Strategy

    class {cls}(Strategy):
        id = "{sid}"
        name = "{name}"
        summary = "test strategy"
        rules = ["a", "b", "c"]
        order = {order}

        async def scan(self, now, equity, c15):
            return None
'''


def test_built_in_strategies_are_found_in_order():
    assert [c.id for c in discover()] == ["sweep_reversal", "sweep_reversal_loose", "trend_pullback", "breakout_retest", "session_breakout", "range_reversion"]
    assert default_strategy_id() == "sweep_reversal"


def test_dropping_a_file_in_adds_a_strategy_and_deleting_it_removes_it(plugin_file):
    plugin_file("zz_plugin_a", GOOD.format(cls="PluginA", sid="plugin_a", name="Plugin A", order=5))
    reload_strategies()
    assert "plugin_a" in REGISTRY and [c["id"] for c in strategy_catalog()][0] == "plugin_a"    # order 5 sorts first
    assert next(c for c in strategy_catalog() if c["id"] == "plugin_a")["name"] == "Plugin A"
    os.remove(os.path.join(os.path.dirname(strat_pkg.__file__), "zz_plugin_a.py"))
    sys.modules.pop("src.engine.strategies.zz_plugin_a", None)
    reload_strategies()
    assert "plugin_a" not in REGISTRY and len(REGISTRY) == 6
    open(os.path.join(os.path.dirname(strat_pkg.__file__), "zz_plugin_a.py"), "w").write(GOOD.format(cls="PluginA", sid="plugin_a", name="Plugin A", order=5))  # for the fixture's cleanup


def test_a_broken_strategy_file_is_skipped_and_the_rest_still_load(plugin_file):
    plugin_file("zz_broken", "this is not python (((")
    plugin_file("zz_import_error", "import module_that_does_not_exist\n")
    plugin_file("zz_good", GOOD.format(cls="Good", sid="plugin_good", name="Good", order=90))
    reload_strategies()
    assert "plugin_good" in REGISTRY and len(REGISTRY) == 7 and "sweep_reversal" in REGISTRY


def test_duplicate_ids_keep_the_first_and_do_not_crash(plugin_file):
    plugin_file("zz_dup_1", GOOD.format(cls="One", sid="plugin_dup", name="First", order=90))
    plugin_file("zz_dup_2", GOOD.format(cls="Two", sid="plugin_dup", name="Second", order=90))
    reload_strategies()
    assert REGISTRY["plugin_dup"].name == "First" and len(REGISTRY) == 7


def test_a_file_without_an_id_or_with_only_the_base_class_is_ignored(plugin_file):
    plugin_file("zz_noid", GOOD.format(cls="NoId", sid="", name="No id", order=1))
    reload_strategies()
    assert len(REGISTRY) == 6


def test_strategies_can_be_disabled_without_deleting_them(monkeypatch):
    monkeypatch.setenv("DISABLED_STRATEGIES", " trend_pullback , range_reversion ,")
    assert [c.id for c in discover()] == ["sweep_reversal", "sweep_reversal_loose", "breakout_retest", "session_breakout"]
    assert len(discover(disabled=[])) == 6                                   # an explicit list overrides the environment
    reload_strategies()
    try:
        assert "trend_pullback" not in REGISTRY and len(strategy_catalog()) == 4
    finally:
        monkeypatch.delenv("DISABLED_STRATEGIES")
        reload_strategies()
    assert len(REGISTRY) == 6


@pytest.fixture
def without(monkeypatch):
    """Take strategies out of the registry the way deleting their files would."""
    def remove(*ids):
        for sid in ids:
            monkeypatch.delitem(REGISTRY, sid)
    return remove


@pytest.mark.asyncio
async def test_removing_the_selected_strategy_falls_back_and_keeps_its_results(without):
    await web_app.record_demo_trade("BTCUSDT", "Buy", 0.1, 100.0, 101.0, 12.0, 1.2, True, "trend_pullback")
    STATE["strategy_progress"]["trend_pullback"]["start"] = NOW
    STATE["active_strategy"] = "trend_pullback"
    without("trend_pullback")
    web_app.PERSISTENCE_INFO["notes"] = []
    await web_app.ensure_active_strategy()
    assert STATE["active_strategy"] == "sweep_reversal"
    assert any("not installed any more" in n and "trend_pullback" in n for n in web_app.PERSISTENCE_INFO["notes"])
    overview = {s["id"]: s for s in web_app._build_status()["strategies"]}
    assert overview["trend_pullback"]["installed"] is False and overview["trend_pullback"]["demo"]["total_trades"] == 1
    assert overview["trend_pullback"]["gate"]["clean_trades"] == 1 and overview["trend_pullback"]["active"] is False
    assert "trend_pullback" not in [s["id"] for s in strategy_catalog()]
    assert get_progress(STATE, "trend_pullback") == (NOW, 1)                 # progress still saved


@pytest.mark.asyncio
async def test_a_removed_strategy_cannot_be_selected_but_the_others_still_can(without):
    without("range_reversion")
    with pytest.raises(HTTPException) as err:
        await web_app.select_strategy(web_app.StrategyPayload(strategy_id="range_reversion"), user="t")
    assert err.value.status_code == 400
    out = await web_app.select_strategy(web_app.StrategyPayload(strategy_id="breakout_retest"), user="t")
    assert out["active"] == "breakout_retest"


@pytest.mark.asyncio
async def test_removing_a_strategy_pauses_live_trading_that_was_unlocked_by_it(without):
    unlock("trend_pullback")
    STATE["active_strategy"], STATE["live_trading_active"], STATE["current_mode"] = "trend_pullback", True, "LIVE"
    without("trend_pullback")
    await web_app.ensure_active_strategy()
    assert STATE["live_trading_active"] is False and STATE["current_mode"] == "DEMO"
    assert any("Live trading was paused" in n for n in web_app.PERSISTENCE_INFO["notes"])


@pytest.mark.asyncio
async def test_putting_a_removed_strategy_back_restores_its_numbers(monkeypatch):
    await web_app.record_demo_trade("BTCUSDT", "Buy", 0.1, 100.0, 101.0, 12.0, 1.2, True, "range_reversion")
    cls = REGISTRY["range_reversion"]
    monkeypatch.delitem(REGISTRY, "range_reversion")
    gone = {s["id"]: s for s in web_app._build_status()["strategies"]}["range_reversion"]
    assert gone["installed"] is False
    monkeypatch.setitem(REGISTRY, "range_reversion", cls)
    back = {s["id"]: s for s in web_app._build_status()["strategies"]}["range_reversion"]
    assert back["installed"] is True and back["demo"] == gone["demo"] and back["gate"] == gone["gate"]


@pytest.mark.asyncio
async def test_a_trade_open_from_a_removed_strategy_is_still_managed_by_the_shared_exits(without, monkeypatch):
    monkeypatch.setenv("BOT_ENGINE", "on")
    STATE["bybit_demo_keys"] = {"api_key": "K", "api_secret": "S"}
    clock = Clock()
    sim = SimExchange(clock=clock)
    h = Harness("Buy", strategy_id="trend_pullback")        # any trade will do; we only need an open record
    h.sim, h.engine.exchange = sim, sim
    sim.position = {"side": "Buy", "size": 0.01, "avg": 102300.0, "stop": 101000.0}
    h.state["active_position"] = {
        "mode": "DEMO", "symbol": "BTCUSDT", "side": "Buy", "qty": 0.01, "entry_price": 102300.0, "stop_price": 101000.0,
        "initial_stop_price": 101000.0, "tp1_price": 106200.0, "tp2_price": 108800.0, "tp1_lots": 0.007, "tp2_lots": 0.003,
        "is_single_exit": False, "be_price": 102310.0, "breakeven_active": False, "early_armed": False, "highest_r": 0.0,
        "opened_ms": 0, "opened_utc": "x", "effective_r": 10.0, "net_rr": 2.5, "leverage": 50, "trend": "", "unclean": False,
        "close_reason": None, "finalize_attempts": 0, "strategy": "long_gone_strategy"}
    await h.engine.tick()
    assert h.state["active_position"] is not None and "In a trade" in h.engine.status["text"]
    sim.move_price(101000.0)                                  # its stop on Bybit is hit
    for _ in range(3):
        await h.engine.tick()
    assert h.state["active_position"] is None and h.trades[-1]["strategy"] == "long_gone_strategy"
    # and the trade is listed under the removed strategy's name in the results
    web_app.STATS_TRACKER.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.01, 1.0, 2.0, -5.0, -0.5, strategy="long_gone_strategy")
    assert {s["id"]: s for s in web_app._build_status()["strategies"]}["long_gone_strategy"]["installed"] is False


@pytest.mark.asyncio
async def test_with_no_strategy_installed_the_console_still_answers_and_nothing_trades(monkeypatch):
    monkeypatch.setenv("BOT_ENGINE", "on")
    for sid in list(REGISTRY):
        monkeypatch.delitem(REGISTRY, sid)
    assert default_strategy_id() is None and strategy_catalog() == []
    await web_app.ensure_active_strategy()
    assert any("No strategy is installed" in n for n in web_app.PERSISTENCE_INFO["notes"])
    status = web_app._build_status()
    assert status["strategies"] == [] or all(s["installed"] is False for s in status["strategies"])
    STATE["demo_testing_active"] = True
    STATE["bybit_demo_keys"] = {"api_key": "K", "api_secret": "S"}
    assert web_app._engine_wanted() is False and "No strategy is installed" in web_app._engine_idle_reason()
    unlock()
    with pytest.raises(HTTPException) as err:
        await web_app.start_live_trading(user="t")
    assert err.value.status_code == 409
