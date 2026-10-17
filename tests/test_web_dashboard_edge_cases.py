"""
Edge-case and comprehensive UI endpoint tests.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from src.web.app import app, RUNTIME_STATE


@pytest.mark.asyncio
async def test_dashboard_html_rendering():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/")
        assert res.status_code == 200
        assert "Trading Bot Mission Control" in res.text
        assert "Demo Qualification Gate" in res.text
        assert "Bybit Demo API Key Vault" in res.text


@pytest.mark.asyncio
async def test_api_keys_edge_cases():
    transport = ASGITransport(app=app)
    auth = ("admin@tradingbot.local", "P@ssword123!")
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid account type (neither demo nor live)
        res = await client.post(
            "/api/keys",
            auth=auth,
            json={"account_type": "arbitrary_unknown", "api_key": "k", "api_secret": "s"},
        )
        assert res.status_code == 400
        assert "Invalid account_type" in res.json()["detail"]

        # Valid live key insertion
        res_live = await client.post(
            "/api/keys",
            auth=auth,
            json={"account_type": "live", "api_key": "live_key_999", "api_secret": "live_sec_888"},
        )
        assert res_live.status_code == 200
        assert RUNTIME_STATE["bybit_live_keys"]["api_key"] == "live_key_999"
