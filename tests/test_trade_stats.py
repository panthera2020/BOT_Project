"""
Unit and integration tests for TradeStatsTracker and new Demo/Live control endpoints.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
from src.core.trade_stats import TradeStatsTracker
from src.web.app import app, RUNTIME_STATE, STATS_TRACKER


def test_trade_stats_tracker_math():
    tracker = TradeStatsTracker()
    assert tracker.get_stats("DEMO")["total_trades"] == 0

    # Record 2 demo wins and 1 loss
    tracker.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.002, 100000.0, 103000.0, 6.0, 3.0)
    tracker.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.002, 100000.0, 105000.0, 10.0, 5.0)
    tracker.record_completed_trade("DEMO", "BTCUSDT", "Buy", 0.002, 100000.0, 99000.0, -2.0, -1.0)

    stats = tracker.get_stats("DEMO")
    assert stats["total_trades"] == 3
    assert stats["wins"] == 2
    assert stats["losses"] == 1
    assert stats["win_rate"] == 66.7
    assert stats["total_pnl_usd"] == 14.0
    assert stats["total_pnl_r"] == 7.0

    # Ensure live mode remains unpolluted
    assert tracker.get_stats("LIVE")["total_trades"] == 0


@pytest.mark.asyncio
async def test_demo_and_live_control_endpoints():
    transport = ASGITransport(app=app)
    auth = ("admin@tradingbot.local", "P@ssword123!")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Start Demo Testing
        res_start_demo = await client.post("/api/demo/start", auth=auth)
        assert res_start_demo.status_code == 200
        assert res_start_demo.json()["status"] == "SUCCESS"
        assert RUNTIME_STATE["demo_testing_active"]
        assert RUNTIME_STATE["current_mode"] == "DEMO"

        # Pause Demo Testing
        res_stop_demo = await client.post("/api/demo/stop", auth=auth)
        assert res_stop_demo.status_code == 200
        assert not RUNTIME_STATE["demo_testing_active"]

        # Attempt to start Live Trading without qualifications (must be blocked 403)
        res_live_blocked = await client.post("/api/live/start", auth=auth)
        assert res_live_blocked.status_code == 403

        # Simulate qualification criteria met + API key provided
        RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=25)
        RUNTIME_STATE["clean_demo_trade_count"] = 21
        RUNTIME_STATE["bybit_live_keys"]["api_key"] = "valid_live_key"

        res_live_ok = await client.post("/api/live/start", auth=auth)
        assert res_live_ok.status_code == 200
        assert RUNTIME_STATE["live_trading_active"]
        assert RUNTIME_STATE["current_mode"] == "LIVE"

        # Pause Live Trading
        res_stop_live = await client.post("/api/live/stop", auth=auth)
        assert res_stop_live.status_code == 200
        assert not RUNTIME_STATE["live_trading_active"]
