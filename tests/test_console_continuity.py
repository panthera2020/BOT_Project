"""
Continuity tests: after a restart the console comes back with the state it had, secrets
are never readable on disk, and live trading cannot restart by itself.

A "restart" here wipes the in-memory state and runs the app's startup hook, exactly what
a fresh process does.
"""
import copy
import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport

import src.storage.secrets_box as sb
import src.web.app as web
from src.storage.state_store import StateStore

# Tests that need the real encryption library are skipped (not failed) without it.
requires_crypto = pytest.mark.skipif(sb.Fernet is None, reason="the 'cryptography' package is not installed")

AUTH = ("admin@tradingbot.local", "P@ssword123!")
PASSPHRASE = "a-long-test-passphrase"
DEMO_KEY, DEMO_SECRET = "AKIA-DEMO-KEY-1234", "demo-secret-value-5678"
LIVE_KEY, LIVE_SECRET = "AKIA-LIVE-KEY-9999", "live-secret-value-0000"
TG_TOKEN = "123456789:telegram-token-xyz"


@pytest.fixture(autouse=True)
def console(tmp_path, monkeypatch):
    saved_state = copy.deepcopy(web.RUNTIME_STATE)
    saved_trades = list(web.STATS_TRACKER.trades)
    saved_next_id = web.STATS_TRACKER._next_id
    saved_info = copy.deepcopy(web.PERSISTENCE_INFO)
    db_path = str(tmp_path / "bot.db")
    monkeypatch.setattr(web, "STATE_DB_PATH", db_path)
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", PASSPHRASE)
    monkeypatch.delenv("AUTO_RESUME_LIVE", raising=False)
    web.STATS_TRACKER.restore_trades([])
    yield db_path
    web.RUNTIME_STATE.clear()
    web.RUNTIME_STATE.update(saved_state)
    web.STATS_TRACKER.trades = saved_trades
    web.STATS_TRACKER._next_id = saved_next_id
    web.PERSISTENCE_INFO.clear()
    web.PERSISTENCE_INFO.update(saved_info)


def _client():
    return AsyncClient(transport=ASGITransport(app=web.app), base_url="http://test")


async def restart():
    """What a fresh process starts with, followed by the app's startup hook."""
    web.RUNTIME_STATE.update(
        is_bot_running=True,
        demo_testing_active=False,
        live_trading_active=False,
        current_mode="DEMO",
        is_live_trading_unlocked=False,
        demo_run_start_time=None,
        clean_demo_trade_count=0,
        bybit_demo_keys={"api_key": "", "api_secret": ""},
        bybit_live_keys={"api_key": "", "api_secret": ""},
        kill_switch_triggered=False,
        active_position=None,
        telegram_config={"bot_token": "", "chat_id": ""},
    )
    web.STATS_TRACKER.restore_trades([])
    web.PERSISTENCE_INFO.update(resumed=[], notes=[], last_saved_utc=None)
    async with web.app.router.lifespan_context(web.app):
        pass


def raw_disk_bytes(db_path):
    data = b""
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            with open(db_path + suffix, "rb") as f:
                data += f.read()
    return data


async def trade(mode="demo", pnl=1.0, clean=True):
    if mode == "demo":
        return await web.record_demo_trade("BTCUSDT", "Buy", 0.002, 100000.0, 100500.0, pnl, pnl, clean=clean)
    return await web.record_live_trade("BTCUSDT", "Buy", 0.002, 100000.0, 100500.0, pnl, pnl)


async def set_secrets(client):
    r1 = await client.post("/api/keys", auth=AUTH, json={"account_type": "demo", "api_key": DEMO_KEY, "api_secret": DEMO_SECRET})
    r2 = await client.post("/api/keys", auth=AUTH, json={"account_type": "live", "api_key": LIVE_KEY, "api_secret": LIVE_SECRET})
    r3 = await client.post("/api/telegram", auth=AUTH, json={"bot_token": TG_TOKEN, "chat_id": "424242"})
    return r1, r2, r3


