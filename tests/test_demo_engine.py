"""
End-to-end tests of the demo engine against a simulated Bybit, for BOTH directions
(the short runs are the exact mirror of the long ones): sweep -> trigger -> entry with the
stop on the exchange -> take-profits -> result recorded, plus restarts, outages, rejections,
the trend table and the kill switch.
"""
import json

import pytest

from src.engine.demo_engine import DemoEngine
from tests.sim_exchange import MIN, SWEEP_END_MS, Clock, SimExchange, default_markets, mirror, zigzag

K = 204000.0  # mirror constant in sim_exchange.mirror


class Harness:
    def __init__(self, side="Buy", markets=None, balance=1000.0, mirror_markets=None):
        self.side = side
        self.clock = Clock()
        markets = markets or default_markets()
        flip = (side == "Sell") if mirror_markets is None else mirror_markets
        self.sim = SimExchange(balance=balance, markets=mirror(markets) if flip else markets, clock=self.clock)
        self.sim.price = (K - 102300.0) if side == "Sell" else 102300.0
        self.state = {"active_position": None}
        self.persisted = 0
        self.trades, self.alerts = [], []
        self.engine = self.make_engine()

    def make_engine(self):
        async def persist():
            self.persisted += 1

        async def record(symbol, side, qty, entry, exit_, pnl, r, clean=True):
            self.trades.append(dict(symbol=symbol, side=side, qty=qty, entry=entry, exit=exit_, pnl=pnl, r=r, clean=clean))
            return self.trades[-1]

        async def notify(title, body, level):
            self.alerts.append(title)

        return DemoEngine(self.sim, self.state, persist, record, notify, now_fn=self.clock.now, sleep=self.clock.sleep)

    def restart(self):
        """A restart: state goes through JSON (as the saved file does) into a brand-new engine."""
        self.state = json.loads(json.dumps(self.state))
        self.engine = self.make_engine()


@pytest.fixture(params=["Buy", "Sell"], ids=["long", "short"])
def side(request):
    return request.param


def sign(rec):
    return 1 if rec["side"] == "Buy" else -1


def risk_distance(rec):
    return abs(rec["entry_price"] - rec["initial_stop_price"])


def at_r(rec, r):
    """Price that is r multiples of the risk in profit (negative r = against the trade)."""
    return rec["entry_price"] + sign(rec) * r * risk_distance(rec)


def past(rec, price, points):
    """`price` pushed `points` further in the profit direction."""
    return price + sign(rec) * points


async def entered(side="Buy", **kw):
    h = Harness(side, **kw)
    await h.engine.tick()
    assert h.state["active_position"], h.engine.status
    return h


@pytest.mark.asyncio
async def test_sweep_and_trigger_open_a_protected_trade(side):
    h = await entered(side)
    rec, sim = h.state["active_position"], h.sim
    assert rec["side"] == side and sim.position["side"] == side
    assert sim.leverage == 50                       # equity above $50 -> 50x ceiling
    expected_stop = 100685.0 if side == "Buy" else K - 100700.0 + 15.0   # sweep extreme -/+ 15 buffer
    assert sim.position["stop"] == rec["stop_price"] == expected_stop    # put on Bybit with the order
    assert rec["qty"] == sim.position["size"] > 0
    assert rec["effective_r"] == 10.0               # $1000 capital, trends aligned: $5 + $1 per $100 above $500
    opening = h.sim.placed[0]
    assert opening["side"] == side and opening["stop_loss"] == expected_stop
    # TP1 and TP2 rest on Bybit as reduce-only orders on the opposite side, covering the whole size
    tps = h.sim.placed[1:]
    assert all(o["reduce_only"] and o["side"] != side for o in tps)
    assert round(sum(o["qty"] for o in tps), 8) == rec["qty"]
    assert {o["price"] for o in sim.orders} == {rec["tp1_price"], rec["tp2_price"]}
    assert sign(rec) * (rec["tp1_price"] - rec["entry_price"]) > 0 and rec["net_rr"] >= 2.0
    for price in (rec["tp1_price"], rec["tp2_price"], rec["be_price"], rec["stop_price"]):
        assert round(price * 10) == pytest.approx(price * 10, abs=1e-6)   # on Bybit's 0.1 tick grid
    assert h.persisted >= 2 and "TRADE_OPENED" in h.alerts


