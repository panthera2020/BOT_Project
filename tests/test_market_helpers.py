"""Tests for the pure market helpers: candles, 2m aggregation, swings and trend."""
from src.core.models import TrendDirection
from src.engine import market as m
from src.strategy.strategy1 import Candle
from tests.sim_exchange import default_markets, zigzag

MIN = 60_000


def c(ts, o, h, l, cl):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=cl)


def test_parse_orders_oldest_first_and_skips_bad_rows():
    rows = [["200", "2", "3", "1", "2.5", "9", "9"], ["bad"], ["100", "1", "2", "0.5", "1.5", "9", "9"]]
    out = m.parse_candles(rows)
    assert [x.timestamp for x in out] == [100, 200]


def test_drop_forming_removes_the_newest_only():
    assert [x.timestamp for x in m.drop_forming([c(1, 1, 1, 1, 1), c(2, 1, 1, 1, 1)])] == [1]
    assert m.drop_forming([]) == []


def test_two_minute_candles_need_both_minutes_and_an_even_start():
    ones = [c(0, 10, 12, 9, 11), c(MIN, 11, 15, 10, 14), c(2 * MIN, 14, 16, 13, 15), c(4 * MIN, 1, 2, 0, 1), c(5 * MIN, 1, 3, 0, 2),
            c(7 * MIN, 5, 5, 5, 5)]
    out = m.aggregate_2m(ones)
    assert [x.timestamp for x in out] == [0, 4 * MIN]  # 2m (one minute only) and 6m/7m (odd start) are not built
    first = out[0]
    assert (first.open, first.high, first.low, first.close) == (10, 15, 9, 14)


def test_swing_points_and_unswept_low():
    pivots = [100, 110, 102, 112, 104, 111]
    cs = [Candle(int(x[0]), x[1], x[2], x[3], x[4]) for x in zigzag(0, 900_000, pivots, per_leg=4, wick=0.5)]
    lows = [cs[i].low for i in m.swing_low_indexes(cs)]
    assert any(abs(v - 101.5) < 1 for v in lows) and any(abs(v - 103.5) < 1 for v in lows)
    assert abs(m.latest_unswept_swing_low(cs) - 103.5) < 1
    # candles that undercut the latest low (without forming a new confirmed swing) make it
    # "swept", so the older swing low takes over
    undercut = cs + [c(10**9, 105, 105, 102.5, 104), c(10**9 + 1, 104, 106, 101.8, 105), c(10**9 + 2, 105, 107, 101.9, 106)]
    assert abs(m.latest_unswept_swing_low(undercut) - 101.5) < 1


def test_trend_up_down_range_and_too_little_data():
    up = [Candle(int(x[0]), x[1], x[2], x[3], x[4]) for x in zigzag(0, 900_000, [100, 110, 102, 112, 104, 114, 106, 116, 108])]
    down = [Candle(int(x[0]), x[1], x[2], x[3], x[4]) for x in zigzag(0, 900_000, [116, 106, 114, 104, 112, 102, 110, 100, 108])]
    flat = [Candle(int(x[0]), x[1], x[2], x[3], x[4]) for x in zigzag(0, 900_000, [100, 110, 100, 110, 100, 110, 100, 110])]
    assert m.detect_trend(up) == TrendDirection.UP
    assert m.detect_trend(down) == TrendDirection.DOWN
    assert m.detect_trend(flat) == TrendDirection.RANGE
    assert m.detect_trend(up[:4]) == TrendDirection.RANGE


def test_round_to_tick():
    assert m.round_to_tick(100685.04, 0.1) == 100685.0
    assert m.round_to_tick(100685.06, 0.1) == 100685.1
    assert m.round_to_tick(7, 1) == 7


def test_the_test_market_really_contains_the_setup():
    mk = default_markets()
    c15 = m.drop_forming(m.parse_candles(mk["15"]))
    swing = m.latest_unswept_swing_low(c15[:-1][-60:])
    assert swing is not None and abs(swing - 101200) < 10
    assert m.detect_trend(c15) == TrendDirection.UP
    assert m.detect_trend(m.drop_forming(m.parse_candles(mk["60"]))) == TrendDirection.UP


def test_authorized_direction_follows_the_prd_trend_table():
    U, D, R = TrendDirection.UP, TrendDirection.DOWN, TrendDirection.RANGE
    # (1h, 15m) -> side: Up/Up long, Down/Down short, Up/Down short (counter), Down/Up long (counter), any Range -> none
    assert m.authorized_direction(U, U) == "Buy"
    assert m.authorized_direction(D, D) == "Sell"
    assert m.authorized_direction(U, D) == "Sell"
    assert m.authorized_direction(D, U) == "Buy"
    for other in (U, D, R):
        assert m.authorized_direction(R, other) is None
        assert m.authorized_direction(other, R) is None


def test_unswept_swing_high_mirrors_the_swing_low_rule():
    pivots = [100, 110, 102, 112, 104, 108]
    cs = [Candle(int(x[0]), x[1], x[2], x[3], x[4]) for x in zigzag(0, 900_000, pivots, per_leg=4, wick=0.5)]
    assert abs(m.latest_unswept_swing_high(cs) - 112.5) < 1
    # candles that exceed the latest high without forming a confirmed swing make it "swept"; the older
    # high (110.5) was already exceeded by it, so there is no unswept swing high left
    exceed = cs + [c(10**9, 107, 113, 106, 108), c(10**9 + 1, 108, 113.5, 107, 109), c(10**9 + 2, 109, 113.2, 108, 110)]
    assert m.latest_unswept_swing_high(exceed) is None
