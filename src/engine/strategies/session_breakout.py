"""
Session open breakout: trade the first break of the opening range of the London or New York
session.

Rules (long; short is the mirror):
  * Sessions (fixed UTC clock times; they do not follow daylight saving): London 08:00,
    New York 14:30.
  * The opening range is the high and low of the first four 15m candles (the first hour).
  * After that hour, within the next 3 hours, the first closed 15m candle that closes above
    the range high (below the range low) is the trigger.
  * Enter at market; stop = the other side of the range -/+ 15 points.
  * At most one trade per session per day: after the first break, that session is finished.
Risk: full base risk.
"""
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.core.models import OrderSide
from src.engine import market
from src.engine.strategies.base import RISK_FULL, Entry, Strategy
from src.strategy.strategy1 import Candle

SESSIONS = (("London", 8, 0), ("New York", 14, 30))
RANGE_MINUTES = 60
TRADE_WINDOW_MINUTES = 180
CANDLE_MS = 15 * 60_000


def session_windows(day_start_ms: int) -> List[Tuple[str, int]]:
    return [(name, day_start_ms + (h * 60 + m) * 60_000) for name, h, m in SESSIONS]


def evaluate_session(c15: List[Candle], buffer: float, done: set) -> Tuple[Optional[Tuple[str, float, str]], str]:
    """((side, raw stop, session key), reason) for the latest candle."""
    last = c15[-1]
    end_ms = last.timestamp + CANDLE_MS
    day = datetime.fromtimestamp(last.timestamp / 1000, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_ms = int(day.timestamp() * 1000)
    for name, start in session_windows(day_ms):
        range_end = start + RANGE_MINUTES * 60_000
        if not (range_end < end_ms <= start + (RANGE_MINUTES + TRADE_WINDOW_MINUTES) * 60_000):
            continue
        key = f"{day.strftime('%Y-%m-%d')}:{name}"
        if key in done:
            return None, f"{name} session already traded today"
        rng = [c for c in c15 if start <= c.timestamp < range_end]
        if len(rng) < RANGE_MINUTES // 15:
            return None, f"{name} opening range is incomplete (only {len(rng)} candles)"
        high, low = max(c.high for c in rng), min(c.low for c in rng)
        if last.close > high:
            return (OrderSide.BUY.value, low - buffer, key), f"{name} range {low:.1f}-{high:.1f} broken upward"
        if last.close < low:
            return (OrderSide.SELL.value, high + buffer, key), f"{name} range {low:.1f}-{high:.1f} broken downward"
        return None, f"{name} range {low:.1f}-{high:.1f} still intact"
    return None, "outside the London / New York breakout windows"


class SessionBreakout(Strategy):
    id = "session_breakout"
    name = "Session open breakout"
    summary = "First 15m close beyond the opening hour's range of the London (08:00 UTC) or New York (14:30 UTC) session."
    rules = [
        "Opening range = high and low of the first four 15m candles after 08:00 UTC (London) or 14:30 UTC (New York). Fixed UTC times, no daylight-saving adjustment.",
        "For the next 3 hours, the first closed 15m candle beyond the range is the trigger; the other side of the range sets the stop (-/+ 15 points).",
        "One trade per session per day.",
        "Full base risk (there is no trend filter).",
    ]

    async def scan(self, now, equity, c15) -> Optional[Entry]:
        e, slot = self.engine, self.slot
        done = set(slot.get("done", []))
        if not self.new_candle(c15):
            e.status["text"] = "Watching for the London (08:00 UTC) / New York (14:30 UTC) opening-range breakout."
            return None
        signal, why = evaluate_session(c15, e.config.default_stop_buffer, done)
        e.status["trend"] = None
        if signal is None:
            e._decide(f"15m candle closed: {why}")
            await e.persist()
            return None
        side, stop, key = signal
        slot["done"] = (sorted(done | {key}))[-10:]
        await e.persist()
        e._decide(f"session breakout ({'long' if side == 'Buy' else 'short'}): {why}")
        return Entry(side=side, stop_price=stop, trigger_price=c15[-1].close, risk_mode=RISK_FULL)
