"""
The strategy list: each strategy's rules on hand-built candles, the shared gates every
strategy goes through (risk modes), and each one run end to end on the simulated Bybit:
signal -> protected trade -> closed -> recorded under its own name.
"""
from datetime import datetime, timezone

import pytest

from src.core.models import TrendDirection
from src.engine import market
from src.engine.strategies import DEFAULT_STRATEGY, REGISTRY, STRATEGY_CLASSES, strategy_catalog
from src.engine.strategies.base import RISK_FULL, RISK_HALF_UNALIGNED, RISK_TABLE, Entry, Strategy
from src.engine.strategies.breakout_retest import check_retest, find_breakout
from src.engine.strategies.range_reversion import reversion_signal
from src.engine.strategies.session_breakout import evaluate_session
from src.engine.strategies.trend_pullback import pullback_signal
from src.strategy.strategy1 import Candle
from tests.sim_exchange import MIN, SWEEP_END_MS, default_markets
from tests.strategy_markets import (M15, flat_candles, flip, pullback_candles, reversion_candles, rows,
                                    ts, with_markets)
from tests.test_demo_engine import Harness

BUF = 15.0
UP, DOWN, RANGE = TrendDirection.UP, TrendDirection.DOWN, TrendDirection.RANGE


# ---------------------------------------------------------------- registry
def test_registry_has_the_six_strategies_with_unique_ids():
    ids = [c.id for c in STRATEGY_CLASSES]
    assert ids == ["sweep_reversal", "sweep_reversal_loose", "trend_pullback", "breakout_retest", "session_breakout", "range_reversion"]
    assert len(set(ids)) == len(ids) and set(REGISTRY) == set(ids)
    assert DEFAULT_STRATEGY == "sweep_reversal"
    for item in strategy_catalog():
        assert item["name"] and item["summary"] and len(item["rules"]) >= 3


def test_default_strategy_constants_agree():
    from src.core.strategy_progress import DEFAULT_STRATEGY as progress_default
    assert progress_default == DEFAULT_STRATEGY


def test_the_looser_sweep_is_a_separate_strategy_and_the_original_is_untouched():
    from src.engine.strategies.sweep_reversal import SweepReversal, SweepReversalLoose
    assert (SweepReversal.swing_n, SweepReversal.window_minutes, SweepReversal.risk_mode) == (market.FRACTAL, 60, RISK_TABLE)
    assert (SweepReversalLoose.swing_n, SweepReversalLoose.window_minutes, SweepReversalLoose.risk_mode) == (1, 120, RISK_HALF_UNALIGNED)
    assert SweepReversal.legacy_state and not SweepReversalLoose.legacy_state


# ---------------------------------------------------------------- indicators
def test_ema_seed_and_recursion():
    out = market.ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None] and out[2] == 2.0
    assert out[3] == pytest.approx(4 * 0.5 + 2.0 * 0.5) and out[4] == pytest.approx(5 * 0.5 + out[3] * 0.5)
    assert market.ema([1, 2], 3) == [None, None]


def test_rsi_extremes_and_midpoint():
    assert market.rsi(list(range(1, 30))) == 100.0
    assert market.rsi(list(range(30, 1, -1))) == pytest.approx(0.0)
    assert market.rsi([5.0] * 20) == 50.0
    assert market.rsi([1, 2]) is None
    # alternating +1 / -1 is balanced
    assert market.rsi([10 + (i % 2) for i in range(40)], 14) == pytest.approx(50.0, abs=3)


def test_rsi_matches_a_hand_computed_wilder_value():
    closes = [44, 44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28]
    assert market.rsi(closes, 14) == pytest.approx(72.44, abs=0.01)  # 14 changes: avg gain 0.26286, avg loss 0.10 -> 100 - 100/(1 + 2.6286)


def test_bollinger_bands():
    mid, up, low = market.bollinger([1.0, 3.0] * 10, 20, 2.0)
    assert mid == 2.0 and up == pytest.approx(4.0) and low == pytest.approx(0.0)
    assert market.bollinger([1.0] * 5, 20) is None


