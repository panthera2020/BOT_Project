"""Supabase link on the phone: client behaviour, mirror, restore, commands, and the loop in the console."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.remote.cloud_sync import CloudSync
from src.remote.supabase import SupabaseClient, SupabaseError, config_from_env
from src.storage.state_store import StateStore
from src.web import app as web_app
from tests.fake_supabase import FakeSupabase


def trade(i, pnl=10.0):
    return {"id": i, "mode": "DEMO", "symbol": "BTCUSDT", "side": "Buy", "qty": 0.1, "entry_price": 100.0,
            "exit_price": 101.0, "pnl_usd": pnl, "pnl_r": 1.0, "is_win": pnl > 0, "status": "CLOSED",
            "timestamp": "2026-10-20 10:00:00"}


# ---------------------------------------------------------------- client
def test_config_needs_both_values(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    assert config_from_env() is None
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co/")
    assert config_from_env() == {"url": "https://x.supabase.co", "key": "k"}


def test_url_must_be_https():
    with pytest.raises(ValueError):
        SupabaseClient("http://example.com", "k")


@pytest.mark.asyncio
async def test_new_style_key_goes_on_apikey_header_only():
    fake = FakeSupabase("sb_secret_abc")
    await fake.api().select("bot_status")
    req = fake.requests[0]
    assert req.headers["apikey"] == "sb_secret_abc"
    assert "authorization" not in req.headers


@pytest.mark.asyncio
async def test_legacy_jwt_key_also_sent_as_bearer():
    fake = FakeSupabase("eyJhbGciOi.payload.sig")
    await fake.api().select("bot_status")
    assert fake.requests[0].headers["authorization"] == "Bearer eyJhbGciOi.payload.sig"


@pytest.mark.asyncio
async def test_errors_become_supabase_error():
    fake = FakeSupabase()
    fake.fail = True
    with pytest.raises(SupabaseError):
        await fake.api().select("bot_status")
    fake.fail, fake.offline = False, True
    with pytest.raises(SupabaseError):
        await fake.api().select("bot_status")


@pytest.mark.asyncio
async def test_wrong_key_is_rejected():
    fake = FakeSupabase("right")
    bad = SupabaseClient("https://fake.supabase.co", "wrong", client=fake.client)
    with pytest.raises(SupabaseError):
        await bad.select("bot_status")


# ---------------------------------------------------------------- status + mirror
@pytest.mark.asyncio
async def test_push_status_overwrites_single_row():
    fake = FakeSupabase()
    sync = CloudSync(fake.api())
    assert await sync.push_status({"status": "RUNNING"})
    assert await sync.push_status({"status": "STOPPED"})
    rows = fake.tables["bot_status"]
    assert len(rows) == 1 and rows[0]["payload"] == {"status": "STOPPED"}
    assert sync.last_push_utc and sync.last_error is None


@pytest.mark.asyncio
async def test_failure_is_recorded_not_raised_and_clears_on_recovery():
    fake = FakeSupabase()
    sync = CloudSync(fake.api())
    fake.fail = True
    assert await sync.push_status({}) is False
    assert "500" in sync.last_error
    fake.fail = False
    assert await sync.push_status({}) is True
    assert sync.last_error is None


@pytest.mark.asyncio
async def test_mirror_uploads_only_changes():
    fake = FakeSupabase()
    sync = CloudSync(fake.api())
    state = {"console_state": '{"a":1}'}
    assert await sync.mirror(state, [trade(1)])
    n = len(fake.requests)
    assert await sync.mirror(state, [trade(1)])
    assert len(fake.requests) == n  # nothing changed, nothing sent
    assert await sync.mirror({"console_state": '{"a":2}'}, [trade(1), trade(2)])
    assert fake.tables["state_backup"][0]["value"] == '{"a":2}'
    assert [r["id"] for r in fake.tables["trade_backup"]] == [1, 2]


@pytest.mark.asyncio
async def test_failed_mirror_is_retried_next_time():
    fake = FakeSupabase()
    sync = CloudSync(fake.api())
    fake.fail = True
    assert await sync.mirror({"console_state": "x"}, []) is False
    fake.fail = False
    assert await sync.mirror({"console_state": "x"}, []) is True
    assert fake.tables["state_backup"][0]["value"] == "x"


@pytest.mark.asyncio
async def test_secrets_not_mirrored_by_default():
    fake = FakeSupabase()
    await CloudSync(fake.api()).mirror({"console_state": "s", "console_secrets": "ENC"}, [])
    assert [r["key"] for r in fake.tables["state_backup"]] == ["console_state"]
    assert "ENC" not in json.dumps(fake.tables)


@pytest.mark.asyncio
async def test_secrets_mirrored_only_when_enabled():
    fake = FakeSupabase()
    await CloudSync(fake.api(), sync_secrets=True).mirror({"console_state": "s", "console_secrets": "ENC"}, [])
    assert {r["key"] for r in fake.tables["state_backup"]} == {"console_state", "console_secrets"}


@pytest.mark.asyncio
async def test_fetch_backup_roundtrip_and_empty_project():
    fake = FakeSupabase()
    sync = CloudSync(fake.api())
    assert await sync.fetch_backup() is None
    await sync.mirror({"console_state": "S"}, [trade(2), trade(1)])
    backup = await CloudSync(fake.api()).fetch_backup()
    assert backup["state"] == {"console_state": "S"}
    assert [t["id"] for t in backup["trades"]] == [1, 2]


@pytest.mark.asyncio
async def test_fetch_backup_ignores_secrets_when_not_enabled():
    fake = FakeSupabase()
    await CloudSync(fake.api(), sync_secrets=True).mirror({"console_state": "S", "console_secrets": "E"}, [])
    backup = await CloudSync(fake.api(), sync_secrets=False).fetch_backup()
    assert "console_secrets" not in backup["state"]


# ---------------------------------------------------------------- commands
def handlers(log):
    async def mk(name):
        log.append(name)
        return {"message": f"{name} ok"}
    return {n: (lambda n=n: mk(n)) for n in ("start_demo", "stop_demo", "kill")}


@pytest.mark.asyncio
async def test_commands_run_in_order_and_are_marked_done():
    fake = FakeSupabase()
    a, b = fake.add_command("start_demo"), fake.add_command("kill")
    log = []
    assert await CloudSync(fake.api()).run_pending_commands(handlers(log)) == 2
    assert log == ["start_demo", "kill"]
    assert fake.command(a)["status"] == "done" and fake.command(a)["result"] == "start_demo ok"
    assert fake.command(b)["handled_at"]


@pytest.mark.asyncio
async def test_a_handled_command_is_not_run_twice():
    fake = FakeSupabase()
    fake.add_command("stop_demo")
    log = []
    sync = CloudSync(fake.api())
    await sync.run_pending_commands(handlers(log))
    await sync.run_pending_commands(handlers(log))
    assert log == ["stop_demo"]


@pytest.mark.asyncio
async def test_old_start_expires_but_old_stop_and_kill_still_run():
    fake = FakeSupabase()
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    s, p, k = fake.add_command("start_demo", old), fake.add_command("stop_demo", old), fake.add_command("kill", old)
    log = []
    await CloudSync(fake.api()).run_pending_commands(handlers(log))
    assert log == ["stop_demo", "kill"]
    assert fake.command(s)["status"] == "expired"
    assert fake.command(p)["status"] == fake.command(k)["status"] == "done"


@pytest.mark.asyncio
async def test_handler_failure_is_reported_and_does_not_block_the_next():
    fake = FakeSupabase()
    a, b = fake.add_command("start_demo"), fake.add_command("stop_demo")

    async def boom():
        raise RuntimeError("nope")

    async def ok():
        return {"message": "fine"}

    await CloudSync(fake.api()).run_pending_commands({"start_demo": boom, "stop_demo": ok})
    assert fake.command(a)["status"] == "error" and "nope" in fake.command(a)["result"]
    assert fake.command(b)["status"] == "done"


@pytest.mark.asyncio
async def test_unknown_command_is_an_error_not_a_crash():
    fake = FakeSupabase()
    cid = fake.add_command("rm -rf")
    await CloudSync(fake.api()).run_pending_commands(handlers([]))
    assert fake.command(cid)["status"] == "error"


@pytest.mark.asyncio
async def test_unreachable_supabase_runs_nothing():
    fake = FakeSupabase()
    fake.add_command("kill")
    fake.offline = True
    log = []
    sync = CloudSync(fake.api())
    assert await sync.run_pending_commands(handlers(log)) == 0
    assert log == [] and sync.last_error


@pytest.mark.asyncio
async def test_prune_removes_old_finished_commands_only():
    fake = FakeSupabase()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    gone = fake.add_command("kill", old, status="done")
    old_pending = fake.add_command("kill", old, status="pending")
    fresh = fake.add_command("kill", status="done")
    await CloudSync(fake.api()).prune()
    ids = {r["id"] for r in fake.tables["bot_commands"]}
    assert ids == {old_pending, fresh} and gone not in ids


# ---------------------------------------------------------------- inside the console
@pytest.fixture
def console(tmp_path, monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(web_app, "STATE_DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setattr(web_app, "_make_cloud", lambda: CloudSync(fake.api()))
    monkeypatch.setattr(web_app, "CLOUD_COMMAND_SECONDS", 0.01)
    monkeypatch.setattr(web_app, "CLOUD_STATUS_SECONDS", 0.01)
    monkeypatch.setattr(web_app, "CLOUD_MIRROR_SECONDS", 0.01)
    monkeypatch.setattr(web_app, "PERSISTENCE_INFO", {"resumed": [], "notes": [], "last_saved_utc": None})
    return fake


async def until(pred, seconds=3.0):
    for _ in range(int(seconds / 0.01)):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_status_without_supabase_says_not_configured():
    assert (web_app._build_status())["cloud"] == {"configured": False}


@pytest.mark.asyncio
async def test_loop_pushes_status_runs_commands_and_mirrors(console):
    fake = console
    await web_app.start_cloud()
    try:
        cid = fake.add_command("start_demo")
        assert await until(lambda: fake.command(cid)["status"] == "done")
        assert web_app.RUNTIME_STATE["demo_testing_active"] is True
        assert await until(lambda: fake.tables["bot_status"] and
                           fake.tables["bot_status"][0]["payload"].get("demo_testing_active") is True)
        assert await until(lambda: any(r["key"] == "console_state" for r in fake.tables["state_backup"]))
        assert web_app._build_status()["cloud"]["configured"] is True
    finally:
        await web_app.stop_cloud()


@pytest.mark.asyncio
async def test_kill_command_halts_the_bot(console):
    fake = console
    web_app.RUNTIME_STATE["demo_testing_active"] = True
    await web_app.start_cloud()
    try:
        cid = fake.add_command("kill")
        assert await until(lambda: fake.command(cid)["status"] == "done")
        assert web_app.RUNTIME_STATE["kill_switch_triggered"] is True
        assert web_app.RUNTIME_STATE["demo_testing_active"] is False
    finally:
        await web_app.stop_cloud()


@pytest.mark.asyncio
async def test_console_keeps_working_when_supabase_is_down(console):
    fake = console
    fake.offline = True
    await web_app.start_cloud()
    try:
        out = await web_app.start_demo_testing(user="t")
        assert out["status"] == "SUCCESS"
        assert await until(lambda: web_app._build_status()["cloud"]["last_error"])
    finally:
        await web_app.stop_cloud()


@pytest.mark.asyncio
async def test_trade_reaches_the_backup_and_a_wiped_phone_recovers(console, tmp_path):
    fake = console
    web_app.RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc) - timedelta(hours=5)
    await web_app.record_demo_trade("BTCUSDT", "Buy", 0.1, 100.0, 101.0, 10.0, 1.0)
    await web_app.record_demo_trade("BTCUSDT", "Sell", 0.1, 100.0, 99.0, 10.0, 1.0)
    await web_app.start_cloud()
    try:
        assert await until(lambda: len(fake.tables["trade_backup"]) == 2)
    finally:
        await web_app.stop_cloud()

    # a brand new phone: empty storage, same Supabase
    web_app.RUNTIME_STATE["clean_demo_trade_count"] = 0
    web_app.RUNTIME_STATE["demo_run_start_time"] = None
    web_app.STATS_TRACKER.restore_trades([])
    web_app.STATE_DB_PATH = str(tmp_path / "new_phone.db")
    await web_app.load_persisted_state()
    assert web_app.RUNTIME_STATE["clean_demo_trade_count"] == 2
    assert web_app.RUNTIME_STATE["demo_run_start_time"] is not None
    assert len(web_app.STATS_TRACKER.get_recent_trades(10)) == 2
    assert any("Supabase backup" in n for n in web_app.PERSISTENCE_INFO["notes"])


@pytest.mark.asyncio
async def test_local_state_wins_over_the_cloud_copy(console, tmp_path):
    fake = console
    await CloudSync(fake.api()).mirror({"console_state": json.dumps({"version": 1, "clean_demo_trade_count": 99})}, [])
    web_app.RUNTIME_STATE["clean_demo_trade_count"] = 3
    await web_app.save_console_state()          # local file now has state
    web_app.RUNTIME_STATE["clean_demo_trade_count"] = 0
    await web_app.load_persisted_state()
    assert web_app.RUNTIME_STATE["clean_demo_trade_count"] == 3


@pytest.mark.asyncio
async def test_empty_supabase_and_empty_phone_just_starts_fresh(console):
    await web_app.load_persisted_state()
    assert web_app.RUNTIME_STATE["clean_demo_trade_count"] == 0
    assert not any("Supabase" in n for n in web_app.PERSISTENCE_INFO["notes"])
