"""
Liquidity sweep reversal (Strategy 1 from the PRD) and its looser sibling.

Rules (long; the short side is the exact mirror):
  1. A closed 15m candle's low goes below the last swing low that nothing has undercut yet
     and the candle closes back above it (a stop run).
  2. Within the setup window, three consecutive bullish 2m candles appear, none going below
     the sweep low.
  3. Enter at market; stop = sweep low - buffer.
`SweepReversal` is the original, rule for rule. `SweepReversalLoose` is a separate strategy
built on the same engine with three looser settings; the original is not changed by it.
"""
from typing import Any, Dict, Optional

from src.core.models import OrderSide, TrendDirection
from src.engine import market
from src.engine.strategies.base import RISK_HALF_UNALIGNED, RISK_TABLE, Entry, Strategy
from src.strategy.strategy1 import Strategy1StateMachine


class SweepReversal(Strategy):
    id = "sweep_reversal"
    order = 10
    name = "Liquidity sweep reversal"
    summary = "15m sweep of a swing low/high, then 3 confirming 2m candles. Needs the 1h and 15m trends to be Up or Down."
    rules = [
        "15m trend picks the side: Up -> long after a swing-low sweep, Down -> short after a swing-high sweep.",
        "1h trend only sets the risk: full when it agrees with the 15m trend, half when it does not.",
        "A Range on the 1h or the 15m trend means no trade.",
        "Sweep = a closed 15m candle goes past the last unswept 5-candle swing and closes back inside.",
        "Trigger = 3 consecutive 2m candles in the trade direction within 60 minutes, none breaking the sweep extreme.",
        "Stop = sweep extreme -/+ 15 points.",
    ]
    swing_n = market.FRACTAL
    window_minutes = 60
    risk_mode = RISK_TABLE
    legacy_state = True       # keeps the state keys v1.7+ already saved, so a restart mid-setup still works

    def __init__(self, engine: Any, window_minutes: Optional[int] = None):
        super().__init__(engine)
        self.sm = Strategy1StateMachine(symbol=engine.symbol, stop_buffer=engine.config.default_stop_buffer)
        if window_minutes is not None:
            self.window_minutes = window_minutes

    @property
    def slot(self) -> Dict[str, Any]:
        return self.engine.es if self.legacy_state else super().slot

    def _direction(self, macro: TrendDirection, micro: TrendDirection) -> Optional[str]:
        return market.authorized_direction(macro, micro)

    def _stand_down_text(self) -> str:
        return f"trend {self.engine.status['trend']} -> stand down (range)"

    # ------------------------------------------------------------------ scanning
    async def scan(self, now, equity, c15) -> Optional[Entry]:
        e = self.engine
        slot = self.slot
        slot.setdefault("setup", None)
        if self.new_candle(c15):
            await self._evaluate_new_15m_candle(c15, c15[-1])

        setup = slot.get("setup")
        if not setup:
            e.status["text"] = "Watching 15m candles for a liquidity sweep of the last swing low."
            return None

        now_ms = int(now.timestamp() * 1000)
        if now_ms > setup["expires_ms"]:
            slot["setup"] = None
            e._decide("setup expired: no 2m trigger within the window")
            await e.persist()
            return None

        side = setup.get("side", OrderSide.BUY.value)  # setups saved by v1.7 had no side: they were long
        extreme = setup.get("sweep_extreme", setup.get("sweep_low"))
        c2 = market.aggregate_2m(market.drop_forming(market.parse_candles(await e.exchange.get_candles(e.symbol, "1", 200))))
        after = [c for c in c2 if c.timestamp >= setup["sweep_end_ms"]]
        if side == OrderSide.BUY.value:
            triggered, price, why = self.sm.check_2m_micro_trigger(after, extreme)
        else:
            triggered, price, why = self.sm.check_2m_micro_trigger_short(after, extreme)
        if why.startswith("INVALIDATED"):
            slot["setup"] = None
            e._decide(f"setup invalidated: price broke the sweep {'low' if side == OrderSide.BUY.value else 'high'}")
            await e.persist()
            return None
        if not triggered:
            word = "bullish" if side == OrderSide.BUY.value else "bearish"
            e.status["text"] = f"{'Long' if side == OrderSide.BUY.value else 'Short'} sweep detected; waiting for 3 {word} 2m candles ({why})."
            return None

        slot["setup"] = None  # a trigger is used up whether or not the trade passes the gates
        await e.persist()
        buffer = self.sm.stop_buffer
        stop = extreme - buffer if side == OrderSide.BUY.value else extreme + buffer
        return Entry(side=side, stop_price=stop, trigger_price=price, risk_mode=self.risk_mode)

    async def _evaluate_new_15m_candle(self, c15: list, latest: Any) -> None:
        e = self.engine
        c1h = market.drop_forming(market.parse_candles(await e.exchange.get_candles(e.symbol, "60", 100)))
        macro, micro = market.detect_trend(c1h), market.detect_trend(c15)
        e.status["trend"] = f"1h {macro.value} / 15m {micro.value}"
        side = self._direction(macro, micro)
        history = c15[:-1][-60:]
        n = self.swing_n
        if side is None:
            self.slot["setup"] = None
            e._decide(f"15m candle closed: {self._stand_down_text()}")
        elif side == OrderSide.BUY.value:
            level = market.latest_unswept_swing_low(history, n)
            if level is None:
                e._decide("15m candle closed: no swing low to sweep")
            else:
                swept, why = self.sm.evaluate_15m_sweep(level, latest)
                if swept:
                    await self._arm(side, level, latest, latest.low)
                else:
                    e._decide(f"15m candle closed: {why} (long side, swing low {level:.1f})")
        else:
            level = market.latest_unswept_swing_high(history, n)
            if level is None:
                e._decide("15m candle closed: no swing high to sweep")
            else:
                swept, why = self.sm.evaluate_15m_sweep_short(level, latest)
                if swept:
                    await self._arm(side, level, latest, latest.high)
                else:
                    e._decide(f"15m candle closed: {why} (short side, swing high {level:.1f})")
        await e.persist()

    async def _arm(self, side: str, level: float, candle: Any, extreme: float) -> None:
        e = self.engine
        end_ms = candle.timestamp + 15 * 60_000
        self.slot["setup"] = {
            "side": side, "sweep_extreme": extreme, "swing_level": level,
            "sweep_end_ms": end_ms, "expires_ms": end_ms + self.window_minutes * 60_000,
        }
        word = "bullish" if side == OrderSide.BUY.value else "bearish"
        kind = "low" if side == OrderSide.BUY.value else "high"
        e._decide(f"15m sweep of swing {kind} {level:.1f} confirmed (extreme {extreme:.1f}); waiting for 3 {word} 2m candles [{e.status['trend']}]")
        await e._alert(
            "SWEEP_DETECTED",
            f"{e.symbol}: {'long' if side == OrderSide.BUY.value else 'short'} setup. 15m sweep of {level:.1f}, extreme {extreme:.1f}. Waiting for the 2m trigger.",
            "INFO",
        )


class SweepReversalLoose(SweepReversal):
    id = "sweep_reversal_loose"
    order = 20
    name = "Liquidity sweep reversal (looser)"
    summary = "Same idea as strategy 1 with three looser settings, so it fires more often. A separate strategy: the original is untouched."
    rules = [
        "Same sweep and 2m trigger as strategy 1, with these differences:",
        "Swings are 3-candle (one candle each side) instead of 5-candle, so more swing points exist to sweep.",
        "The 15m trend alone picks the side (Up -> long, Down -> short). A Range on the 1h no longer stops it.",
        "Risk is full when the 1h and 15m trends agree, half otherwise (including a 1h Range).",
        "The 2m trigger may take up to 120 minutes instead of 60.",
    ]
    swing_n = 1
    window_minutes = 120
    risk_mode = RISK_HALF_UNALIGNED
    legacy_state = False

    def _direction(self, macro: TrendDirection, micro: TrendDirection) -> Optional[str]:
        if micro == TrendDirection.UP:
            return OrderSide.BUY.value
        if micro == TrendDirection.DOWN:
            return OrderSide.SELL.value
        return None

    def _stand_down_text(self) -> str:
        return f"trend {self.engine.status['trend']} -> stand down (15m range)"