def test_swing_helpers_accept_a_smaller_fractal():
    c = [Candle(i * 1000, 10, h, l, 10) for i, (h, l) in enumerate([(11, 9), (12, 8), (11, 9), (13, 7), (11, 9)])]
    assert market.swing_low_indexes(c, 1) == [1, 3] or market.swing_low_indexes(c, 1) == [3]
    assert market.swing_low_indexes(c) == market.swing_low_indexes(c, 2)   # default unchanged


# ---------------------------------------------------------------- trend pullback
def test_pullback_long_signal_and_stop():
    c = pullback_candles()
    signal, why = pullback_signal(c, UP, BUF)
    assert signal == ("Buy", min(x.low for x in c[-5:]) - BUF), why


def test_pullback_short_is_the_mirror():
    c = [flip(x) for x in pullback_candles()]
    signal, why = pullback_signal(c, DOWN, BUF)
    assert signal == ("Sell", max(x.high for x in c[-5:]) + BUF), why


@pytest.mark.parametrize("change", ["range_1h", "wrong_1h", "no_touch", "bearish", "closes_below_ema", "ema_order"])
def test_pullback_refusals(change):
    c, macro = pullback_candles(), UP
    last = c[-1]
    if change == "range_1h":
        macro = RANGE
    elif change == "wrong_1h":
        macro = DOWN
    elif change == "no_touch":
        c[-1] = Candle(last.timestamp, last.open, last.high, last.close - 5, last.close)
    elif change == "bearish":
        c[-1] = Candle(last.timestamp, last.close + 5, last.close + 10, last.low, last.close)
    elif change == "closes_below_ema":
        c[-1] = Candle(last.timestamp, last.low + 1, last.low + 5, last.low, last.low + 2)
    elif change == "ema_order":
        c = [Candle(x.timestamp, x.open, x.high, x.low, x.close) for x in c]
        c = [Candle(x.timestamp, 200000 - x.open if i < 45 else x.open, x.high, x.low, 200000 - x.close if i < 45 else x.close) for i, x in enumerate(c)]
    assert pullback_signal(c, macro, BUF)[0] is None


def test_pullback_needs_the_ema_order_to_match_the_hourly_trend():
    """A bounce off the EMA20 inside a falling 15m market is not a long, even with the 1h Up."""
    falling = [flip(x) for x in pullback_candles()]
    closes = [x.close for x in falling[:-1]]
    f0 = market.ema(closes, 20)[-1]
    last = falling[-1]
    falling[-1] = Candle(last.timestamp, f0 - 10, f0 + 70, f0 - 120, f0 + 60)       # a perfect bullish bounce
    assert market.ema([x.close for x in falling], 20)[-1] < market.ema([x.close for x in falling], 50)[-1]
    signal, why = pullback_signal(falling, UP, BUF)
    assert signal is None and "EMA50" in why


def test_pullback_needs_enough_history():
    assert pullback_signal(pullback_candles()[-30:], UP, BUF)[0] is None


# ---------------------------------------------------------------- breakout and retest
def breakout_set(close=102200.0):
    base = flat_candles(SWEEP_END_MS - M15, 30)
    last = Candle(SWEEP_END_MS - M15, 102040, close + 10, 102030, close)
    return base + [last]


def test_breakout_up_down_and_none():
    assert find_breakout(breakout_set(), UP) == ("Buy", 102050.0)
    assert find_breakout([flip(x) for x in breakout_set()], DOWN) == ("Sell", 204000.0 - 102050.0)
    assert find_breakout(breakout_set(102040.0), UP) is None       # closes inside the range
    assert find_breakout(flat_candles(SWEEP_END_MS, 10), UP) is None  # too little history


def test_breakout_respects_the_hourly_trend():
    assert find_breakout(breakout_set(), DOWN) is None
    assert find_breakout(breakout_set(), RANGE) == ("Buy", 102050.0)
    assert find_breakout([flip(x) for x in breakout_set()], UP) is None


