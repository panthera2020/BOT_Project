"""
Shared test setup.

The web console keeps its state in module-level globals, so a test that marks the demo
run as qualified (25h, 21 trades) would otherwise leak into whichever test runs next.
Every test starts from the same fresh, un-qualified console state.
"""
import pytest


@pytest.fixture(autouse=True)
def engine_off_by_default(monkeypatch):
    """The web tests use fake API keys; the trading engine must not try to reach Bybit."""
    monkeypatch.setenv("BOT_ENGINE", "off")


@pytest.fixture(autouse=True)
def fresh_console_gate_state():
    from src.web.app import RUNTIME_STATE, STATS_TRACKER

    RUNTIME_STATE["demo_run_start_time"] = None
    RUNTIME_STATE["clean_demo_trade_count"] = 0
    RUNTIME_STATE["is_live_trading_unlocked"] = False
    RUNTIME_STATE["demo_testing_active"] = False
    RUNTIME_STATE["live_trading_active"] = False
    RUNTIME_STATE["kill_switch_triggered"] = False
    RUNTIME_STATE["is_bot_running"] = True
    RUNTIME_STATE["current_mode"] = "DEMO"
    RUNTIME_STATE["active_position"] = None
    RUNTIME_STATE["engine_state"] = {}
    RUNTIME_STATE["active_strategy"] = "sweep_reversal"
    RUNTIME_STATE["strategy_progress"] = {}
    STATS_TRACKER.restore_trades([])
    yield
