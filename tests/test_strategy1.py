"""
Unit tests for Strategy 1 state machine and technical entry rules.
"""
import pytest
from src.strategy.strategy1 import (
    Candle,
    Strategy1StateMachine,
    detect_trend_from_pivots,
)
from src.core.models import TrendDirection


def test_trend_detection():
    # Bullish candles
    bullish_series = [
        Candle(timestamp=1, open=100, high=105, low=98, close=104),
        Candle(timestamp=2, open=104, high=110, low=102, close=108),
        Candle(timestamp=3, open=108, high=115, low=106, close=114),
        Candle(timestamp=4, open=114, high=120, low=112, close=119),
    ]
    assert detect_trend_from_pivots(bullish_series) == TrendDirection.UP

    # Bearish series
    bearish_series = [
        Candle(timestamp=1, open=120, high=122, low=115, close=116),
        Candle(timestamp=2, open=116, high=118, low=110, close=112),
        Candle(timestamp=3, open=112, high=114, low=105, close=106),
        Candle(timestamp=4, open=106, high=108, low=100, close=101),
    ]
    assert detect_trend_from_pivots(bearish_series) == TrendDirection.DOWN

    # Indeterminate series
    choppy_series = [
        Candle(timestamp=1, open=100, high=105, low=95, close=100),
        Candle(timestamp=2, open=100, high=102, low=98, close=101),
        Candle(timestamp=3, open=101, high=107, low=99, close=103),
        Candle(timestamp=4, open=103, high=104, low=97, close=99),
    ]
    assert detect_trend_from_pivots(choppy_series) == TrendDirection.RANGE


def test_15m_sweep_evaluation():
    sm = Strategy1StateMachine(symbol="BTCUSDT")
    swing_low = 99655.0

    # Case 1: Wick sweeps low and closes back above (VALID)
    sweep_valid = Candle(timestamp=100, open=99700, high=99750, low=99640, close=99680)
    ok, reason = sm.evaluate_15m_sweep(swing_low, sweep_valid)
    assert ok
    assert reason == "SWEEP_CONFIRMED"

    # Case 2: Candle closes below swing low (INVALIDATED / CANCELLED)
    sweep_fail = Candle(timestamp=101, open=99700, high=99710, low=99620, close=99630)
    ok, reason = sm.evaluate_15m_sweep(swing_low, sweep_fail)
    assert not ok
    assert reason == "SWEEP_FAILED_CLOSE_BELOW_SWING"

    # Case 3: No sweep took place
    no_sweep = Candle(timestamp=102, open=99700, high=99750, low=99660, close=99710)
    ok, reason = sm.evaluate_15m_sweep(swing_low, no_sweep)
    assert not ok
    assert reason == "NO_SWEEP"


def test_2m_micro_trigger():
    sm = Strategy1StateMachine(symbol="BTCUSDT")
    sweep_extreme = 99640.0

    # 3 consecutive bullish candles
    candles_valid = [
        Candle(timestamp=1, open=99660, high=99670, low=99650, close=99668),
        Candle(timestamp=2, open=99668, high=99680, low=99665, close=99678),
        Candle(timestamp=3, open=99678, high=99695, low=99675, close=99690),
    ]
    ok, entry, reason = sm.check_2m_micro_trigger(candles_valid, sweep_extreme)
    assert ok
    assert entry == 99690.0
    assert reason == "TRIGGER_CONFIRMED"

    # Invalidation: candle dips below sweep extreme
    candles_inval = [
        Candle(timestamp=1, open=99660, high=99670, low=99635, close=99668),
    ]
    ok, _, reason = sm.check_2m_micro_trigger(candles_inval, sweep_extreme)
    assert not ok
    assert reason == "INVALIDATED_BELOW_SWEEP_EXTREME"

    # Fewer than 3 consecutive bullish candles (one bearish)
    candles_not_all_bullish = [
        Candle(timestamp=1, open=99660, high=99670, low=99650, close=99668),
        Candle(timestamp=2, open=99668, high=99675, low=99660, close=99662), # bearish
        Candle(timestamp=3, open=99662, high=99680, low=99660, close=99675),
    ]
    ok, _, reason = sm.check_2m_micro_trigger(candles_not_all_bullish, sweep_extreme)
    assert not ok
    assert reason == "LESS_THAN_THREE_CONSECUTIVE_BULLISH"