def test_retest_outcomes_long_and_short():
    setup = {"side": "Buy", "level": 102050.0, "armed_ts": 0}
    t = SWEEP_END_MS
    assert check_retest(setup, Candle(t, 102080, 102200, 102040, 102150), BUF) == ("entry", 102040 - BUF)
    assert check_retest(setup, Candle(t, 102080, 102200, 102060, 102150), BUF)[0] == "wait"     # never came back
    assert check_retest(setup, Candle(t, 102080, 102100, 102030, 102040), BUF)[0] == "invalid"  # closed through
    assert check_retest(setup, Candle(t, 102150, 102200, 102040, 102100), BUF)[0] == "wait"     # touched but bearish
    short = {"side": "Sell", "level": 102050.0, "armed_ts": 0}
    assert check_retest(short, Candle(t, 102000, 102060, 101900, 101950), BUF) == ("entry", 102060 + BUF)
    assert check_retest(short, Candle(t, 102000, 102070, 101900, 102060), BUF)[0] == "invalid"


# ---------------------------------------------------------------- session breakout
def session_set(day=(2026, 10, 19), hour=8, minute=0, close=102150.0, range_close=None):
    """Filler, then the four opening-range candles, one inside candle and a closing candle."""
    start = ts(*day, hour, minute)
    cs = flat_candles(start, 24, mid=102000.0, half=50.0)
    # opening range: high 102100, low 101700
    cs += [Candle(start + i * M15, 102000, 102100 if i == 1 else 102050, 101700 if i == 2 else 101950, 102000) for i in range(4)]
    cs.append(Candle(start + 4 * M15, 102000, 102090, 101950, 102020))              # inside, first candle after the hour
    cs.append(Candle(start + 5 * M15, 102020, close + 10, 102010, close))
    return cs, start


def test_session_long_break_of_the_london_range():
    cs, start = session_set()
    sig, why = evaluate_session(cs, BUF, set())
    assert sig == ("Buy", 101700 - BUF, "2026-10-19:London"), why


def test_session_short_break_and_new_york():
    cs, _ = session_set(close=101650.0)
    sig, _ = evaluate_session(cs, BUF, set())
    assert sig == ("Sell", 102100 + BUF, "2026-10-19:London")
    cs, _ = session_set(hour=14, minute=30)
    sig, _ = evaluate_session(cs, BUF, set())
    assert sig[0] == "Buy" and sig[2] == "2026-10-19:New York"


def test_session_refusals():
    cs, _ = session_set()
    assert evaluate_session(cs, BUF, {"2026-10-19:London"})[0] is None              # already traded
    cs, _ = session_set(close=102050.0)
    assert "intact" in evaluate_session(cs, BUF, set())[1]                           # still inside
    cs, start = session_set()
    assert evaluate_session(cs[:-2], BUF, set())[0] is None                          # too early: still building the range
    quiet = flat_candles(ts(2026, 10, 19, 13, 0), 30)
    assert "outside" in evaluate_session(quiet, BUF, set())[1]                       # between sessions
    late, _ = session_set(close=102150.0)
    late = late[:-1] + [Candle(late[-1].timestamp + 12 * M15, 102020, 102160, 102010, 102150)]
    assert evaluate_session(late, BUF, set())[0] is None                             # window (3h after the hour) is over


def test_session_incomplete_range_is_not_traded():
    cs, start = session_set()
    cs = [c for c in cs if c.timestamp != start + M15]                               # a missing candle in the range
    assert "incomplete" in evaluate_session(cs, BUF, set())[1]


# ---------------------------------------------------------------- range reversion
def test_reversion_long_signal():
    c = reversion_candles(SWEEP_END_MS)
    micro = market.detect_trend(c)
    assert micro == RANGE
    signal, why = reversion_signal(c, micro, BUF)
    assert signal == ("Buy", c[-1].low - BUF), why


def test_reversion_short_is_the_mirror():
    c = [flip(x) for x in reversion_candles(SWEEP_END_MS)]
    signal, why = reversion_signal(c, market.detect_trend(c), BUF)
    assert signal == ("Sell", c[-1].high + BUF), why


def test_reversion_refusals():
    c = reversion_candles(SWEEP_END_MS)
    assert reversion_signal(c, UP, BUF)[0] is None                                   # only in a range
    last = c[-1]
    c2 = c[:-1] + [Candle(last.timestamp, last.close + 5, last.high, last.low, last.close)]       # bearish candle
    assert reversion_signal(c2, RANGE, BUF)[0] is None
    c3 = c[:-1] + [Candle(last.timestamp, last.open, last.high, last.close - 5, last.close)]      # no stab below the band
    assert reversion_signal(c3, RANGE, BUF)[0] is None
    assert reversion_signal(c[-10:], RANGE, BUF)[0] is None                          # not enough candles
    calm = reversion_candles(SWEEP_END_MS, slide_step=0.0)                           # same stab below the band, but RSI is not oversold
    _, _, lower = market.bollinger([x.close for x in calm])
    assert calm[-1].low < lower < calm[-1].close and calm[-1].is_bullish             # every other condition is met
    assert reversion_signal(calm, RANGE, BUF)[0] is None
    mirrored = [flip(x) for x in calm]
    assert reversion_signal(mirrored, RANGE, BUF)[0] is None


