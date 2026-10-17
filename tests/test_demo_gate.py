"""
Unit tests for the demo qualification gate (24h runtime and >= 20 clean trades).
"""
from datetime import datetime, timedelta, timezone

from src.core.demo_gate import evaluate_demo_gate

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_never_started_is_locked_with_zero_clock():
    status = evaluate_demo_gate(None, 50, now=NOW)
    assert status.elapsed_hours == 0
    assert not status.has_24h_run
    assert not status.is_unlocked


def test_exactly_24h_and_20_trades_unlocks():
    status = evaluate_demo_gate(NOW - timedelta(hours=24), 20, now=NOW)
    assert status.has_24h_run and status.has_20_clean_trades
    assert status.is_unlocked


def test_one_minute_short_of_24h_stays_locked():
    status = evaluate_demo_gate(NOW - timedelta(hours=23, minutes=59), 20, now=NOW)
    assert not status.has_24h_run
    assert status.has_20_clean_trades
    assert not status.is_unlocked


def test_19_trades_stays_locked_even_after_24h():
    status = evaluate_demo_gate(NOW - timedelta(hours=48), 19, now=NOW)
    assert status.has_24h_run
    assert not status.has_20_clean_trades
    assert not status.is_unlocked


def test_both_conditions_required():
    assert not evaluate_demo_gate(NOW - timedelta(hours=1), 5, now=NOW).is_unlocked
    assert evaluate_demo_gate(NOW - timedelta(hours=100), 100, now=NOW).is_unlocked


def test_naive_start_time_is_treated_as_utc():
    naive = (NOW - timedelta(hours=30)).replace(tzinfo=None)
    status = evaluate_demo_gate(naive, 20, now=NOW)
    assert status.elapsed_hours == 30.0
    assert status.is_unlocked


def test_start_time_in_the_future_never_goes_negative():
    status = evaluate_demo_gate(NOW + timedelta(hours=2), 20, now=NOW)
    assert status.elapsed_hours == 0
    assert not status.is_unlocked
