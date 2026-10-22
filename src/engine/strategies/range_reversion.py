"""
Range mean reversion: fade a stretch to the Bollinger band when the 15m market is ranging.
It trades in the conditions where strategy 1 stands down.

Rules (long; short is the mirror):
  * The 15m trend is Range (no consecutive higher highs and lows, or lower ones).
  * The latest closed 15m candle's low goes below the lower Bollinger band (20, 2 standard
    deviations), it closes back above that band, closes bullish, and the 14-period RSI is at
    or below 35.
  * Enter at market; stop = that candle's low - 15 points.
Risk: full base risk. Note: the shared exit plan (take-profits at 3R and 5R) is the same as for
every strategy, which is a long way for a mean-reversion trade to travel; the early-exit rule
will close many of these trades early. That is the cost of using one exit structure for all.
"""
from typing import List, Optional, Tuple

from src.core.models import OrderSide, TrendDirection
from src.engine import market
from src.engine.strategies.base import RISK_FULL, Entry, Strategy
from src.strategy.strategy1 import Candle

BB_PERIOD, BB_K, RSI_PERIOD, RSI_LOW, RSI_HIGH = 20, 2.0, 14, 35.0, 65.0


def reversion_signal(c15: List[Candle], micro: TrendDirection, buffer: float) -> Tuple[Optional[Tuple[str, float]], str]:
    if micro != TrendDirection.RANGE:
        return None, f"15m trend is {micro.value}, not Range"
    closes = [c.close for c in c15]
    bands = market.bollinger(closes, BB_PERIOD, BB_K)
    value = market.rsi(closes, RSI_PERIOD)
    if bands is None or value is None:
        return None, "not enough 15m candles"
    _, upper, lower = bands
    last = c15[-1]
    if last.low < lower and last.close > lower and last.is_bullish and value <= RSI_LOW:
        return (OrderSide.BUY.value, last.low - buffer), f"pierced the lower band {lower:.1f} and closed back inside, RSI {value:.0f}"
    if last.high > upper and last.close < upper and last.is_bearish and value >= RSI_HIGH:
        return (OrderSide.SELL.value, last.high + buffer), f"pierced the upper band {upper:.1f} and closed back inside, RSI {value:.0f}"
    return None, f"no band reversal (bands {lower:.1f}-{upper:.1f}, RSI {value:.0f})"


class RangeReversion(Strategy):
    id = "range_reversion"
    order = 60
    name = "Range mean reversion"
    summary = "In a ranging 15m market, fade a close back inside the Bollinger band with an RSI extreme."
    rules = [
        "Only when the 15m trend is Range (the conditions where strategy 1 stands down).",
        "Long: a 15m candle dips below the lower Bollinger band (20, 2), closes back above it, closes bullish, RSI(14) <= 35. Short is the mirror (RSI >= 65).",
        "Stop = the candle's extreme -/+ 15 points.",
        "Full base risk. Uses the same 3R / 5R exit plan as every strategy, so expect many early exits.",
    ]

    async def scan(self, now, equity, c15) -> Optional[Entry]:
        e = self.engine
        if not self.new_candle(c15):
            e.status["text"] = "Watching 15m candles for a Bollinger-band reversal in a range."
            return None
        c1h = market.drop_forming(market.parse_candles(await e.exchange.get_candles(e.symbol, "60", 100)))
        macro, micro = market.detect_trend(c1h), market.detect_trend(c15)
        e.status["trend"] = f"1h {macro.value} / 15m {micro.value}"
        signal, why = reversion_signal(c15, micro, e.config.default_stop_buffer)
        await e.persist()
        if signal is None:
            e._decide(f"15m candle closed: {why} [{e.status['trend']}]")
            return None
        side, stop = signal
        e._decide(f"reversion signal ({'long' if side == 'Buy' else 'short'}): {why}")
        return Entry(side=side, stop_price=stop, trigger_price=c15[-1].close, risk_mode=RISK_FULL)