# ---------------------------------------------------------------- risk modes (shared gate)
class FixedStrategy(Strategy):
    id, name = "fixed", "fixed"

    def __init__(self, engine, entry):
        super().__init__(engine)
        self.entry = entry

    async def scan(self, now, equity, c15):
        e, self.entry = self.entry, None
        return e


def flat_hour_markets():
    """UP 15m trend (default markets) but a ranging 1h."""
    mk = default_markets()
    mk["60"] = rows(flat_candles(SWEEP_END_MS - 3600_000, 40, half=40.0)[:-1], SWEEP_END_MS - 3600_000, 102000.0)
    return mk


async def run_entry(mode, markets=None, side="Buy"):
    h = Harness("Buy", markets=markets)
    h.engine.strategy = FixedStrategy(h.engine, Entry(side=side, stop_price=101500.0, trigger_price=102300.0, risk_mode=mode))
    await h.engine.tick()
    return h


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,hour_range,expected_r", [
    (RISK_TABLE, False, 10.0), (RISK_TABLE, True, None),
    (RISK_HALF_UNALIGNED, False, 10.0), (RISK_HALF_UNALIGNED, True, 5.0),
    (RISK_FULL, False, 10.0), (RISK_FULL, True, 10.0),
])
async def test_risk_mode_sets_the_risk(mode, hour_range, expected_r):
    h = await run_entry(mode, flat_hour_markets() if hour_range else None)
    if expected_r is None:
        assert h.state["active_position"] is None and "TREND_RANGE_STAND_DOWN" in h.engine.status["last_decision"]
    else:
        assert h.state["active_position"]["effective_r"] == expected_r


@pytest.mark.asyncio
async def test_the_table_mode_still_rechecks_the_side_but_the_others_do_not():
    h = await run_entry(RISK_TABLE, side="Sell")           # trend table says long only
    assert h.state["active_position"] is None and "trend table now allows long" in h.engine.status["last_decision"]
    h = await run_entry(RISK_FULL, side="Sell", markets=None)
    # a short with a stop BELOW the entry is refused by the shared "wrong side" check
    assert h.state["active_position"] is None and "wrong side" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_every_mode_goes_through_the_same_sizing_and_rr_gates():
    h = Harness("Buy")
    h.engine.strategy = FixedStrategy(h.engine, Entry(side="Buy", stop_price=102299.0, trigger_price=102300.0, risk_mode=RISK_FULL))
    await h.engine.tick()                                   # 1 point stop: net RR cannot reach 2
    assert h.state["active_position"] is None and "trigger skipped" in h.engine.status["last_decision"]


# ---------------------------------------------------------------- end to end, per strategy
async def finish_trade(h, strategy_id):
    rec = h.state["active_position"]
    assert rec["strategy"] == strategy_id
    h.sim.move_price(rec["tp2_price"] + (1 if rec["side"] == "Buy" else -1))   # both take-profits fill on Bybit
    for _ in range(3):
        await h.engine.tick()
    assert h.state["active_position"] is None
    assert h.trades[-1]["strategy"] == strategy_id and h.trades[-1]["pnl"] > 0 and h.trades[-1]["clean"]
    return h.trades[-1]


@pytest.mark.asyncio
async def test_trend_pullback_end_to_end():
    c = pullback_candles()
    h = Harness("Buy", markets=with_markets(c, SWEEP_END_MS, c[-1].close), strategy_id="trend_pullback")
    h.sim.price = c[-1].close
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["side"] == "Buy" and rec["strategy"] == "trend_pullback"
    assert rec["stop_price"] == min(x.low for x in c[-5:]) - BUF
    assert rec["effective_r"] == 5.0                        # 15m has no swings (Range) so the 1h Up is not aligned: half risk
    assert h.sim.position["stop"] == rec["stop_price"] and len(h.sim.orders) == 2
    await finish_trade(h, "trend_pullback")


