"""
Demo trading engine: watches Bybit candles, runs Strategy 1 and the risk gates, places
demo orders with the stop-loss and take-profit orders sitting on Bybit itself, manages
the open trade, and records each closed trade (which is what unlocks live trading).

Flow, one pass ("tick") every few seconds:
  open trade?  -> check it against Bybit, apply the breakeven / early-exit rules, and when
                  Bybit shows it flat, record the result.
  flat?        -> watch for a closed 15m candle that sweeps the last swing low (long) or swing
                  high (short) and closes back inside, then for three 2m candles in the trade
                  direction that never break the sweep extreme; then check trend / risk
                  schedule / sizing / net RR, and enter.

Both directions are traded, following the PRD's trend table: the 15m trend picks the side
(Up -> long after a swing-low sweep, Down -> short after a swing-high sweep, mirrored rules),
the 1h trend only decides full or half risk, and a Range on either timeframe stands down.
Everything the engine must remember across a restart (the open trade and its progress,
the evaluated candle, the armed setup, the day's starting capital) lives in the shared
state dict and is saved through `persist`, so a restart carries on where it stopped.
Bybit itself holds the stop and take-profits, so an open trade is protected even while
this app is off. On startup the first tick compares the saved trade with Bybit.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.core.daily_cap import get_trading_day_id
from src.core.exit_policy import ExitActionType, ExitPolicyMonitor, TradeExitPlan, build_trade_exit_plan
from src.core.models import BTCUSDT_CONFIG, OrderSide, PairConfig, TrendDirection
from src.core.net_rr_gate import calculate_net_rr
from src.core.position_sizing import calculate_position_size
from src.core.risk_schedule import evaluate_risk_schedule
from src.engine import market
from src.strategy.strategy1 import Strategy1StateMachine

logger = logging.getLogger("DemoEngine")

MARGIN_USE_LIMIT = 0.9          # refuse a trade that would need more than 90% of equity as margin
MAX_FINALIZE_ATTEMPTS = 8       # ticks to wait for Bybit's closed-PnL record
ALERT_AFTER_ERRORS = 5


def _ms(now: datetime) -> int:
    return int(now.timestamp() * 1000)


def _opposite(side: str) -> str:
    return OrderSide.SELL.value if side == OrderSide.BUY.value else OrderSide.BUY.value


class DemoEngine:
    def __init__(
        self,
        exchange: Any,
        state: Dict[str, Any],
        persist: Callable[[], Awaitable[Any]],
        record_trade: Callable[..., Awaitable[Any]],
        notify: Optional[Callable[[str, str, str], Awaitable[Any]]] = None,
        *,
        config: PairConfig = BTCUSDT_CONFIG,
        now_fn: Optional[Callable[[], datetime]] = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        poll_seconds: float = 15.0,
        position_poll_seconds: float = 5.0,
        setup_window_minutes: int = 60,
    ):
        self.exchange = exchange
        self.state = state
        self.persist = persist
        self.record_trade = record_trade
        self.notify = notify
        self.config = config
        self.symbol = config.symbol
        self.strategy = Strategy1StateMachine(symbol=config.symbol, stop_buffer=config.default_stop_buffer)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self.poll_seconds = poll_seconds
        self.position_poll_seconds = position_poll_seconds
        self.setup_window_ms = setup_window_minutes * 60_000
        self._stop = False
        self.consecutive_errors = 0
        self._error_alerted = False
        self.status: Dict[str, Any] = {
            "text": "Starting...", "last_scan_utc": None, "last_decision": None,
            "last_error": None, "balance": None, "trend": None,
        }
        self.state.setdefault("engine_state", {})
        es = self.state["engine_state"]
        es.setdefault("day", "")
        es.setdefault("day_start_capital", 0.0)
        es.setdefault("last_15m_eval_ts", 0)
        es.setdefault("setup", None)

    # ------------------------------------------------------------------ plumbing
    @property
    def es(self) -> Dict[str, Any]:
        return self.state["engine_state"]

    async def _alert(self, title: str, body: str, level: str = "INFO") -> None:
        if self.notify:
            try:
                await self.notify(title, body, level)
            except Exception as exc:  # an alert problem must never stop trading logic
                logger.warning("Alert failed: %s", exc)

    def _decide(self, text: str) -> None:
        self.status["last_decision"] = f"{self._now().strftime('%H:%M:%S UTC')} {text}"
        logger.info(text)

    def request_stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """Loop until asked to stop. Errors (network, Bybit) are logged and retried with
        a growing pause; they never end the loop."""
        logger.info("Demo engine started for %s", self.symbol)
        while not self._stop:
            delay = self.position_poll_seconds if self.state.get("active_position") else self.poll_seconds
            try:
                await self.tick()
                if self.consecutive_errors:
                    self.consecutive_errors = 0
                    self.status["last_error"] = None
                    if self._error_alerted:
                        self._error_alerted = False
                        await self._alert("ENGINE_RECOVERED", "Connection to Bybit is back; trading checks resumed.", "INFO")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.consecutive_errors += 1
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                self.status["text"] = f"Cannot reach Bybit or it refused a request; retrying ({self.status['last_error']})"
                logger.warning("Engine tick failed (%d in a row): %s", self.consecutive_errors, exc)
                delay = min(self.poll_seconds * (2 ** min(self.consecutive_errors, 4)), 120.0)
                if self.consecutive_errors >= ALERT_AFTER_ERRORS and not self._error_alerted:
                    self._error_alerted = True
                    await self._alert("ENGINE_ERRORS", f"{self.consecutive_errors} failures in a row: {self.status['last_error']}", "WARNING")
            await self._sleep(delay)
        logger.info("Demo engine stopped")

    async def tick(self) -> None:
        now = self._now()
        self.status["last_scan_utc"] = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        record = self.state.get("active_position")
        if record:
            await self._manage_position(record, now)
        else:
            await self._scan(now)

    # ------------------------------------------------------------------ scanning
    async def _scan(self, now: datetime) -> None:
        equity = await self.exchange.get_balance()
        self.status["balance"] = equity

        if await self.exchange.get_position(self.symbol):
            self.status["text"] = "A position exists on Bybit that this bot did not open; not trading until it is closed."
            return

        day = get_trading_day_id(now)
        if self.es["day"] != day or not self.es["day_start_capital"]:
            self.es["day"], self.es["day_start_capital"] = day, equity
            await self.persist()

        c15 = market.drop_forming(market.parse_candles(await self.exchange.get_candles(self.symbol, "15", 100)))
        if len(c15) < 20:
            raise RuntimeError(f"only {len(c15)} closed 15m candles available")
        latest = c15[-1]

        if latest.timestamp > self.es["last_15m_eval_ts"]:
            await self._evaluate_new_15m_candle(c15, latest)

        setup = self.es.get("setup")
        if not setup:
            self.status["text"] = "Watching 15m candles for a liquidity sweep of the last swing low."
            return

        now_ms = _ms(now)
        if now_ms > setup["expires_ms"]:
            self.es["setup"] = None
            self._decide("setup expired: no 2m trigger within the window")
            await self.persist()
            return

        side = setup.get("side", OrderSide.BUY.value)  # setups saved by v1.7 had no side: they were long
        extreme = setup.get("sweep_extreme", setup.get("sweep_low"))
        c2 = market.aggregate_2m(market.drop_forming(market.parse_candles(await self.exchange.get_candles(self.symbol, "1", 200))))
        after = [c for c in c2 if c.timestamp >= setup["sweep_end_ms"]]
        if side == OrderSide.BUY.value:
            triggered, price, why = self.strategy.check_2m_micro_trigger(after, extreme)
        else:
            triggered, price, why = self.strategy.check_2m_micro_trigger_short(after, extreme)
        if why.startswith("INVALIDATED"):
            self.es["setup"] = None
            self._decide(f"setup invalidated: price broke the sweep {'low' if side == OrderSide.BUY.value else 'high'}")
            await self.persist()
            return
        if not triggered:
            word = "bullish" if side == OrderSide.BUY.value else "bearish"
            self.status["text"] = f"{'Long' if side == OrderSide.BUY.value else 'Short'} sweep detected; waiting for 3 {word} 2m candles ({why})."
            return
        await self._try_enter(price, setup, side, extreme, equity, c15, now)

    async def _evaluate_new_15m_candle(self, c15: list, latest: Any) -> None:
        """Look at a newly closed 15m candle once: pick the side from the trend table, then
        check whether it swept the last swing low (long) or swing high (short)."""
        self.es["last_15m_eval_ts"] = latest.timestamp
        c1h = market.drop_forming(market.parse_candles(await self.exchange.get_candles(self.symbol, "60", 100)))
        macro, micro = market.detect_trend(c1h), market.detect_trend(c15)
        self.status["trend"] = f"1h {macro.value} / 15m {micro.value}"
        side = market.authorized_direction(macro, micro)
        history = c15[:-1][-60:]
        if side is None:
            self.es["setup"] = None
            self._decide(f"15m candle closed: trend {self.status['trend']} -> stand down (range)")
        elif side == OrderSide.BUY.value:
            level = market.latest_unswept_swing_low(history)
            if level is None:
                self._decide("15m candle closed: no swing low to sweep")
            else:
                swept, why = self.strategy.evaluate_15m_sweep(level, latest)
                if swept:
                    await self._arm(side, level, latest, latest.low)
                else:
                    self._decide(f"15m candle closed: {why} (long side, swing low {level:.1f})")
        else:
            level = market.latest_unswept_swing_high(history)
            if level is None:
                self._decide("15m candle closed: no swing high to sweep")
            else:
                swept, why = self.strategy.evaluate_15m_sweep_short(level, latest)
                if swept:
                    await self._arm(side, level, latest, latest.high)
                else:
                    self._decide(f"15m candle closed: {why} (short side, swing high {level:.1f})")
        await self.persist()

    async def _arm(self, side: str, level: float, candle: Any, extreme: float) -> None:
        end_ms = candle.timestamp + 15 * 60_000
        self.es["setup"] = {
            "side": side, "sweep_extreme": extreme, "swing_level": level,
            "sweep_end_ms": end_ms, "expires_ms": end_ms + self.setup_window_ms,
        }
        word = "bullish" if side == OrderSide.BUY.value else "bearish"
        kind = "low" if side == OrderSide.BUY.value else "high"
        self._decide(f"15m sweep of swing {kind} {level:.1f} confirmed (extreme {extreme:.1f}); waiting for 3 {word} 2m candles [{self.status['trend']}]")
        await self._alert(
            "SWEEP_DETECTED",
            f"{self.symbol}: {'long' if side == OrderSide.BUY.value else 'short'} setup. 15m sweep of {level:.1f}, extreme {extreme:.1f}. Waiting for the 2m trigger.",
            "INFO",
        )

    # ------------------------------------------------------------------ entry
    async def _try_enter(self, trigger_price: float, setup: Dict[str, Any], side: str, extreme: float, equity: float, c15: list, now: datetime) -> None:
        self.es["setup"] = None  # a trigger is used up whether or not the trade passes the gates
        await self.persist()
        is_long = side == OrderSide.BUY.value
        word = "long" if is_long else "short"

        c1h = market.drop_forming(market.parse_candles(await self.exchange.get_candles(self.symbol, "60", 100)))
        macro, micro = market.detect_trend(c1h), market.detect_trend(c15)
        self.status["trend"] = f"1h {macro.value} / 15m {micro.value}"

        schedule = evaluate_risk_schedule(self.es["day_start_capital"], equity, macro, micro)
        if not schedule.is_trading_enabled:
            self._decide(f"trigger skipped: {schedule.rejection_reason} ({self.status['trend']})")
            return
        allowed = market.authorized_direction(macro, micro)
        if allowed != side:
            self._decide(f"trigger skipped: the trend table now allows {'long' if allowed == 'Buy' else 'short' if allowed else 'nothing'}, not {word} ({self.status['trend']})")
            return

        entry = trigger_price
        buffer = self.strategy.stop_buffer
        stop = market.round_to_tick(extreme - buffer if is_long else extreme + buffer, self.config.tick_size)
        if (is_long and stop >= entry) or (not is_long and stop <= entry):
            self._decide(f"trigger skipped: stop is on the wrong side of entry ({word})")
            return
        sizing = calculate_position_size(schedule.effective_r, entry, stop, self.config)
        if not sizing.is_valid:
            self._decide(f"trigger skipped: {sizing.rejection_reason}")
            return
        # calculate_net_rr is written for a long (its exit fee is on entry + 3d); for a short the
        # exit fee is on entry - 3d, a hair smaller, so the gate is very slightly conservative.
        rr = calculate_net_rr(sizing.nominal_distance_d, sizing.loss_per_unit_l, entry)
        if not rr.passes_gate:
            self._decide(f"trigger skipped: {rr.rejection_reason}")
            return
        leverage = min(schedule.leverage_ceiling, self.config.max_exchange_leverage)
        if sizing.notional / leverage > MARGIN_USE_LIMIT * equity:
            self._decide(f"trigger skipped: needs {sizing.notional / leverage:.2f} margin at {leverage}x, equity {equity:.2f}")
            return

        if not await self.exchange.set_leverage(self.symbol, leverage):
            self._decide("trigger skipped: Bybit refused the leverage change")
            return
        reply = await self.exchange.place_order({
            "symbol": self.symbol, "side": side, "qty": sizing.qty,
            "order_type": "Market", "stop_loss": stop,
        })
        if reply.get("retCode") != 0:
            self._decide(f"entry rejected by Bybit: {reply.get('retMsg') or reply}")
            await self._alert("ENTRY_REJECTED", f"{self.symbol} {word} {sizing.qty}: {reply.get('retMsg') or reply}", "WARNING")
            return

        live = None
        for _ in range(6):
            await self._sleep(1)
            live = await self.exchange.get_position(self.symbol)
            if live:
                break
        if not live:
            await self.exchange.cancel_all_orders(self.symbol)
            self._decide("entry order sent but no position appeared; cancelled")
            await self._alert("ENTRY_NOT_FILLED", f"{self.symbol}: no position after the market order.", "WARNING")
            return

        filled_entry, filled_qty = float(live["avgPrice"]), float(live["size"])
        order_side = OrderSide.BUY if is_long else OrderSide.SELL
        plan = build_trade_exit_plan(order_side, filled_entry, stop, filled_qty, self.config)
        tick = self.config.tick_size
        record = {
            "mode": "DEMO", "symbol": self.symbol, "side": side, "qty": filled_qty,
            "entry_price": filled_entry, "stop_price": stop, "initial_stop_price": stop,
            # Bybit rejects prices that are not on the tick grid, so the plan's 2-decimal prices are rounded to it
            "tp1_price": market.round_to_tick(plan.tp1_price, tick), "tp2_price": market.round_to_tick(plan.tp2_price, tick),
            "tp1_lots": plan.tp1_lots, "tp2_lots": plan.tp2_lots, "is_single_exit": plan.is_single_exit,
            "be_price": market.round_to_tick(plan.breakeven_price, tick), "breakeven_active": False, "early_armed": False,
            "highest_r": 0.0, "opened_ms": _ms(now) - 5000, "opened_utc": now.isoformat(),
            "effective_r": schedule.effective_r, "net_rr": rr.net_rr, "leverage": leverage,
            "trend": self.status["trend"], "unclean": False, "close_reason": None, "finalize_attempts": 0,
        }
        self.state["active_position"] = record
        await self.persist()  # saved BEFORE the take-profits go out, so a crash cannot lose track of the trade

        problem = None
        if (is_long and filled_entry <= stop) or (not is_long and filled_entry >= stop):
            problem = "price gapped through the stop"
        else:
            if not (live.get("stopLoss") and float(live["stopLoss"]) > 0):
                if not await self.exchange.set_position_stop(self.symbol, stop):
                    problem = "the stop-loss could not be set"
            if problem is None:
                problem = await self._place_take_profits(record)
        if problem:
            record["unclean"] = True
            self._decide(f"trade opened but {problem}; closing it for safety")
            await self._alert("ENTRY_ABORTED", f"{self.symbol}: {problem}. Closing the position.", "CRITICAL")
            await self._close_at_market(record, "SAFETY_CLOSE")
            return

        self._decide(f"ENTERED {word} {filled_qty} @ {filled_entry:.1f}, stop {stop:.1f}, TP1 {record['tp1_price']:.1f}, TP2 {record['tp2_price']:.1f}, net RR {rr.net_rr}")
        await self._alert(
            "TRADE_OPENED",
            f"{self.symbol} {word.upper()} {filled_qty} @ {filled_entry:.1f}\nStop {stop:.1f} | TP1 {record['tp1_price']:.1f} | TP2 {record['tp2_price']:.1f}\n"
            f"Risk ${schedule.effective_r:.2f} | net RR {rr.net_rr} | {leverage}x | {self.status['trend']}",
            "INFO",
        )

    async def _place_take_profits(self, record: Dict[str, Any]) -> Optional[str]:
        """Rest the take-profit orders on Bybit. Returns a problem description, or None."""
        legs = [(record["tp1_lots"], record["tp1_price"], "TP1")]
        if not record["is_single_exit"]:
            legs.append((record["tp2_lots"], record["tp2_price"], "TP2"))
        for qty, price, name in legs:
            reply = await self.exchange.place_order({
                "symbol": self.symbol, "side": _opposite(record["side"]), "qty": qty,
                "order_type": "Limit", "price": price, "reduce_only": True,
            })
            if reply.get("retCode") != 0:
                return f"the {name} order was rejected ({reply.get('retMsg') or reply})"
        return None

    # ------------------------------------------------------------------ open trade
    def _monitor_for(self, record: Dict[str, Any]) -> ExitPolicyMonitor:
        plan = TradeExitPlan(
            tp1_lots=record["tp1_lots"], tp2_lots=record["tp2_lots"], is_single_exit=record["is_single_exit"],
            tp1_price=record["tp1_price"], tp2_price=record["tp2_price"],
            initial_stop_price=record["initial_stop_price"], breakeven_price=record["be_price"],
        )
        monitor = ExitPolicyMonitor(OrderSide(record["side"]), record["entry_price"], record["initial_stop_price"], plan)
        monitor.highest_r = record["highest_r"]
        monitor.is_early_exit_armed = record["early_armed"]
        monitor.is_breakeven_active = record["breakeven_active"]
        return monitor

    async def _manage_position(self, record: Dict[str, Any], now: datetime) -> None:
        live = await self.exchange.get_position(self.symbol)  # raises if Bybit is unreachable
        if live is None:
            await self._finalize(record)
            return

        self.status["text"] = f"In a trade: {'long' if record['side'] == 'Buy' else 'short'} {live.get('size')} @ {record['entry_price']:.1f}."
        if record.get("close_reason"):
            return  # a market close is already on its way
        price = float(live.get("markPrice") or 0.0)
        if price <= 0:
            c1 = market.parse_candles(await self.exchange.get_candles(self.symbol, "1", 2))
            price = c1[-1].close
        monitor = self._monitor_for(record)
        action, level, why = monitor.on_price_update(price)

        changed = (monitor.highest_r != record["highest_r"] or monitor.is_early_exit_armed != record["early_armed"])
        record["highest_r"], record["early_armed"] = monitor.highest_r, monitor.is_early_exit_armed

        if action == ExitActionType.MOVE_STOP_BREAKEVEN:
            if await self.exchange.set_position_stop(self.symbol, level):
                record["breakeven_active"], record["stop_price"], changed = True, level, True
                self._decide(f"stop moved to breakeven {level:.1f} ({why})")
                await self._alert("BREAKEVEN", f"{self.symbol}: stop moved to {level:.1f}.", "INFO")
        elif action == ExitActionType.CLOSE_MARKET:
            self._decide(f"early exit: {why}")
            await self._close_at_market(record, why)
            return
        if changed:
            await self.persist()

    async def _close_at_market(self, record: Dict[str, Any], reason: str) -> None:
        """Cancel the resting orders and close what is left at market; the next tick sees the
        position flat and records the result."""
        try:
            await self.exchange.cancel_all_orders(self.symbol)
        except Exception as exc:
            logger.warning("cancel_all_orders failed: %s", exc)
        live = await self.exchange.get_position(self.symbol)
        if live:
            reply = await self.exchange.place_order({
                "symbol": self.symbol, "side": _opposite(record["side"]), "qty": float(live["size"]),
                "order_type": "Market", "reduce_only": True,
            })
            if reply.get("retCode") != 0:
                logger.error("Market close rejected: %s", reply)
                await self._alert("CLOSE_FAILED", f"{self.symbol}: market close rejected ({reply.get('retMsg') or reply}). The stop on Bybit still protects the trade.", "CRITICAL")
                return
        record["close_reason"] = reason
        await self.persist()

    async def emergency_close(self) -> str:
        """Kill switch: cancel orders and close the trade at market. Returns a one-line result."""
        record = self.state.get("active_position")
        try:
            await self.exchange.cancel_all_orders(self.symbol)
            live = await self.exchange.get_position(self.symbol)
        except Exception as exc:
            return f"Could not reach Bybit to check or close a position ({exc}). If a trade is open, its stop and take-profit orders on Bybit are still in place."
        if not live:
            if record:
                record["unclean"] = True
                await self._finalize(record, force_unclean=True)
            return "No open position on Bybit; open orders cancelled."
        if record:
            record["unclean"] = True
        reply = await self.exchange.place_order({
            "symbol": self.symbol, "side": _opposite(live.get("side") or (record or {}).get("side") or OrderSide.BUY.value),
            "qty": float(live["size"]), "order_type": "Market", "reduce_only": True,
        })
        if reply.get("retCode") != 0:
            return f"Bybit refused the close order ({reply.get('retMsg') or reply}). The stop on Bybit is still in place."
        for _ in range(8):
            await self._sleep(1)
            if not await self.exchange.get_position(self.symbol):
                break
        if record:
            await self._finalize(record, force_unclean=True)
        return "Position closed at market and orders cancelled."

    async def _finalize(self, record: Dict[str, Any], force_unclean: bool = False) -> None:
        """Bybit shows the position flat: cancel leftovers, read the realised result, record it."""
        if self.state.get("active_position") is not record:
            return  # already recorded (another path finished this trade first)
        try:
            await self.exchange.cancel_all_orders(self.symbol)
        except Exception as exc:
            logger.warning("cancel_all_orders failed: %s", exc)

        rows = [
            r for r in await self.exchange.get_closed_pnl(self.symbol, start_ms=record["opened_ms"])
            if int(r.get("updatedTime") or 0) >= record["opened_ms"]
        ]
        if not rows and record["finalize_attempts"] < MAX_FINALIZE_ATTEMPTS:
            record["finalize_attempts"] += 1  # Bybit can take a few seconds to publish the result
            self.status["text"] = "Trade closed on Bybit; waiting for the result."
            return

        if self.state.get("active_position") is not record:
            return
        qty = float(record["qty"])
        if rows:
            pnl = sum(float(r["closedPnl"]) for r in rows)
            sized = sum(float(r.get("closedSize") or r.get("qty") or 0) for r in rows)
            exit_price = (
                sum(float(r["avgExitPrice"]) * float(r.get("closedSize") or r.get("qty") or 0) for r in rows) / sized
                if sized else float(rows[-1]["avgExitPrice"])
            )
        else:
            pnl, exit_price = 0.0, record["entry_price"]
            record["unclean"] = True
            await self._alert("RESULT_UNKNOWN", f"{self.symbol}: the trade closed but Bybit gave no result; recorded as not clean.", "WARNING")

        clean = not (record.get("unclean") or force_unclean)
        r_multiple = round(pnl / record["effective_r"], 3) if record["effective_r"] else 0.0
        self.state["active_position"] = None  # cleared before recording so both reach disk in one write
        trade = await self.record_trade(
            self.symbol, record["side"], qty, record["entry_price"], round(exit_price, 2),
            round(pnl, 4), r_multiple, clean,
        )
        self._decide(f"trade closed: {pnl:+.2f} USD ({r_multiple:+.2f}R){'' if clean else ' [not counted as clean]'}")
        await self._alert(
            "TRADE_CLOSED",
            f"{self.symbol} {'LONG' if record['side'] == 'Buy' else 'SHORT'} closed {pnl:+.2f} USD ({r_multiple:+.2f}R)\nEntry {record['entry_price']:.1f} -> exit {exit_price:.1f}"
            f"{'' if clean else chr(10) + 'Not counted toward the 20-trade gate.'}",
            "INFO" if pnl >= 0 else "WARNING",
        )
        return trade
