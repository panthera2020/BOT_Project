"""
Trend pullback: buy a dip to the 15m 20-period moving average inside a 1h uptrend (sell a
rally inside a 1h downtrend).

Rules (long; short is the mirror):
  * 1h trend is Up (last two swing highs and lows rising).
  * 15m EMA20 is above 15m EMA50.
  * The latest closed 15m candle dips to the EMA20 (low <= EMA20), closes back above it, and
    closes bullish.
  * Enter at market; stop = lowest low of the last 5 closed 15m candles - 15 points.
Risk: full when the 15m trend also agrees with the 1h, half otherwise.
"""
from typing import List, Optional, Tuple

from src.core.models import OrderSide, TrendDirection
from src.engine import market
from src.engine.strategies.base import RISK_HALF_UNALIGNED, Entry, Strategy
from src.strategy.strategy1 import Candle

FAST, SLOW, STOP_LOOKBACK = 20, 50, 5


def pullback_signal(c15: List[Candle], macro: TrendDirection, buffer: float) -> Tuple[Optional[Tuple[str, float]], str]:
    """((side, raw stop), reason). side/stop are None when there is no trade."""
    if macro not in (TrendDirection.UP, TrendDirection.DOWN):
        return None, "1h trend is Range"
    if len(c15) < SLOW + 1:
        return None, "not enough 15m candles"
    closes = [c.close for c in c15]
    fast, slow = market.ema(closes, FAST)[-1], market.ema(closes, SLOW)[-1]
    last = c15[-1]
    recent = c15[-STOP_LOOKBACK:]
    if macro == TrendDirection.UP:
        if not fast > slow:
            return None, "1h Up but 15m EMA20 is not above EMA50"
        if not (last.low <= fast < last.close and last.is_bullish):
            return None, f"no bullish bounce off the 15m EMA20 ({fast:.1f})"
        return (OrderSide.BUY.value, min(c.low for c in recent) - buffer), f"bounce off EMA20 {fast:.1f}"
    if not fast < slow:
        return None, "1h Down but 15m EMA20 is not below EMA50"
    if not (last.high >= fast > last.close and last.is_bearish):
        return None, f"no bearish rejection at the 15m EMA20 ({fast:.1f})"
    return (OrderSide.SELL.value, max(c.high for c in recent) + buffer), f"rejection at EMA20 {fast:.1f}"


class TrendPullback(Strategy):
    id = "trend_pullback"
    order = 30
    name = "Trend pullback"
    summary = "Buy a dip to the 15m EMA20 inside a 1h uptrend; sell a rally to it inside a 1h downtrend."
    rules = [
        "1h trend must be Up (long) or Down (short); a 1h Range means no trade.",
        "15m EMA20 must be above EMA50 for longs (below for shorts).",
        "Trigger: the latest closed 15m candle touches the EMA20, closes back on the trend side of it, and closes in the trade direction.",
        "Stop = lowest low (highest high) of the last 5 closed 15m candles -/+ 15 points.",
        "Risk: full if the 15m trend agrees with the 1h trend, half otherwise.",
    ]

    async def scan(self, now, equity, c15) -> Optional[Entry]:
        e = self.engine
        if not self.new_candle(c15):
            e.status["text"] = "Watching closed 15m candles for a pullback to the EMA20 in a 1h trend."
            return None
        c1h = market.drop_forming(market.parse_candles(await e.exchange.get_candles(e.symbol, "60", 100)))
        macro, micro = market.detect_trend(c1h), market.detect_trend(c15)
        e.status["trend"] = f"1h {macro.value} / 15m {micro.value}"
        signal, why = pullback_signal(c15, macro, e.config.default_stop_buffer)
        await e.persist()
        if signal is None:
            e._decide(f"15m candle closed: {why} [{e.status['trend']}]")
            return None
        side, stop = signal
        e._decide(f"pullback signal ({'long' if side == 'Buy' else 'short'}): {why}")
        return Entry(side=side, stop_price=stop, trigger_price=c15[-1].close, risk_mode=RISK_HALF_UNALIGNED)