@pytest.mark.asyncio
async def test_trend_pullback_stands_down_in_an_hourly_range():
    c = pullback_candles()
    mk = with_markets(c, SWEEP_END_MS, c[-1].close, base=flat_hour_markets())
    h = Harness("Buy", markets=mk, strategy_id="trend_pullback")
    await h.engine.tick()
    assert h.state["active_position"] is None and "1h trend is Range" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_a_candle_is_judged_once_not_every_tick():
    c = pullback_candles()
    mk = with_markets(c, SWEEP_END_MS, c[-1].close, base=flat_hour_markets())
    h = Harness("Buy", markets=mk, strategy_id="trend_pullback")
    await h.engine.tick()
    first = h.engine.status["last_decision"]
    h.engine.status["last_decision"] = "sentinel"
    await h.engine.tick()
    assert h.engine.status["last_decision"] == "sentinel" and first != "sentinel"


@pytest.mark.asyncio
async def test_breakout_retest_end_to_end_across_two_candles():
    base = flat_candles(SWEEP_END_MS - 2 * M15, 30)
    breakout = Candle(SWEEP_END_MS - 2 * M15, 102040, 102210, 102030, 102200)
    now = datetime(2026, 10, 18, 11, 46, tzinfo=timezone.utc)
    h = Harness("Buy", markets=with_markets(base + [breakout], SWEEP_END_MS - M15, 102200.0), strategy_id="breakout_retest", now=now)
    await h.engine.tick()
    assert h.state["active_position"] is None
    setup = h.state["engine_state"]["strategies"]["breakout_retest"]["setup"]
    assert setup["side"] == "Buy" and setup["level"] == 102050.0
    retest = Candle(SWEEP_END_MS - M15, 102080, 102200, 101750, 102150)
    h.sim.markets = with_markets(base + [breakout, retest], SWEEP_END_MS, 102150.0)
    h.sim.price = 102150.0
    h.clock.advance(15 * 60)
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["stop_price"] == 101750 - BUF and rec["strategy"] == "breakout_retest"
    assert h.state["engine_state"]["strategies"]["breakout_retest"]["setup"] is None
    await finish_trade(h, "breakout_retest")


@pytest.mark.asyncio
async def test_breakout_setup_survives_a_restart_and_a_close_back_through_cancels_it():
    base = flat_candles(SWEEP_END_MS - 2 * M15, 30)
    breakout = Candle(SWEEP_END_MS - 2 * M15, 102040, 102210, 102030, 102200)
    h = Harness("Buy", markets=with_markets(base + [breakout], SWEEP_END_MS - M15, 102200.0), strategy_id="breakout_retest")
    await h.engine.tick()
    h.restart()
    assert h.state["engine_state"]["strategies"]["breakout_retest"]["setup"]["level"] == 102050.0
    through = Candle(SWEEP_END_MS - M15, 102100, 102110, 102000, 102020)
    h.sim.markets = with_markets(base + [breakout, through], SWEEP_END_MS, 102020.0)
    await h.engine.tick()
    assert h.state["engine_state"]["strategies"]["breakout_retest"]["setup"] is None
    assert "cancelled" in h.engine.status["last_decision"] and h.state["active_position"] is None


@pytest.mark.asyncio
async def test_breakout_setup_expires():
    base = flat_candles(SWEEP_END_MS - 12 * M15, 30)
    breakout = Candle(SWEEP_END_MS - 12 * M15, 102040, 102210, 102030, 102200)
    h = Harness("Buy", markets=with_markets(base + [breakout], SWEEP_END_MS - 11 * M15, 102200.0), strategy_id="breakout_retest")
    await h.engine.tick()
    later = [Candle(SWEEP_END_MS - (11 - i) * M15, 102200, 102250, 102150, 102200) for i in range(11)]
    h.sim.markets = with_markets(base + [breakout] + later, SWEEP_END_MS, 102200.0)
    await h.engine.tick()
    assert h.state["engine_state"]["strategies"]["breakout_retest"]["setup"] is None   # too late: the retest window is over
    assert h.state["active_position"] is None