@pytest.mark.asyncio
async def test_no_second_entry_while_a_trade_is_open(side):
    h = await entered(side)
    before = len(h.sim.placed)
    for _ in range(3):
        await h.engine.tick()
    assert len(h.sim.placed) == before


@pytest.mark.asyncio
async def test_full_win_is_recorded_once_and_counts_as_clean(side):
    h = await entered(side)
    rec = h.state["active_position"]
    h.sim.move_price(past(rec, rec["tp1_price"], 1))   # TP1 fills on Bybit
    await h.engine.tick()
    assert h.state["active_position"] is not None      # TP2 still open
    h.sim.move_price(past(rec, rec["tp2_price"], 1))
    await h.engine.tick()
    assert h.state["active_position"] is None
    assert len(h.trades) == 1
    t = h.trades[0]
    assert t["clean"] is True and t["pnl"] > 0 and t["r"] > 2.0 and t["side"] == side
    assert h.sim.orders == []
    await h.engine.tick()                              # flat again: no duplicate record
    assert len(h.trades) == 1


@pytest.mark.asyncio
async def test_stop_out_is_a_recorded_loss(side):
    h = await entered(side)
    rec = h.state["active_position"]
    h.sim.move_price(past(rec, rec["stop_price"], -5))  # 5 points through the stop, against the trade
    await h.engine.tick()
    assert h.state["active_position"] is None
    t = h.trades[0]
    assert t["pnl"] < 0 and t["clean"] is True          # a stop-out is a normal, clean result
    assert -1.6 < t["r"] < -0.5


@pytest.mark.asyncio
async def test_breakeven_stop_moves_on_bybit_at_one_and_a_half_r(side):
    h = await entered(side)
    rec = h.state["active_position"]
    h.sim.move_price(at_r(rec, 1.1))
    await h.engine.tick()                               # arms the early exit at 1R
    h.sim.move_price(at_r(rec, 1.6))
    await h.engine.tick()
    assert h.sim.position["stop"] == rec["be_price"]
    assert sign(rec) * (rec["be_price"] - rec["entry_price"]) > 0   # on the profit side of entry
    assert rec["breakeven_active"] is True
    h.sim.move_price(past(rec, rec["be_price"], -1))    # falls back: the new stop is hit
    await h.engine.tick()
    assert h.state["active_position"] is None and h.trades[0]["pnl"] > 0


@pytest.mark.asyncio
async def test_early_exit_closes_at_market_after_a_50_percent_giveback(side):
    h = await entered(side)
    rec = h.state["active_position"]
    h.sim.move_price(at_r(rec, 1.2))
    await h.engine.tick()                               # armed
    h.sim.move_price(at_r(rec, 0.5))                    # gave back more than half
    await h.engine.tick()                               # market close sent (opposite side, reduce-only)
    assert h.sim.position is None
    closing = h.sim.placed[-1]
    assert closing["order_type"] == "Market" and closing["reduce_only"] and closing["side"] != side
    await h.engine.tick()                               # result recorded
    assert len(h.trades) == 1 and h.state["active_position"] is None
    assert h.sim.orders == []


@pytest.mark.asyncio
async def test_restart_in_a_trade_picks_it_up_and_does_not_enter_again(side):
    h = await entered(side)
    rec = h.state["active_position"]
    placed = len(h.sim.placed)
    h.restart()
    await h.engine.tick()
    assert h.state["active_position"]["entry_price"] == rec["entry_price"]
    assert len(h.sim.placed) == placed
    h.sim.move_price(past(rec, rec["tp2_price"], 1))
    await h.engine.tick()
    assert len(h.trades) == 1 and h.state["active_position"] is None


