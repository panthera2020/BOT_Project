"""
Comprehensive unit tests for pure quantitative components:
- Risk schedule & capital tiers
- Position sizing & lot floor quantization
- Net RR gate
- Daily trade quota & Lagos boundary reset
"""
import pytest
from datetime import datetime, timezone
from src.core.models import BTCUSDT_CONFIG, TrendDirection
from src.core.risk_schedule import (
    calculate_base_risk,
    get_leverage_ceiling,
    evaluate_risk_schedule,
)
from src.core.position_sizing import calculate_position_size, calculate_loss_per_unit
from src.core.net_rr_gate import calculate_net_rr
from src.core.daily_cap import get_trading_day_id, DailyTradeTracker


class TestRiskSchedule:
    def test_capital_tiers_pure_schedule(self):
        # PRD Table:
        # Below $50 -> $1
        assert calculate_base_risk(9.99) == 1.0
        assert calculate_base_risk(49.99) == 1.0
        # $50 to $449 -> $1 + $0.5 per $50 above $50
        assert calculate_base_risk(50.0) == 1.0
        assert calculate_base_risk(100.0) == 1.5
        assert calculate_base_risk(449.0) == 4.5
        # $450 to $599 -> $5 cap
        assert calculate_base_risk(450.0) == 5.0
        assert calculate_base_risk(599.0) == 5.0
        # $600 and above -> $5 + $1 per $100 above $500
        assert calculate_base_risk(600.0) == 6.0
        assert calculate_base_risk(750.0) == 7.0

    def test_leverage_ceiling(self):
        # Below $10 disabled
        enabled, lev, reason = get_leverage_ceiling(9.99)
        assert not enabled
        assert lev == 0
        assert "MINIMUM" in reason

        # $10 to $50: 100x
        enabled, lev, _ = get_leverage_ceiling(10.0)
        assert enabled
        assert lev == 100

        enabled, lev, _ = get_leverage_ceiling(50.0)
        assert enabled
        assert lev == 100

        # Above $50: 50x
        enabled, lev, _ = get_leverage_ceiling(50.01)
        assert enabled
        assert lev == 50

    def test_half_risk_counter_trend(self):
        # At $50 capital, aligned trend uses 1.0R
        res_aligned = evaluate_risk_schedule(
            day_start_capital=50.0,
            current_equity=50.0,
            macro_trend=TrendDirection.UP,
            micro_trend=TrendDirection.UP,
        )
        assert res_aligned.is_trading_enabled
        assert res_aligned.effective_r == 1.0

        # Counter-trend uses half-risk (0.5R)
        res_counter = evaluate_risk_schedule(
            day_start_capital=50.0,
            current_equity=50.0,
            macro_trend=TrendDirection.UP,
            micro_trend=TrendDirection.DOWN,
        )
        assert res_counter.is_trading_enabled
        assert res_counter.effective_r == 0.5

    def test_range_trend_stand_down(self):
        res_range = evaluate_risk_schedule(
            day_start_capital=100.0,
            current_equity=100.0,
            macro_trend=TrendDirection.RANGE,
            micro_trend=TrendDirection.UP,
        )
        assert not res_range.is_trading_enabled
        assert res_range.rejection_reason == "TREND_RANGE_STAND_DOWN"


class TestPositionSizingAndNetRR:
    def test_worked_example_from_prd(self):
        """
        PRD Worked Example:
        E = 99,655, S = 99,640 (d = 15 + 345 = 360)
        s_in = 5, s_out = 10, r_taker = 0.055% (0.00055)
        R = $1, lot = 0.001
        """
        entry_price = 99655.0
        stop_price = 99640.0
        s_in = 5.0
        s_out = 10.0
        r_taker = 0.00055
        r_trade = 1.0

        # Step 1: Loss per BTC
        d, l = calculate_loss_per_unit(entry_price, stop_price, s_in, s_out, r_taker)
        assert d == 15.0  # entry to stop distance
        
        # Sizing with PRD distance d=360 worked example
        # In PRD: entry 99,655, sweep 99,655-345 = 99,310, stop 99,295 -> d = 360
        entry_prd = 100000.0
        stop_prd = 99640.0
        d_prd, l_prd = calculate_loss_per_unit(entry_prd, stop_prd, s_in, s_out, r_taker)
        assert d_prd == 360.0
        assert round(l_prd, 1) == 484.8

        res = calculate_position_size(
            r_trade=r_trade,
            entry_price=entry_prd,
            stop_price=stop_prd,
            config=BTCUSDT_CONFIG,
            s_in=s_in,
            s_out=s_out,
            r_taker=r_taker,
        )
        assert res.is_valid
        assert res.qty == 0.002
        assert res.notional == 200.0
        assert res.max_loss <= r_trade

        # Net RR calculation for d=360
        net_rr_res = calculate_net_rr(
            nominal_distance_d=d_prd,
            loss_per_unit_l=l_prd,
            entry_price=entry_prd,
            s_in=s_in,
            r_taker=r_taker,
            r_maker=0.00020,
            min_net_rr=2.0,
        )
        assert net_rr_res.passes_gate
        assert net_rr_res.net_rr >= 2.0

    def test_raw_qty_below_lot_size_is_rejected_never_rounded_up(self):
        # A stop distance that would require 0.0007 BTC (< 0.001 lot)
        res = calculate_position_size(
            r_trade=0.5,
            entry_price=100000.0,
            stop_price=99000.0,  # 1000 pt distance
            config=BTCUSDT_CONFIG,
        )
        assert not res.is_valid
        assert res.qty == 0.0
        assert res.rejection_reason == "QTY_BELOW_MINIMUM_LOT"


class TestDailyCapAndBoundary:
    def test_lagos_day_boundary_shift(self):
        # 00:59 UTC belongs to yesterday
        dt_yesterday = datetime(2026, 10, 17, 0, 59, tzinfo=timezone.utc)
        assert get_trading_day_id(dt_yesterday) == "2026-10-16"

        # 01:00 UTC belongs to today
        dt_today = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)
        assert get_trading_day_id(dt_today) == "2026-10-17"

    def test_two_trade_daily_limit(self):
        tracker = DailyTradeTracker(max_daily_trades=2)
        dt = datetime(2026, 10, 17, 5, 0, tzinfo=timezone.utc)

        can_open, _ = tracker.can_open_trade(dt)
        assert can_open
        assert tracker.record_trade(dt)

        can_open, _ = tracker.can_open_trade(dt)
        assert can_open
        assert tracker.record_trade(dt)

        # Third trade of the day must be rejected
        can_open, reason = tracker.can_open_trade(dt)
        assert not can_open
        assert "DAILY_CAP_REACHED" in reason
        assert not tracker.record_trade(dt)

        # Next day reset
        dt_next = datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)
        can_open, _ = tracker.can_open_trade(dt_next)
        assert can_open
