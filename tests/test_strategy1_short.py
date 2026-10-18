"""The short side of Strategy 1 is the exact mirror of the long side."""
from src.strategy.strategy1 import Candle, Strategy1StateMachine

S = Strategy1StateMachine()


def c(o, h, l, cl, ts=0):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=cl)


def test_short_sweep_needs_a_wick_above_and_a_close_back_below():
    assert S.evaluate_15m_sweep_short(100.0, c(99, 101, 98, 99.5)) == (True, "SWEEP_CONFIRMED")
    assert S.evaluate_15m_sweep_short(100.0, c(99, 100.0, 98, 99.5)) == (False, "NO_SWEEP")      # wick must be strictly above
    assert S.evaluate_15m_sweep_short(100.0, c(99, 99.9, 98, 99.5)) == (False, "NO_SWEEP")
    assert S.evaluate_15m_sweep_short(100.0, c(99, 101, 98, 100.5)) == (False, "SWEEP_FAILED_CLOSE_ABOVE_SWING")
    assert S.evaluate_15m_sweep_short(100.0, c(99, 101, 98, 100.0)) == (True, "SWEEP_CONFIRMED")   # a close AT the level is not above it


def test_short_trigger_needs_three_bearish_candles_and_never_a_high_above_the_sweep():
    bear = [c(10, 10.5, 9, 9.5), c(9.5, 9.8, 8.5, 9.0), c(9.0, 9.2, 8.0, 8.5)]
    assert S.check_2m_micro_trigger_short(bear, 11.0) == (True, 8.5, "TRIGGER_CONFIRMED")
    assert S.check_2m_micro_trigger_short([], 11.0)[2] == "AWAITING_2M_CANDLES"
    assert S.check_2m_micro_trigger_short(bear[:2], 11.0)[2] == "INSUFFICIENT_2M_CANDLES"
    mixed = [bear[0], c(9.5, 9.8, 8.5, 9.9), bear[2]]
    assert S.check_2m_micro_trigger_short(mixed, 11.0)[2] == "LESS_THAN_THREE_CONSECUTIVE_BEARISH"
    broke = [c(10, 11.5, 9, 9.5)] + bear[1:]
    assert S.check_2m_micro_trigger_short(broke, 11.0) == (False, None, "INVALIDATED_ABOVE_SWEEP_EXTREME")
    # only the last three must be bearish, as on the long side
    assert S.check_2m_micro_trigger_short([c(8, 9, 7, 8.9)] + bear, 11.0)[0] is True
