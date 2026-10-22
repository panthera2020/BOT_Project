"""
Pure market-data helpers for the demo engine (no network, no clock):
candle parsing, 2-minute candles built from 1-minute ones, swing points and trend.

Interpretations the PRD does not spell out (kept here so they are easy to find and change):
  * Bybit has no 2-minute candle, so a 2m candle is two aligned 1m candles.
  * A swing low / swing high is a 5-candle fractal: strictly lower (higher) than the two
    candles on each side. Only confirmed swings count (two later candles exist).
  * The swing low a sweep is measured against is the most recent swing low that has not
    already been undercut by a later candle.
  * Trend is UP when the last two swing highs AND the last two swing lows both rise, DOWN
    when both fall, otherwise RANGE (so too few swings means RANGE: stand down).
"""
from typing import Any, Dict, List, Optional, Sequence

from src.core.models import TrendDirection
from src.strategy.strategy1 import Candle

FRACTAL = 2  # candles on each side of a swing point


def parse_candles(rows: Sequence[Sequence[Any]]) -> List[Candle]:
    """Bybit rows are newest first: [start_ms, open, high, low, close, volume, turnover].
    Returns candles oldest first. Rows that cannot be read are skipped."""
    out: List[Candle] = []
    for row in rows:
        try:
            out.append(Candle(
                timestamp=int(row[0]), open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5]) if len(row) > 5 else 0.0,
            ))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda c: c.timestamp)
    return out


def drop_forming(candles: List[Candle]) -> List[Candle]:
    """Bybit always includes the candle that is still forming as the newest one."""
    return candles[:-1] if candles else []


def aggregate_2m(closed_1m: List[Candle]) -> List[Candle]:
    """Build 2-minute candles from closed 1-minute candles. A 2m candle starts on an even
    minute and needs both of its 1m candles."""
    by_start: Dict[int, Candle] = {c.timestamp: c for c in closed_1m}
    out: List[Candle] = []
    for start in sorted(by_start):
        if (start // 60_000) % 2 != 0:
            continue
        first, second = by_start[start], by_start.get(start + 60_000)
        if second is None:
            continue
        out.append(Candle(
            timestamp=start, open=first.open, high=max(first.high, second.high),
            low=min(first.low, second.low), close=second.close,
            volume=first.volume + second.volume,
        ))
    return out


def swing_low_indexes(candles: List[Candle], n: int = FRACTAL) -> List[int]:
    return [
        i for i in range(n, len(candles) - n)
        if all(candles[i].low < candles[i - k].low and candles[i].low < candles[i + k].low for k in range(1, n + 1))
    ]


def swing_high_indexes(candles: List[Candle], n: int = FRACTAL) -> List[int]:
    return [
        i for i in range(n, len(candles) - n)
        if all(candles[i].high > candles[i - k].high and candles[i].high > candles[i + k].high for k in range(1, n + 1))
    ]


def latest_unswept_swing_low(history: List[Candle], n: int = FRACTAL) -> Optional[float]:
    """Most recent confirmed swing low in `history` that no later candle has undercut."""
    for i in reversed(swing_low_indexes(history, n)):
        level = history[i].low
        if all(c.low >= level for c in history[i + 1:]):
            return level
    return None


def latest_unswept_swing_high(history: List[Candle], n: int = FRACTAL) -> Optional[float]:
    """Most recent confirmed swing high in `history` that no later candle has exceeded."""
    for i in reversed(swing_high_indexes(history, n)):
        level = history[i].high
        if all(c.high <= level for c in history[i + 1:]):
            return level
    return None


def authorized_direction(macro: TrendDirection, micro: TrendDirection) -> Optional[str]:
    """The PRD's trend table: the 15m trend picks the side (long when 15m is Up, short when
    Down); the 1h trend only decides full or half risk. A Range on either timeframe means no
    trade. Returns "Buy", "Sell" or None."""
    if macro == TrendDirection.RANGE or micro == TrendDirection.RANGE:
        return None
    return "Buy" if micro == TrendDirection.UP else "Sell"


def detect_trend(candles: List[Candle], n: int = FRACTAL) -> TrendDirection:
    highs = [candles[i].high for i in swing_high_indexes(candles, n)]
    lows = [candles[i].low for i in swing_low_indexes(candles, n)]
    if len(highs) < 2 or len(lows) < 2:
        return TrendDirection.RANGE
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return TrendDirection.UP
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return TrendDirection.DOWN
    return TrendDirection.RANGE


def round_to_tick(price: float, tick: float) -> float:
    """Round a price to the exchange tick size (Bybit rejects prices off the tick grid)."""
    decimals = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[1])) if tick < 1 else 0
    return round(round(price / tick) * tick, decimals)


# ---------------------------------------------------------------- indicators
def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Exponential moving average, seeded with the simple average of the first `period`
    values. The first period-1 entries are None."""
    out: List[Optional[float]] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI of the latest close, or None with fewer than period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def bollinger(closes: Sequence[float], period: int = 20, k: float = 2.0) -> Optional[tuple]:
    """(middle, upper, lower) bands of the latest `period` closes (population standard
    deviation), or None with too few closes."""
    if len(closes) < period:
        return None
    window = list(closes[-period:])
    mid = sum(window) / period
    sd = (sum((x - mid) ** 2 for x in window) / period) ** 0.5
    return mid, mid + k * sd, mid - k * sd
