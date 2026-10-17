"""
Unit tests for what the console saves, what it restores, and what may resume by itself.
"""
import copy
import json
from datetime import datetime, timedelta, timezone

from src.core.console_state import (
    apply_resume_policy,
    has_any_secret,
    restore_plain,
    restore_secrets,
    snapshot_plain,
    snapshot_secrets,
)

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


def fresh_state():
    return {
        "is_bot_running": True,
        "demo_testing_active": False,
        "live_trading_active": False,
        "current_mode": "DEMO",
        "is_live_trading_unlocked": False,
        "demo_run_start_time": None,
        "clean_demo_trade_count": 0,
        "bybit_demo_keys": {"api_key": "", "api_secret": ""},
        "bybit_live_keys": {"api_key": "", "api_secret": ""},
        "kill_switch_triggered": False,
        "active_position": None,
        "telegram_config": {"bot_token": "", "chat_id": ""},
    }


def busy_state():
    s = fresh_state()
    s.update(
        demo_testing_active=True,
        demo_run_start_time=NOW - timedelta(hours=5),
        clean_demo_trade_count=7,
        active_position={"symbol": "BTCUSDT", "side": "Buy", "qty": 0.002},
    )
    s["bybit_demo_keys"] = {"api_key": "DK", "api_secret": "DS"}
    s["bybit_live_keys"] = {"api_key": "LK", "api_secret": "LS"}
    s["telegram_config"] = {"bot_token": "123:ABC", "chat_id": "42"}
    return s


def test_plain_snapshot_never_contains_a_secret():
    text = json.dumps(snapshot_plain(busy_state(), now=NOW))
    for secret in ("DK", "DS", "LK", "LS", "123:ABC"):
        assert secret not in text


def test_secrets_snapshot_holds_all_secrets():
    s = snapshot_secrets(busy_state())
    assert s["bybit_demo_keys"] == {"api_key": "DK", "api_secret": "DS"}
    assert s["bybit_live_keys"] == {"api_key": "LK", "api_secret": "LS"}
    assert s["telegram_bot_token"] == "123:ABC"
    assert has_any_secret(s)
    assert not has_any_secret(snapshot_secrets(fresh_state()))


def test_full_round_trip_through_json():
    original = busy_state()
    plain = json.loads(json.dumps(snapshot_plain(original, now=NOW)))
    secrets = json.loads(json.dumps(snapshot_secrets(original)))

    restored = fresh_state()
    assert restore_plain(restored, plain) == []
    assert restore_secrets(restored, secrets) == []
    assert restored == {**original, "is_live_trading_unlocked": False}


def test_damaged_field_is_skipped_and_the_rest_still_restores():
    plain = snapshot_plain(busy_state(), now=NOW)
    plain["clean_demo_trade_count"] = "seven"
    plain["demo_run_start_time"] = "not a time"
    plain["flags"]["demo_testing_active"] = "yes"
    plain["flags"]["current_mode"] = "MOON"
    restored = fresh_state()
    problems = restore_plain(restored, plain)
    assert len(problems) == 4
    assert restored["clean_demo_trade_count"] == 0
    assert restored["demo_run_start_time"] is None
    assert restored["demo_testing_active"] is False
    assert restored["current_mode"] == "DEMO"
    assert restored["active_position"] == plain["active_position"]  # untouched fields still restore
    assert restored["telegram_config"]["chat_id"] == "42"


def test_negative_or_boolean_count_is_rejected():
    for bad in (-1, True, 2.5, None):
        restored = fresh_state()
        restore_plain(restored, {"clean_demo_trade_count": bad})
        assert restored["clean_demo_trade_count"] == 0


def test_garbage_snapshots_do_not_crash():
    state = fresh_state()
    assert restore_plain(state, "oops")
    assert restore_plain(state, None)
    assert restore_secrets(state, ["x"])
    assert state == fresh_state()


def test_naive_saved_time_is_read_as_utc():
    restored = fresh_state()
    restore_plain(restored, {"demo_run_start_time": "2026-10-18T10:00:00"})
    assert restored["demo_run_start_time"].tzinfo is not None


def test_bad_secrets_are_skipped():
    restored = fresh_state()
    problems = restore_secrets(restored, {"bybit_demo_keys": {"api_key": 5}, "telegram_bot_token": 9})
    assert problems
    assert restored["bybit_demo_keys"] == {"api_key": "", "api_secret": ""}
    assert restored["telegram_config"]["bot_token"] == ""


# --- resume policy -------------------------------------------------------------

def test_demo_testing_resumes_by_itself():
    s = busy_state()
    resumed, notes = apply_resume_policy(s, gate_unlocked=False, auto_resume_live=False)
    assert s["demo_testing_active"] is True
    assert any("Demo testing resumed" in r for r in resumed)


def test_live_does_not_resume_by_default_even_when_unlocked():
    s = busy_state()
    s.update(live_trading_active=True, current_mode="LIVE")
    resumed, notes = apply_resume_policy(s, gate_unlocked=True, auto_resume_live=False)
    assert s["live_trading_active"] is False
    assert s["current_mode"] == "DEMO"
    assert any("NOT been resumed" in n for n in notes)


def test_live_resumes_only_with_opt_in_unlocked_gate_and_keys():
    def run(unlocked, auto, live_key):
        s = busy_state()
        s.update(live_trading_active=True, current_mode="LIVE")
        s["bybit_live_keys"]["api_key"] = live_key
        apply_resume_policy(s, gate_unlocked=unlocked, auto_resume_live=auto)
        return s["live_trading_active"]

    assert run(True, True, "LK") is True
    assert run(False, True, "LK") is False   # gate no longer unlocked
    assert run(True, False, "LK") is False   # no opt-in
    assert run(True, True, "") is False      # no live keys


def test_kill_switch_keeps_everything_stopped():
    s = busy_state()
    s.update(kill_switch_triggered=True, live_trading_active=True, is_bot_running=True)
    resumed, notes = apply_resume_policy(s, gate_unlocked=True, auto_resume_live=True)
    assert not s["demo_testing_active"] and not s["live_trading_active"] and not s["is_bot_running"]
    assert resumed == []
    assert any("kill switch" in n for n in notes)


def test_open_position_is_flagged_for_checking_against_bybit():
    s = busy_state()
    _, notes = apply_resume_policy(s, gate_unlocked=False, auto_resume_live=False)
    assert any("Bybit" in n for n in notes)
    s2 = fresh_state()
    _, notes2 = apply_resume_policy(s2, gate_unlocked=False, auto_resume_live=False)
    assert notes2 == []


def test_snapshot_does_not_alias_the_live_state():
    s = busy_state()
    secrets = snapshot_secrets(s)
    secrets["bybit_demo_keys"]["api_key"] = "changed"
    assert s["bybit_demo_keys"]["api_key"] == "DK"
    before = copy.deepcopy(s)
    snapshot_plain(s, now=NOW)
    assert s == before
