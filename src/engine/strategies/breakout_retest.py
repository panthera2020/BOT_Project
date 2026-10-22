"""
Breakout and retest: a 15m candle closes beyond the last 20 candles' range, then price comes
back to the broken level and holds it.

Rules (long; short is the mirror):
  * Breakout: a closed 15m candle closes above the highest high of the 20 candles before it.
    The 1h trend must not be Down.
  * Retest (within 8 candles = 2 hours): a later closed 15m candle trades down to the broken
    level (low <= level), closes back above it, and closes bullish. A close below the level
    cancels the setup.
  * Enter at market; stop = the retest candle's low - 15 points.
Risk: full when the 15m and 1h trends agree, half otherwise.
"""
from typing import List, Optional, Tuple

from src.core.models import OrderSide, TrendDirection
from src.engine import market
from src.engine.strategies.base import RISK_HALF_UNALIGNED, Entry, Strategy
from src.strategy.strategy1 import Candle

LOOKBACK = 20
RETEST_CANDLES = 8
CANDLE_MS = 15 * 60_000


def find_breakout(c15: List[Candle], macro: TrendDirection) -> Optional[Tuple[str, float]]:
    """(side, broken level) when the latest candle closed beyond the 20 before it."""
    if len(c15) < LOOKBACK + 1:
        return None
    last, prior = c15[-1], c15[-LOOKBACK - 1:-1]
    high, low = max(c.high for c in prior), min(c.low for c in prior)
    if last.close > high and macro != TrendDirection.DOWN:
        return OrderSide.BUY.value, high
    if last.close < low and macro != TrendDirection.UP:
        return OrderSide.SELL.value, low
    return None


def check_retest(setup: dict, candle: Candle, buffer: float) -> Tuple[str, Optional[float]]:
    """("entry", raw stop) | ("invalid", None) | ("wait", None) for a candle after the breakout."""
    level = setup["level"]
    if setup["side"] == OrderSide.BUY.value:
        if candle.close < level:
            return "invalid", None
        if candle.low <= level and candle.is_bullish:
            return "entry", candle.low - buffer
    else:
        if candle.close > level:
            return "invalid", None
        if candle.high >= level and candle.is_bearish:
            return "entry", candle.high + buffer
    return "wait", None


class BreakoutRetest(Strategy):
    id = "breakout_retest"
    order = 40
    name = "Breakout and retest"
    summary = "A 15m close beyond the 20-candle high/low, then a retest of the broken level that holds."
    rules = [
        "Breakout: a closed 15m candle closes above the highest high (below the lowest low) of the 20 candles before it. Longs are skipped when the 1h trend is Down, shorts when it is Up.",
        "Retest: within 8 candles, a closed 15m candle returns to the broken level, closes back on the breakout side and in the trade direction. A close back through the level cancels it.",
        "Stop = the retest candle's extreme -/+ 15 points.",
        "Risk: full if the 15m and 1h trends agree, half otherwise.",
    ]

    async def scan(self, now, equity, c15) -> Optional[Entry]:
        e, slot = self.engine, self.slot
        slot.setdefault("setup", None)
        if not self.new_candle(c15):
            e.status["text"] = "Setup armed: waiting for a retest of the broken level." if slot["setup"] else "Watching 15m candles for a 20-candle breakout."
            return None
        last = c15[-1]
        c1h = market.drop_forming(market.parse_candles(await e.exchange.get_candles(e.symbol, "60", 100)))
        macro, micro = market.detect_trend(c1h), market.detect_trend(c15)
        e.status["trend"] = f"1h {macro.value} / 15m {micro.value}"

        setup = slot["setup"]
        if setup:
            if last.timestamp > setup["armed_ts"] + RETEST_CANDLES * CANDLE_MS:
                slot["setup"] = None  # expired; the same candle may start a new breakout below
            else:
                verdict, stop = check_retest(setup, last, e.config.default_stop_buffer)
                if verdict == "invalid":
                    slot["setup"] = None
                    e._decide(f"breakout cancelled: price closed back through {setup['level']:.1f}")
                elif verdict == "entry":
                    slot["setup"] = None
                    await e.persist()
                    e._decide(f"retest of {setup['level']:.1f} held ({'long' if setup['side'] == 'Buy' else 'short'})")
                    return Entry(side=setup["side"], stop_price=stop, trigger_price=last.close, risk_mode=RISK_HALF_UNALIGNED)
                else:
                    e._decide(f"waiting for the retest of {setup['level']:.1f}")
                await e.persist()
                return None
        if slot["setup"] is None:
            found = find_breakout(c15, macro)
            if found:
                side, level = found
                slot["setup"] = {"side": side, "level": level, "armed_ts": last.timestamp}
                e._decide(f"{'upside' if side == 'Buy' else 'downside'} breakout of {level:.1f}; waiting for a retest [{e.status['trend']}]")
            else:
                e._decide(f"15m candle closed: no 20-candle breakout [{e.status['trend']}]")
        await e.persist()
        return None