@pytest.mark.asyncio
@requires_crypto
async def test_everything_comes_back_after_a_restart():
    async with _client() as client:
        await set_secrets(client)
        await client.post("/api/demo/start", auth=AUTH)
        await trade(pnl=2.0)
        await trade(pnl=-1.0)
        await trade(pnl=0.5, clean=False)
        web.RUNTIME_STATE["active_position"] = {"symbol": "BTCUSDT", "side": "Buy", "qty": 0.002}
        await web.save_console_state()
        started = web.RUNTIME_STATE["demo_run_start_time"]
        before = (await client.get("/api/status", auth=AUTH)).json()

        await restart()

        after = (await client.get("/api/status", auth=AUTH)).json()

    assert web.RUNTIME_STATE["demo_run_start_time"] == started
    assert web.RUNTIME_STATE["clean_demo_trade_count"] == 2
    assert web.RUNTIME_STATE["demo_testing_active"] is True
    assert web.RUNTIME_STATE["bybit_demo_keys"] == {"api_key": DEMO_KEY, "api_secret": DEMO_SECRET}
    assert web.RUNTIME_STATE["bybit_live_keys"] == {"api_key": LIVE_KEY, "api_secret": LIVE_SECRET}
    assert web.RUNTIME_STATE["telegram_config"] == {"bot_token": TG_TOKEN, "chat_id": "424242"}
    assert web.RUNTIME_STATE["active_position"] == {"symbol": "BTCUSDT", "side": "Buy", "qty": 0.002}
    assert after["stats"] == before["stats"]
    assert after["recent_trades"] == before["recent_trades"]
    assert after["demo_qualification"]["clean_trades"] == 2
    assert after["keys_configured"] == {"demo": True, "live": True}
    assert after["telegram"]["is_configured"] is True


@pytest.mark.asyncio
async def test_trade_ids_continue_after_a_restart_and_live_trades_survive():
    await trade()
    await trade()
    await trade(mode="live", pnl=3.0)
    await restart()
    new = await trade()
    assert new.id == 4
    assert web.STATS_TRACKER.get_stats("LIVE")["total_trades"] == 1
    assert web.STATS_TRACKER.get_stats("DEMO")["total_trades"] == 3
    # A live trade never counts toward the demo gate.
    assert web.RUNTIME_STATE["clean_demo_trade_count"] == 3


@pytest.mark.asyncio
@requires_crypto
async def test_secrets_are_never_readable_on_disk(console):
    async with _client() as client:
        responses = await set_secrets(client)
        await client.post("/api/demo/start", auth=AUTH)
        await trade()
    assert all(r.json()["saved_to_disk"] is True for r in (responses[0], responses[1], responses[2]))
    raw = raw_disk_bytes(console)
    assert len(raw) > 0
    for secret in (DEMO_KEY, DEMO_SECRET, LIVE_KEY, LIVE_SECRET, TG_TOKEN, PASSPHRASE):
        assert secret.encode() not in raw, f"{secret} is readable on disk"
    # ...but the non-secret chat id is stored as plain state.
    assert b"424242" in raw


@pytest.mark.asyncio
@requires_crypto
async def test_without_an_encryption_key_secrets_stay_off_disk(console, monkeypatch):
    monkeypatch.delenv("STATE_ENCRYPTION_KEY")
    async with _client() as client:
        r1, _, r3 = await set_secrets(client)
        await client.post("/api/demo/start", auth=AUTH)
        status = (await client.get("/api/status", auth=AUTH)).json()["persistence"]
    assert r1.json()["saved_to_disk"] is False and "STATE_ENCRYPTION_KEY" in r1.json()["note"]
    assert r3.json()["saved_to_disk"] is False
    assert status["enabled"] is True and status["secrets_saved_encrypted"] is False
    assert "STATE_ENCRYPTION_KEY" in status["secrets_note"]

    raw = raw_disk_bytes(console)
    for secret in (DEMO_KEY, DEMO_SECRET, LIVE_KEY, LIVE_SECRET, TG_TOKEN):
        assert secret.encode() not in raw

    await restart()
    # Everything else still comes back; the keys must be re-entered.
    assert web.RUNTIME_STATE["demo_testing_active"] is True
    assert web.RUNTIME_STATE["bybit_demo_keys"]["api_key"] == ""
    assert web.RUNTIME_STATE["telegram_config"]["chat_id"] == "424242"
    assert web.RUNTIME_STATE["telegram_config"]["bot_token"] == ""


@pytest.mark.asyncio
@requires_crypto
async def test_old_saved_keys_cannot_come_back_after_keys_change_without_a_key(monkeypatch):
    async with _client() as client:
        await set_secrets(client)  # saved encrypted with the passphrase
        monkeypatch.delenv("STATE_ENCRYPTION_KEY")
        await client.post("/api/keys", auth=AUTH, json={"account_type": "demo", "api_key": "NEW", "api_secret": "NEW2"})
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", PASSPHRASE)
    await restart()
    assert web.RUNTIME_STATE["bybit_demo_keys"]["api_key"] == ""  # the stale copy was dropped


