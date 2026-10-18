"""
End-to-end tests of the demo engine against a simulated Bybit: sweep -> trigger -> entry
with the stop on the exchange -> take-profits -> result recorded, plus restarts, outages,
rejections and the kill switch.
"""
import copy
import json

import pytest

from src.engine.demo_engine import DemoEngine
from tests.sim_exchange import MIN, SWEEP_END_MS, Clock, SimExchange, default_markets, zigzag


class Harness:
    def __init__(self, sim=None, state=None):
        self.clock = Clock()
        self.sim = sim or SimExchange(clock=self.clock)
        self.sim.clock = self.clock
        self.state = state if state is not None else {"active_position": None}
        self.persisted = 0
        self.trades = []
        self.alerts = []
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


async def entered(h=None):
    h = h or Harness()
    await h.engine.tick()
    assert h.state["active_position"], h.engine.status
    return h


@pytest.mark.asyncio
async def test_sweep_and_trigger_open_a_protected_trade():
    h = await entered()
    rec, sim = h.state["active_position"], h.sim
    assert sim.leverage == 50                       # equity above $50 -> 50x ceiling
    assert sim.position["stop"] == rec["stop_price"] == 100685.0  # sweep low 100700 - 15 buffer, put on Bybit with the order
    assert rec["qty"] == sim.position["size"] > 0
    assert rec["effective_r"] == 10.0               # $1000 capital: $5 + $1 per $100 above $500
    # TP1 and TP2 rest on Bybit as reduce-only sells for the whole size
    assert [(o["qty"], o["price"], o["reduce_only"]) for o in sim.orders][0][2] is True
    assert round(sum(o["qty"] for o in sim.orders), 8) == rec["qty"]
    assert {o["price"] for o in sim.orders} == {rec["tp1_price"], rec["tp2_price"]}
    assert rec["tp1_price"] > rec["entry_price"] and rec["net_rr"] >= 2.0
    assert h.persisted >= 2 and "TRADE_OPENED" in h.alerts


@pytest.mark.asyncio
async def test_no_second_entry_while_a_trade_is_open():
    h = await entered()
    before = len(h.sim.placed)
    for _ in range(3):
        await h.engine.tick()
    assert len(h.sim.placed) == before


@pytest.mark.asyncio
async def test_full_win_is_recorded_once_and_counts_as_clean():
    h = await entered()
    rec = h.state["active_position"]
    h.sim.move_price(rec["tp1_price"] + 1)          # TP1 fills on Bybit
    await h.engine.tick()
    assert h.state["active_position"] is not None   # TP2 still open
    h.sim.move_price(rec["tp2_price"] + 1)
    await h.engine.tick()
    assert h.state["active_position"] is None
    assert len(h.trades) == 1
    t = h.trades[0]
    assert t["clean"] is True and t["pnl"] > 0 and t["r"] > 2.0 and t["side"] == "Buy"
    assert h.sim.orders == []
    await h.engine.tick()                           # flat again: no duplicate record
    assert len(h.trades) == 1


@pytest.mark.asyncio
async def test_stop_out_is_a_recorded_loss():
    h = await entered()
    h.sim.move_price(h.state["active_position"]["stop_price"] - 5)
    await h.engine.tick()
    assert h.state["active_position"] is None
    t = h.trades[0]
    assert t["pnl"] < 0 and t["clean"] is True       # a stop-out is a normal, clean result
    assert -1.6 < t["r"] < -0.5


@pytest.mark.asyncio
async def test_breakeven_stop_moves_on_bybit_at_one_and_a_half_r():
    h = await entered()
    rec = h.state["active_position"]
    d = rec["entry_price"] - rec["initial_stop_price"]
    h.sim.move_price(rec["entry_price"] + 1.1 * d)
    await h.engine.tick()                           # arms the early exit at 1R
    h.sim.move_price(rec["entry_price"] + 1.6 * d)
    await h.engine.tick()
    assert h.sim.position["stop"] == rec["be_price"] > rec["entry_price"]
    assert rec["breakeven_active"] is True
    h.sim.move_price(rec["be_price"] - 1)           # falls back: the new stop is hit
    await h.engine.tick()
    assert h.state["active_position"] is None and h.trades[0]["pnl"] > 0


@pytest.mark.asyncio
async def test_early_exit_closes_at_market_after_a_50_percent_giveback():
    h = await entered()
    rec = h.state["active_position"]
    d = rec["entry_price"] - rec["initial_stop_price"]
    h.sim.move_price(rec["entry_price"] + 1.2 * d)
    await h.engine.tick()                           # armed
    h.sim.move_price(rec["entry_price"] + 0.5 * d)  # gave back more than half
    await h.engine.tick()                           # market close sent
    assert h.sim.position is None
    await h.engine.tick()                           # result recorded
    assert len(h.trades) == 1 and h.state["active_position"] is None
    assert h.sim.orders == []


@pytest.mark.asyncio
async def test_restart_in_a_trade_picks_it_up_and_does_not_enter_again():
    h = await entered()
    rec = h.state["active_position"]
    placed = len(h.sim.placed)
    h.restart()
    await h.engine.tick()
    assert h.state["active_position"]["entry_price"] == rec["entry_price"]
    assert len(h.sim.placed) == placed
    h.sim.move_price(rec["tp2_price"] + 1)
    await h.engine.tick()
    assert len(h.trades) == 1 and h.state["active_position"] is None