@pytest.mark.asyncio
async def test_trade_that_closed_on_bybit_while_the_app_was_off_is_recorded_after_restart(side):
    h = await entered(side)
    rec = h.state["active_position"]
    h.sim.move_price(past(rec, rec["stop_price"], -10))   # the stop fires on Bybit; the app is down
    h.restart()
    await h.engine.tick()
    assert h.state["active_position"] is None and len(h.trades) == 1 and h.trades[0]["pnl"] < 0


@pytest.mark.asyncio
async def test_waits_when_bybit_has_not_published_the_result_yet():
    h = await entered()
    h.sim.hide_closed_pnl = 2
    h.sim.move_price(h.state["active_position"]["stop_price"] - 10)
    await h.engine.tick()
    assert h.state["active_position"] is not None and h.trades == []
    await h.engine.tick()
    await h.engine.tick()
    assert h.state["active_position"] is None and len(h.trades) == 1 and h.trades[0]["clean"] is True


@pytest.mark.asyncio
async def test_never_published_result_is_recorded_as_not_clean():
    h = await entered()
    h.sim.hide_closed_pnl = 99
    h.sim.move_price(h.state["active_position"]["stop_price"] - 10)
    for _ in range(12):
        await h.engine.tick()
    assert len(h.trades) == 1 and h.trades[0]["clean"] is False and "RESULT_UNKNOWN" in h.alerts


@pytest.mark.asyncio
async def test_range_trend_stands_down_and_does_not_trade(side):
    mk = default_markets()
    flat = zigzag(0, 3_600_000, [100, 110, 100, 110, 100, 110, 100, 110, 100, 110], per_leg=4)
    mk["60"] = [[str(int(c[0])), str(c[1]), str(c[2]), str(c[3]), str(c[4]), "1", "1"] for c in flat][::-1]
    h = Harness(side, markets=mk)
    await h.engine.tick()
    assert h.state["active_position"] is None and h.sim.placed == []
    assert "stand down" in h.engine.status["last_decision"]
    assert h.state["engine_state"]["setup"] is None
    await h.engine.tick()                              # the same candle is not evaluated twice
    assert h.sim.placed == []


@pytest.mark.asyncio
async def test_counter_trend_long_takes_half_risk():
    # 1h Down, 15m Up -> "Long Only (Counter-trend)" at half risk
    mk = default_markets()
    down = zigzag(0, 3_600_000, [102000, 100500, 101200, 99500, 100200, 98500, 99200, 97500], per_leg=4)
    mk["60"] = [[str(int(c[0])), str(c[1]), str(c[2]), str(c[3]), str(c[4]), "1", "1"] for c in down][::-1] + [mk["60"][0]]
    mk["60"] = sorted(mk["60"], key=lambda r: -int(r[0]))
    h = Harness("Buy", markets=mk)
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["side"] == "Buy" and rec["effective_r"] == 5.0 and "1h Down / 15m Up" in rec["trend"]


@pytest.mark.asyncio
async def test_counter_trend_short_takes_half_risk():
    # 1h Up, 15m Down -> "Short Only (Counter-trend)" at half risk: keep the 1h market un-mirrored (Up)
    h = Harness("Sell", mirror_markets=False)
    h.sim.markets = {**mirror(default_markets()), "60": default_markets()["60"]}
    h.sim.price = K - 102300.0
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec and rec["side"] == "Sell" and rec["effective_r"] == 5.0 and "1h Up / 15m Down" in rec["trend"]


@pytest.mark.asyncio
async def test_old_saved_setup_without_a_side_is_treated_as_long():
    h = Harness("Buy")
    await h.engine.tick()                              # opens the trade normally; use a second harness for the old format
    fresh = Harness("Buy")
    fresh.state["engine_state"] = {
        "day": "2026-10-18", "day_start_capital": 1000.0,
        "last_15m_eval_ts": SWEEP_END_MS - 15 * MIN,   # the sweep candle was already evaluated
        "setup": {"sweep_low": 100700.0, "swing_low": 101200.0, "sweep_end_ms": SWEEP_END_MS, "expires_ms": SWEEP_END_MS + 3_600_000},
    }
    fresh.engine = fresh.make_engine()
    await fresh.engine.tick()
    assert fresh.state["active_position"]["side"] == "Buy"