@pytest.mark.asyncio
@requires_crypto
async def test_wrong_key_on_restart_reports_it_and_keeps_the_rest(monkeypatch):
    async with _client() as client:
        await set_secrets(client)
        await client.post("/api/demo/start", auth=AUTH)
    monkeypatch.setenv("STATE_ENCRYPTION_KEY", "a-completely-different-key")
    await restart()
    assert web.RUNTIME_STATE["bybit_demo_keys"]["api_key"] == ""
    assert web.RUNTIME_STATE["demo_testing_active"] is True
    assert any("decrypt" in n for n in web.PERSISTENCE_INFO["notes"])


@pytest.mark.asyncio
@requires_crypto
async def test_missing_key_on_restart_says_the_keys_are_locked(monkeypatch):
    async with _client() as client:
        await set_secrets(client)
    monkeypatch.delenv("STATE_ENCRYPTION_KEY")
    await restart()
    assert web.RUNTIME_STATE["bybit_live_keys"]["api_key"] == ""
    assert any("STATE_ENCRYPTION_KEY" in n for n in web.PERSISTENCE_INFO["notes"])


@pytest.mark.asyncio
async def test_demo_testing_resumes_by_itself_and_says_so():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        await restart()
        status = (await client.get("/api/status", auth=AUTH)).json()
    assert status["demo_testing_active"] is True
    # the tests run with the trading engine switched off, so the radar must say it is NOT scanning
    assert status["market_radar"]["is_actively_scanning"] is False
    assert "switched off" in status["market_radar"]["current_market_status"]
    assert any("Demo testing resumed" in r for r in status["persistence"]["resumed"])


@pytest.mark.asyncio
async def test_a_paused_demo_stays_paused_after_a_restart():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        await client.post("/api/demo/stop", auth=AUTH)
    await restart()
    assert web.RUNTIME_STATE["demo_testing_active"] is False


async def _go_live(client):
    await client.post("/api/demo/start", auth=AUTH)
    await client.post("/api/keys", auth=AUTH, json={"account_type": "live", "api_key": LIVE_KEY, "api_secret": LIVE_SECRET})
    web.RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=25)
    for _ in range(20):
        await trade()
    res = await client.post("/api/live/start", auth=AUTH)
    assert res.status_code == 200 and web.RUNTIME_STATE["live_trading_active"]


@pytest.mark.asyncio
async def test_live_trading_does_not_restart_by_itself_by_default():
    async with _client() as client:
        await _go_live(client)
        await restart()
        status = (await client.get("/api/status", auth=AUTH)).json()
        assert status["live_trading_active"] is False
        assert status["current_mode"] == "DEMO"
        assert status["demo_qualification"]["is_live_unlocked"] is True  # still unlocked, just not running
        assert any("NOT been resumed" in n for n in status["persistence"]["notes"])
        # One deliberate press brings it back.
        assert (await client.post("/api/live/start", auth=AUTH)).status_code == 200

    # Even after that press, the next restart pauses live again: it never restarts unattended.
    await restart()
    assert web.RUNTIME_STATE["live_trading_active"] is False


@pytest.mark.asyncio
@requires_crypto
async def test_live_trading_resumes_only_when_opted_in(monkeypatch):
    monkeypatch.setenv("AUTO_RESUME_LIVE", "1")
    async with _client() as client:
        await _go_live(client)
        await restart()
        status = (await client.get("/api/status", auth=AUTH)).json()
    assert status["live_trading_active"] is True
    assert any("Live trading resumed" in r for r in status["persistence"]["resumed"])


@pytest.mark.asyncio
async def test_opt_in_still_refuses_to_resume_live_without_keys(monkeypatch):
    monkeypatch.setenv("AUTO_RESUME_LIVE", "1")
    monkeypatch.delenv("STATE_ENCRYPTION_KEY")  # live keys cannot be saved, so they are gone after a restart
    async with _client() as client:
        await _go_live(client)
        await restart()
    assert web.RUNTIME_STATE["live_trading_active"] is False


@pytest.mark.asyncio
async def test_kill_switch_stays_engaged_across_a_restart():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        await client.post("/api/emergency/kill", auth=AUTH)
        await restart()
        status = (await client.get("/api/status", auth=AUTH)).json()
    assert status["kill_switch_active"] is True
    assert status["status"] == "STOPPED"
    assert status["demo_testing_active"] is False and status["live_trading_active"] is False


@pytest.mark.asyncio
async def test_damaged_saved_state_does_not_stop_the_app_from_starting(console):
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        await trade()
        store = StateStore(db_path=console)
        await store.set_state("console_state", "{this is not json")
        await restart()
        status = (await client.get("/api/status", auth=AUTH)).json()
    assert status["status"] == "RUNNING"
    assert web.RUNTIME_STATE["demo_testing_active"] is False  # defaults, not garbage
    assert web.STATS_TRACKER.get_stats("DEMO")["total_trades"] == 1  # trades live in their own table
    assert any("damaged" in n for n in status["persistence"]["notes"])


