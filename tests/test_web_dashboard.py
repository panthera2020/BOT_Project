"""
End-to-end integration tests for FastAPI operations console and demo qualification gate.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone, timedelta
from src.web.app import app, RUNTIME_STATE


@pytest.mark.asyncio
async def test_admin_authentication_and_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthorized attempt
        res = await client.get("/api/status")
        assert res.status_code == 401

        # Authorized attempt
        auth = ("admin@tradingbot.local", "P@ssword123!")
        res = await client.get("/api/status", auth=auth)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "RUNNING"
        assert not data["demo_qualification"]["is_live_unlocked"]


@pytest.mark.asyncio
async def test_api_key_vault_and_live_gate_rejection():
    transport = ASGITransport(app=app)
    auth = ("admin@tradingbot.local", "P@ssword123!")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Save Demo Keys
        res = await client.post(
            "/api/keys",
            auth=auth,
            json={"account_type": "demo", "api_key": "demo_key_123", "api_secret": "demo_sec_456"},
        )
        assert res.status_code == 200

        # Attempt to activate Live before requirements met (SHOULD BE REJECTED 403)
        res_live = await client.post("/api/live/enable", auth=auth)
        assert res_live.status_code == 403
        assert "Live Trading Locked" in res_live.json()["detail"]


@pytest.mark.asyncio
async def test_live_gate_unlock_after_24h_and_20_trades():
    transport = ASGITransport(app=app)
    auth = ("admin@tradingbot.local", "P@ssword123!")
    
    # Simulate criteria met: 25 hours elapsed and 21 clean demo trades
    RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=25)
    RUNTIME_STATE["clean_demo_trade_count"] = 21

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_live = await client.post("/api/live/enable", auth=auth)
        assert res_live.status_code == 200
        assert res_live.json()["status"] == "SUCCESS"
        assert RUNTIME_STATE["current_mode"] == "LIVE"


@pytest.mark.asyncio
async def test_emergency_kill_switch():
    transport = ASGITransport(app=app)
    auth = ("admin@tradingbot.local", "P@ssword123!")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/emergency/kill", auth=auth)
        assert res.status_code == 200
        assert res.json()["status"] == "HALTED"
        assert not RUNTIME_STATE["is_bot_running"]
        assert RUNTIME_STATE["kill_switch_triggered"]