@pytest.mark.asyncio
async def test_net_rr_below_the_gate_is_rejected(side):
    # shallow sweep and a short climb: the stop is only ~225 away, so after costs RR < 2
    mk = default_markets(sweep_low=100990.0, climb_start=101000.0, climb_step=20.0)
    h = Harness(side, markets=mk)
    h.sim.price = 101200.0 if side == "Buy" else K - 101200.0
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["active_position"] is None
    assert "NET_RR_BELOW_GATE" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_a_small_account_that_cannot_size_the_trade_does_not_trade(side):
    h = Harness(side, balance=60.0)   # $60 -> $1.5 risk: below the minimum lot / notional
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["active_position"] is None


@pytest.mark.asyncio
async def test_below_minimum_equity_never_trades():
    h = Harness(balance=8.0)
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["active_position"] is None
    assert "CAPITAL_BELOW_MINIMUM_EQUITY" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_foreign_position_on_bybit_blocks_trading(side):
    h = Harness(side)
    h.sim.position = {"side": "Buy", "size": 0.01, "avg": 100000.0, "stop": None}
    await h.engine.tick()
    assert h.sim.placed == [] and "did not open" in h.engine.status["text"]


@pytest.mark.asyncio
async def test_entry_rejected_by_bybit_leaves_nothing_open(side):
    h = Harness(side)
    h.sim.reject_market = True
    await h.engine.tick()
    assert h.state["active_position"] is None and "ENTRY_REJECTED" in h.alerts


@pytest.mark.asyncio
async def test_missing_stop_is_repaired_then_trade_continues(side):
    h = Harness(side)
    h.sim.ignore_stop_on_order = True
    await h.engine.tick()
    assert h.state["active_position"] and h.sim.position["stop"] == h.state["active_position"]["stop_price"]


@pytest.mark.asyncio
async def test_rejected_take_profit_closes_the_trade_and_it_is_not_clean(side):
    h = Harness(side)
    h.sim.reject_limits = True
    await h.engine.tick()
    assert h.sim.position is None
    await h.engine.tick()
    assert len(h.trades) == 1 and h.trades[0]["clean"] is False and "ENTRY_ABORTED" in h.alerts


def _flip(row):
    o, hi, lo, cl = (float(x) for x in row[1:5])
    return [row[0], str(cl), str(hi), str(lo), str(o), row[5], row[6]]


@pytest.mark.asyncio
async def test_price_breaking_the_sweep_extreme_cancels_the_setup(side):
    mk = default_markets()
    target = str(SWEEP_END_MS + 3 * MIN)
    i = next(i for i, r in enumerate(mk["1"]) if r[0] == target)
    mk["1"][i] = [target, "101000", "101050", "100600", "101040", "1", "1"]   # trades under 100700 (long coordinates)
    h = Harness(side, markets=mk)
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["engine_state"]["setup"] is None
    assert "invalidated" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_setup_expires_when_no_trigger_comes(side):
    mk = default_markets()
    mk["1"] = [_flip(r) if int(r[0]) >= SWEEP_END_MS else r for r in mk["1"]]   # no candles in the trade direction after the sweep
    h = Harness(side, markets=mk)
    await h.engine.tick()
    assert h.state["engine_state"]["setup"] is not None and h.sim.placed == []
    h.clock.advance(3 * 3600)
    await h.engine.tick()
    assert h.state["engine_state"]["setup"] is None and "expired" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_outage_is_retried_and_then_recovers(side):
    h = Harness(side)
    h.sim.fail = True
    naps = []

    async def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            h.sim.fail = False
        if len(naps) >= 6:
            h.engine.request_stop()

    h.engine._sleep = sleep
    await h.engine.run()
    assert naps[1] > naps[0]                         # backs off while it fails
    assert h.engine.consecutive_errors == 0 and h.state["active_position"]  # recovered and traded


@pytest.mark.asyncio
async def test_repeated_errors_send_one_alert():
    h = Harness()
    h.sim.fail = True
    count = 0

    async def sleep(_):
        nonlocal count
        count += 1
        if count >= 9:
            h.engine.request_stop()

    h.engine._sleep = sleep
    await h.engine.run()
    assert h.alerts.count("ENGINE_ERRORS") == 1


