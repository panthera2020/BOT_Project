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
    # Reset runtime state to fresh un-qualified baseline
    RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc)
    RUNTIME_STATE["clean_demo_trade_count"] = 0
    RUNTIME_STATE["is_bot_running"] = True
    RUNTIME_STATE["kill_switch_triggered"] = False
    
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
    # Reset qualification state
    RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc)
    RUNTIME_STATE["clean_demo_trade_count"] = 0

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


@pytest.mark.asyncio
async def test_telegram_config_and_test_ping():
    transport = ASGITransport(app=app)
    auth = ("admin@tradingbot.local", "P@ssword123!")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Save credentials
        res = await client.post(
            "/api/telegram",
            auth=auth,
            json={"bot_token": "mock_bot_token_123", "chat_id": "999888777"},
        )
        assert res.status_code == 200
        assert RUNTIME_STATE["telegram_config"]["bot_token"] == "mock_bot_token_123"
        assert RUNTIME_STATE["telegram_config"]["chat_id"] == "999888777"

        # Trigger test ping
        res_test = await client.post("/api/telegram/test", auth=auth)
        assert res_test.status_code == 200
        assert "Test ping sent" in res_test.json()["message"]


@pytest.mark.asyncio
async def test_login_endpoint_accepts_valid_and_rejects_invalid_credentials():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post(
            "/api/login",
            json={"username": "admin@tradingbot.local", "password": "P@ssword123!"},
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "OK"

        bad_password = await client.post(
            "/api/login",
            json={"username": "admin@tradingbot.local", "password": "wrong"},
        )
        assert bad_password.status_code == 401

        bad_user = await client.post(
            "/api/login",
            json={"username": "someone@else.local", "password": "P@ssword123!"},
        )
        assert bad_user.status_code == 401


@pytest.mark.asyncio
async def test_unauthorized_responses_never_trigger_browser_login_popup():
    """A WWW-Authenticate challenge makes browsers show a native popup that cloud
    proxies often suppress; the page uses its own sign-in form instead."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/status")
        assert res.status_code == 401
        assert "www-authenticate" not in res.headers

        bad = await client.post(
            "/api/login",
            json={"username": "admin@tradingbot.local", "password": "wrong"},
        )
        assert bad.status_code == 401
        assert "www-authenticate" not in bad.headers


@pytest.mark.asyncio
async def test_dashboard_page_contains_sign_in_form():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert 'id="loginOverlay"' in res.text
        assert "authFetch" in res.text
        assert "/api/login" in res.text


@pytest.mark.asyncio
async def test_dashboard_page_has_saved_state_card_and_safe_rendering():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        for marker in ('id="persistCard"', 'id="persistState"', 'id="persistResumed"', "renderPersistence", "data.persistence"):
            assert marker in res.text, marker
        # Server-supplied notes must be inserted as text, never as HTML.
        assert "li.textContent = text" in res.text