@pytest.mark.asyncio
async def test_unwritable_location_never_breaks_the_console(monkeypatch):
    monkeypatch.setattr(web, "STATE_DB_PATH", "/nonexistent-dir/deeper/bot.db")
    async with _client() as client:
        assert (await client.post("/api/demo/start", auth=AUTH)).status_code == 200
        assert (await client.post("/api/keys", auth=AUTH, json={"account_type": "demo", "api_key": "a", "api_secret": "b"})).status_code == 200
        await trade()
    assert web.RUNTIME_STATE["clean_demo_trade_count"] == 1
    await web.load_persisted_state()  # must not raise
    assert web.PERSISTENCE_INFO["notes"]


@pytest.mark.asyncio
async def test_with_saving_switched_off_everything_still_works_and_says_so(monkeypatch):
    monkeypatch.setattr(web, "STATE_DB_PATH", "")
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        res = await client.post("/api/keys", auth=AUTH, json={"account_type": "demo", "api_key": "a", "api_secret": "b"})
        assert res.json()["saved_to_disk"] is False and "note" not in res.json()
        await trade()
        await web.load_persisted_state()
        status = (await client.get("/api/status", auth=AUTH)).json()["persistence"]
    assert status["enabled"] is False
    assert any("DATABASE_URL" in n for n in status["notes"])
    assert web.STATS_TRACKER.get_stats("DEMO")["total_trades"] == 1  # in-memory state untouched


@pytest.mark.asyncio
@requires_crypto
async def test_status_reports_when_state_was_last_saved():
    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        status = (await client.get("/api/status", auth=AUTH)).json()["persistence"]
    assert status["enabled"] and status["secrets_saved_encrypted"]
    assert status["last_saved_utc"]


@pytest.mark.asyncio
async def test_when_the_cryptography_package_is_missing_secrets_stay_off_disk_and_the_app_works(console, monkeypatch):
    """Runs everywhere: simulates a phone where the optional package could not be installed."""
    web._box_for.cache_clear()
    monkeypatch.setattr(sb, "Fernet", None)
    try:
        async with _client() as client:
            r1, _, r3 = await set_secrets(client)
            await client.post("/api/demo/start", auth=AUTH)
            await trade()
            status = (await client.get("/api/status", auth=AUTH)).json()["persistence"]
        assert r1.status_code == 200 and r1.json()["saved_to_disk"] is False
        assert "cryptography" in r1.json()["note"]
        assert r3.json()["saved_to_disk"] is False
        assert status["secrets_saved_encrypted"] is False and "cryptography" in status["secrets_note"]

        raw = raw_disk_bytes(console)
        for secret in (DEMO_KEY, DEMO_SECRET, LIVE_KEY, LIVE_SECRET, TG_TOKEN):
            assert secret.encode() not in raw

        await restart()
        # Everything that is not a secret still comes back; the keys must be re-entered.
        assert web.RUNTIME_STATE["demo_testing_active"] is True
        assert web.RUNTIME_STATE["clean_demo_trade_count"] == 1
        assert web.STATS_TRACKER.get_stats("DEMO")["total_trades"] == 1
        assert web.RUNTIME_STATE["bybit_demo_keys"]["api_key"] == ""
        assert web.RUNTIME_STATE["telegram_config"]["chat_id"] == "424242"
    finally:
        web._box_for.cache_clear()  # do not leak the "no crypto" box into other tests


@pytest.mark.asyncio
async def test_many_simultaneous_changes_never_leave_an_older_snapshot_on_disk():
    import asyncio

    async with _client() as client:
        await client.post("/api/demo/start", auth=AUTH)
        jobs = [trade(pnl=1.0 + i / 100) for i in range(25)]
        jobs += [client.post("/api/demo/stop", auth=AUTH), client.post("/api/demo/start", auth=AUTH)]
        jobs += [web.save_console_state() for _ in range(10)]
        await asyncio.gather(*jobs)
        await web.save_console_state()  # the last word is whatever memory says now
        in_memory = (web.RUNTIME_STATE["clean_demo_trade_count"], web.RUNTIME_STATE["demo_testing_active"])

    await restart()
    assert (web.RUNTIME_STATE["clean_demo_trade_count"], web.RUNTIME_STATE["demo_testing_active"]) == in_memory
    assert in_memory[0] == 25
    ids = [t.id for t in web.STATS_TRACKER.trades]
    assert len(ids) == 25 and len(set(ids)) == 25