@pytest.mark.asyncio
async def test_kill_switch_closes_the_trade_and_marks_it_not_clean(side):
    h = await entered(side)
    msg = await h.engine.emergency_close()
    assert "closed" in msg and h.sim.position is None and h.sim.orders == []
    assert h.sim.placed[-1]["side"] != side and h.sim.placed[-1]["reduce_only"]
    assert h.state["active_position"] is None
    assert len(h.trades) == 1 and h.trades[0]["clean"] is False


@pytest.mark.asyncio
async def test_kill_switch_with_bybit_unreachable_keeps_the_record():
    h = await entered()
    h.sim.fail = True
    msg = await h.engine.emergency_close()
    assert "Could not reach Bybit" in msg and h.state["active_position"] is not None


@pytest.mark.asyncio
async def test_a_15m_candle_is_evaluated_only_once():
    h = Harness()
    h.sim.reject_market = True
    await h.engine.tick()
    placed = len(h.sim.placed)
    await h.engine.tick()
    await h.engine.tick()
    assert len(h.sim.placed) == placed
    assert h.state["engine_state"]["last_15m_eval_ts"] > 0


@pytest.mark.asyncio
async def test_no_second_close_order_while_a_market_close_is_pending(side):
    h = await entered(side)
    rec = h.state["active_position"]
    rec["close_reason"] = "EARLY_EXIT"               # a close was already sent but has not filled yet
    rec["early_armed"], rec["highest_r"] = True, 1.2
    h.sim.move_price(at_r(rec, 0.3))                 # would trigger the early exit again
    sent = len(h.sim.placed)
    await h.engine.tick()
    assert len(h.sim.placed) == sent and h.sim.position is not None


@pytest.mark.asyncio
async def test_a_trade_is_never_recorded_twice_when_two_paths_finish_it():
    h = await entered()
    rec = h.state["active_position"]
    h.sim.move_price(rec["stop_price"] - 10)
    await h.engine.emergency_close()                 # kill switch path records it...
    await h.engine._finalize(rec)                    # ...and the normal path must not record it again
    assert len(h.trades) == 1


@pytest.mark.asyncio
async def test_take_profit_and_breakeven_prices_stay_on_the_tick_grid_when_the_fill_is_off_grid(side):
    h = Harness(side)
    h.sim.price = 102333.37 if side == "Buy" else K - 102333.37   # a fill price with 2 decimals
    await h.engine.tick()
    rec = h.state["active_position"]
    assert rec
    for key in ("tp1_price", "tp2_price", "be_price"):
        assert abs(round(rec[key] * 10) - rec[key] * 10) < 1e-6, (key, rec[key])
    for o in h.sim.orders:
        assert abs(round(o["price"] * 10) - o["price"] * 10) < 1e-6


def _bearish(row):
    o, hi, lo, cl = (float(x) for x in row[1:5])
    new_close = o - 30
    return [row[0], str(cl), str(hi), str(min(lo, new_close)), str(new_close), row[5], row[6]]


@pytest.mark.asyncio
async def test_trigger_is_skipped_when_the_trend_table_no_longer_allows_that_side():
    h = Harness("Buy")   # market: 15m Up, so only longs are allowed
    h.sim.markets["1"] = [_bearish(r) if int(r[0]) >= SWEEP_END_MS else r for r in h.sim.markets["1"]]  # bearish candles
    h.state["engine_state"] = {
        "day": "2026-10-18", "day_start_capital": 1000.0, "last_15m_eval_ts": SWEEP_END_MS - 15 * MIN,
        "setup": {"side": "Sell", "sweep_extreme": 102500.0, "swing_level": 102400.0,
                  "sweep_end_ms": SWEEP_END_MS, "expires_ms": SWEEP_END_MS + 3_600_000},
    }
    h.engine = h.make_engine()
    await h.engine.tick()
    assert h.sim.placed == [] and "not short" in h.engine.status["last_decision"]