@pytest.mark.asyncio
async def test_session_breakout_end_to_end_and_only_once_per_session():
    cs, start = session_set()
    last = cs[-1]
    now = datetime.fromtimestamp((last.timestamp + M15) / 1000 + 30, tz=timezone.utc)
    h = Harness("Buy", markets=with_markets(cs, last.timestamp + M15, last.close), strategy_id="session_breakout", now=now)
    h.sim.price = last.close
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["side"] == "Buy" and rec["stop_price"] == 101700 - BUF and rec["effective_r"] == 10.0
    assert h.state["engine_state"]["strategies"]["session_breakout"]["done"] == ["2026-10-19:London"]
    await finish_trade(h, "session_breakout")
    # same session, a later breakout candle: already traded
    again = cs + [Candle(last.timestamp + M15, 102150, 102260, 102140, 102250)]
    h.sim.markets = with_markets(again, last.timestamp + 2 * M15, 102250.0)
    h.clock.advance(15 * 60)
    await h.engine.tick()
    assert h.state["active_position"] is None and "already traded" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_range_reversion_end_to_end_with_a_flipped_short():
    c = reversion_candles(SWEEP_END_MS)
    h = Harness("Buy", markets=with_markets(c, SWEEP_END_MS, c[-1].close), strategy_id="range_reversion")
    h.sim.price = c[-1].close
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["side"] == "Buy" and rec["stop_price"] == pytest.approx(c[-1].low - BUF, abs=0.05) and rec["effective_r"] == 10.0
    await finish_trade(h, "range_reversion")

    flipped = [flip(x) for x in c]
    mk = with_markets(flipped, SWEEP_END_MS, flipped[-1].close)
    from tests.sim_exchange import mirror
    mk["60"] = mirror(default_markets())["60"]
    s = Harness("Sell", markets=mk, strategy_id="range_reversion", mirror_markets=False)
    s.sim.price = flipped[-1].close
    await s.engine.tick()
    rec = s.state["active_position"]
    assert rec and rec["side"] == "Sell" and rec["stop_price"] == pytest.approx(flipped[-1].high + BUF, abs=0.05)


@pytest.mark.asyncio
async def test_range_reversion_does_nothing_in_a_trending_15m_market():
    c = pullback_candles()                                   # steadily rising
    h = Harness("Buy", markets=with_markets(c, SWEEP_END_MS, c[-1].close), strategy_id="range_reversion")
    await h.engine.tick()
    assert h.state["active_position"] is None


# ---------------------------------------------------------------- the loose sweep
@pytest.mark.asyncio
async def test_loose_sweep_trades_the_default_setup_and_keeps_its_own_state():
    h = Harness("Buy", strategy_id="sweep_reversal_loose")
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["strategy"] == "sweep_reversal_loose" and rec["side"] == "Buy"
    assert "setup" not in {k for k in h.state["engine_state"] if k == "setup" and h.state["engine_state"][k]}   # original's slot untouched
    assert "sweep_reversal_loose" in h.state["engine_state"]["strategies"]


@pytest.mark.asyncio
async def test_loose_sweep_trades_when_the_hour_is_a_range_with_half_risk_but_the_original_stands_down():
    mk = flat_hour_markets()
    loose = Harness("Buy", markets=mk, strategy_id="sweep_reversal_loose")
    await loose.engine.tick()
    rec = loose.state["active_position"]
    assert rec and rec["effective_r"] == 5.0
    original = Harness("Buy", markets=mk, strategy_id="sweep_reversal")
    await original.engine.tick()
    assert original.state["active_position"] is None and "stand down" in original.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_loose_sweep_stands_down_when_the_15m_is_a_range():
    mk = default_markets()
    mk["15"] = rows(flat_candles(SWEEP_END_MS, 60), SWEEP_END_MS, 102000.0)
    h = Harness("Buy", markets=mk, strategy_id="sweep_reversal_loose")
    await h.engine.tick()
    assert h.state["active_position"] is None and "15m range" in h.engine.status["last_decision"]


def test_unknown_strategy_is_refused():
    from src.engine.demo_engine import DemoEngine
    with pytest.raises(ValueError):
        Harness("Buy", strategy_id="nope")
