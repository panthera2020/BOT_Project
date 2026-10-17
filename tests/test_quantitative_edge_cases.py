"""
Additional edge-case tests to achieve ~100% test coverage across quantitative models.
"""
import pytest
from datetime import datetime, timezone
from src.core.daily_cap import get_trading_day_id, DailyTradeTracker
from src.core.net_rr_gate import calculate_net_rr
from src.core.position_sizing import calculate_position_size
from src.core.risk_schedule import evaluate_risk_schedule, calculate_base_risk
from src.core.models import BTCUSDT_CONFIG, TrendDirection


def test_daily_cap_edge_cases():
    tracker = DailyTradeTracker(max_daily_trades=2)
    # Default now_utc when none passed
    day_id = get_trading_day_id(None)
    assert len(day_id) == 10

    # reset_if_new_day
    dt1 = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
    tracker.record_trade(dt1)
    assert tracker.daily_trade_count == 1
    assert not tracker.reset_if_new_day(dt1)

    dt2 = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    assert tracker.reset_if_new_day(dt2)
    assert tracker.daily_trade_count == 0


def test_net_rr_invalid_loss():
    res = calculate_net_rr(nominal_distance_d=10, loss_per_unit_l=0, entry_price=1000)
    assert not res.passes_gate
    assert res.rejection_reason == "INVALID_LOSS_PER_UNIT"


def test_position_sizing_notional_rejection():
    # Min notional for BTC is 100 USDT; 0.001 BTC at 50,000 is only 50 USDT notional (< 100 USDT)
    cheap_config = BTCUSDT_CONFIG
    res = calculate_position_size(
        r_trade=0.5,
        entry_price=50000.0,
        stop_price=49700.0,
        config=cheap_config,
    )
    assert not res.is_valid
    assert res.qty == 0.001
    assert res.notional == 50.0
    assert res.rejection_reason == "NOTIONAL_BELOW_EXCHANGE_MINIMUM"


def test_risk_schedule_below_ten_equity():
    res = evaluate_risk_schedule(
        day_start_capital=50.0,
        current_equity=8.0,
        macro_trend=TrendDirection.UP,
        micro_trend=TrendDirection.UP,
    )
    assert not res.is_trading_enabled
    assert res.rejection_reason == "CAPITAL_BELOW_MINIMUM_EQUITY"
