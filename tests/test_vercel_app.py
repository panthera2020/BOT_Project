"""The Vercel remote dashboard: sign-in, status view, offline handling, command passing."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.fake_supabase import FakeSupabase
from vercel_app import server

AUTH = ("boss@example.com", "a-long-password-1")


@pytest.fixture
def remote(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setenv("ADMIN_USER", AUTH[0])
    monkeypatch.setenv("ADMIN_PASS", AUTH[1])
    monkeypatch.setattr(server, "_client", lambda: fake.api())
    monkeypatch.setattr(server, "COMMAND_POLL_SECONDS", 0.01)
    monkeypatch.setattr(server, "COMMAND_WAIT_SECONDS", 0.3)
    return fake, TestClient(server.app)


def phone_reports(fake, age_seconds=1, payload=None):
    seen = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    fake.tables["bot_status"] = [{"id": 1, "payload": payload or {"status": "RUNNING"}, "updated_at": seen}]


def test_health_and_page_need_no_login(remote):
    _, c = remote
    assert c.get("/health").json()["role"] == "remote-dashboard"
    page = c.get("/")
    assert page.status_code == 200 and "Mission Control" in page.text


def test_page_is_the_same_file_the_phone_serves(remote):
    _, c = remote
    from src.web import app as phone
    import os
    with open(os.path.join(os.path.dirname(phone.__file__), "templates", "index.html"), encoding="utf-8") as f:
        assert c.get("/").text == f.read()


def test_status_requires_login(remote):
    _, c = remote
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", auth=("boss@example.com", "wrong-password-x")).status_code == 401


def test_login_endpoint(remote):
    _, c = remote
    assert c.post("/api/login", json={"username": AUTH[0], "password": AUTH[1]}).status_code == 200
    assert c.post("/api/login", json={"username": AUTH[0], "password": "nope-nope-nope"}).status_code == 401


@pytest.mark.parametrize("user,pw", [("", "long-enough-pass"), ("u", "short"), ("u", "P@ssword123!")])
def test_weak_or_missing_credentials_lock_everything(monkeypatch, user, pw):
    monkeypatch.setenv("ADMIN_USER", user)
    monkeypatch.setenv("ADMIN_PASS", pw)
    c = TestClient(server.app)
    assert c.get("/api/status", auth=(user or "u", pw)).status_code == 503
    assert c.post("/api/login", json={"username": user or "u", "password": pw}).status_code == 503
    assert c.post("/api/emergency/kill", auth=(user or "u", pw)).status_code == 503


def test_unset_password_locks_everything(monkeypatch):
    monkeypatch.delenv("ADMIN_PASS", raising=False)
    monkeypatch.setenv("ADMIN_USER", "u")
    assert TestClient(server.app).get("/api/status", auth=("u", "")).status_code == 503


def test_status_online(remote):
    fake, c = remote
    phone_reports(fake, 5, {"status": "RUNNING", "demo_testing_active": True})
    data = c.get("/api/status", auth=AUTH).json()
    assert data["demo_testing_active"] is True
    assert data["remote_view"]["online"] is True and data["remote_view"]["age_seconds"] >= 5


def test_status_offline_when_stale(remote):
    fake, c = remote
    phone_reports(fake, 600)
    view = c.get("/api/status", auth=AUTH).json()["remote_view"]
    assert view["online"] is False and view["last_seen"]


def test_status_when_phone_never_reported(remote):
    _, c = remote
    data = c.get("/api/status", auth=AUTH).json()
    assert data["remote_view"] == {"online": False, "last_seen": None, "age_seconds": None}


def test_status_when_supabase_is_down(remote):
    fake, c = remote
    fake.fail = True
    assert c.get("/api/status", auth=AUTH).status_code == 502


def test_missing_supabase_config_is_explained(monkeypatch):
    monkeypatch.setenv("ADMIN_USER", AUTH[0])
    monkeypatch.setenv("ADMIN_PASS", AUTH[1])
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    r = TestClient(server.app).get("/api/status", auth=AUTH)
    assert r.status_code == 503 and "SUPABASE_URL" in r.json()["detail"]


def test_command_when_phone_online_waits_for_the_result(remote):
    fake, c = remote
    phone_reports(fake)

    import threading, time

    def run():
        for _ in range(100):
            time.sleep(0.01)
            if fake.tables["bot_commands"]:
                fake.tables["bot_commands"][0].update(status="done", result="Demo Testing Started!")
                return
    t = threading.Thread(target=run)
    t.start()
    r = c.post("/api/demo/start", auth=AUTH)
    t.join()
    assert r.status_code == 200 and r.json() == {"status": "SUCCESS", "message": "Demo Testing Started!"}
    assert fake.tables["bot_commands"][0]["command"] == "start_demo"


def test_command_when_phone_offline_is_queued_with_a_clear_note(remote):
    fake, c = remote
    phone_reports(fake, 900)
    r = c.post("/api/emergency/kill", auth=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "QUEUED"
    assert "offline" in r.json()["message"].lower() and "Bybit" in r.json()["message"]
    assert [x["command"] for x in fake.tables["bot_commands"]] == ["kill"]


def test_command_online_but_unconfirmed_says_so(remote):
    fake, c = remote
    phone_reports(fake)
    r = c.post("/api/demo/stop", auth=AUTH)
    assert r.json()["status"] == "QUEUED" and "not confirmed" in r.json()["message"]


def test_command_the_phone_rejected_is_reported(remote):
    fake, c = remote
    phone_reports(fake)
    import threading, time

    def run():
        for _ in range(100):
            time.sleep(0.01)
            if fake.tables["bot_commands"]:
                fake.tables["bot_commands"][0].update(status="error", result="boom")
                return
    t = threading.Thread(target=run)
    t.start()
    r = c.post("/api/demo/stop", auth=AUTH)
    t.join()
    assert r.status_code == 409 and "boom" in r.json()["detail"]


def test_commands_require_login(remote):
    fake, c = remote
    for path in ("/api/demo/start", "/api/demo/stop", "/api/emergency/kill"):
        assert c.post(path).status_code == 401
    assert fake.tables["bot_commands"] == []


@pytest.mark.parametrize("path", ["/api/keys", "/api/telegram", "/api/telegram/test", "/api/live/start", "/api/live/stop", "/api/live/enable", "/api/strategy"])
def test_secrets_and_live_controls_stay_on_the_phone(remote, path):
    fake, c = remote
    r = c.post(path, auth=AUTH, json={"account_type": "demo", "api_key": "K", "api_secret": "S"})
    assert r.status_code == 403 and "phone" in r.json()["detail"]
    assert fake.requests == []  # nothing was sent to Supabase


def test_strategy_rules_are_served_without_asking_the_phone(remote):
    fake, c = remote
    data = c.get("/api/strategies", auth=AUTH).json()
    assert len(data["strategies"]) == 6 and fake.requests == []
    assert c.get("/api/strategies").status_code == 401