@pytest.mark.asyncio
async def test_trade_that_closed_on_bybit_while_the_app_was_off_is_recorded_after_restart():
    h = await entered()
    rec = h.state["active_position"]
    h.sim.move_price(rec["stop_price"] - 10)        # the stop fires on Bybit; the app is down
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
async def test_range_trend_stands_down_and_uses_up_the_trigger():
    mk = default_markets()
    flat = zigzag(0, 3_600_000, [100, 110, 100, 110, 100, 110, 100, 110, 100, 110], per_leg=4)
    mk["60"] = [[str(int(c[0])), str(c[1]), str(c[2]), str(c[3]), str(c[4]), "1", "1"] for c in flat][::-1]
    h = Harness(SimExchange(markets=mk))
    await h.engine.tick()
    assert h.state["active_position"] is None and h.sim.placed == []
    assert "TREND_RANGE_STAND_DOWN" in h.engine.status["last_decision"]
    assert h.state["engine_state"]["setup"] is None
    await h.engine.tick()                           # the same candle is not evaluated twice
    assert h.sim.placed == []


@pytest.mark.asyncio
async def test_net_rr_below_the_gate_is_rejected():
    # shallow sweep and a short climb: the stop is only ~225 away, so after costs RR < 2
    mk = default_markets(sweep_low=100990.0, climb_start=101000.0, climb_step=20.0)
    h = Harness(SimExchange(markets=mk))
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["active_position"] is None
    assert "NET_RR_BELOW_GATE" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_a_small_account_that_cannot_size_the_trade_does_not_trade():
    h = Harness(SimExchange(balance=60.0))   # $60 -> $1.5 risk: below the minimum lot / notional
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["active_position"] is None


@pytest.mark.asyncio
async def test_below_minimum_equity_never_trades():
    h = Harness(SimExchange(balance=8.0))
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["active_position"] is None
    assert "CAPITAL_BELOW_MINIMUM_EQUITY" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_foreign_position_on_bybit_blocks_trading():
    h = Harness()
    h.sim.position = {"size": 0.01, "avg": 100000.0, "stop": None}
    await h.engine.tick()
    assert h.sim.placed == [] and "did not open" in h.engine.status["text"]


@pytest.mark.asyncio
async def test_entry_rejected_by_bybit_leaves_nothing_open():
    h = Harness()
    h.sim.reject_market = True
    await h.engine.tick()
    assert h.state["active_position"] is None and "ENTRY_REJECTED" in h.alerts


@pytest.mark.asyncio
async def test_missing_stop_is_repaired_then_trade_continues():
    h = Harness()
    h.sim.ignore_stop_on_order = True
    await h.engine.tick()
    assert h.state["active_position"] and h.sim.position["stop"] == 100685.0


@pytest.mark.asyncio
async def test_rejected_take_profit_closes_the_trade_and_it_is_not_clean():
    h = Harness()
    h.sim.reject_limits = True
    await h.engine.tick()
    assert h.sim.position is None
    await h.engine.tick()
    assert len(h.trades) == 1 and h.trades[0]["clean"] is False and "ENTRY_ABORTED" in h.alerts


def _flip(row):
    o, hi, lo, cl = (float(x) for x in row[1:5])
    return [row[0], str(cl), str(hi), str(lo), str(o), row[5], row[6]]


@pytest.mark.asyncio
async def test_price_breaking_the_sweep_low_cancels_the_setup():
    mk = default_markets()
    target = str(SWEEP_END_MS + 3 * MIN)
    i = next(i for i, r in enumerate(mk["1"]) if r[0] == target)
    mk["1"][i] = [target, "101000", "101050", "100600", "101040", "1", "1"]   # trades under 100700
    h = Harness(SimExchange(markets=mk))
    await h.engine.tick()
    assert h.sim.placed == [] and h.state["engine_state"]["setup"] is None
    assert "invalidated" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_setup_expires_when_no_trigger_comes():
    mk = default_markets()
    mk["1"] = [_flip(r) if int(r[0]) >= SWEEP_END_MS else r for r in mk["1"]]   # no bullish candles after the sweep
    h = Harness(SimExchange(markets=mk))
    await h.engine.tick()
    assert h.state["engine_state"]["setup"] is not None and h.sim.placed == []
    h.clock.advance(3 * 3600)
    await h.engine.tick()
    assert h.state["engine_state"]["setup"] is None and "expired" in h.engine.status["last_decision"]


@pytest.mark.asyncio
async def test_outage_is_retried_and_then_recovers():
    h = Harness()
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
async def test_kill_switch_closes_the_trade_and_marks_it_not_clean():
    h = await entered()
    msg = await h.engine.emergency_close()
    assert "closed" in msg and h.sim.position is None and h.sim.orders == []
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
async def test_no_second_close_order_while_a_market_close_is_pending():
    h = await entered()
    rec = h.state["active_position"]
    d = rec["entry_price"] - rec["initial_stop_price"]
    rec["close_reason"] = "EARLY_EXIT"               # a close was already sent but has not filled yet
    rec["early_armed"], rec["highest_r"] = True, 1.2
    h.sim.move_price(rec["entry_price"] + 0.3 * d)   # would trigger the early exit again
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
