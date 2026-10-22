"""Hand-built candle sets that make each strategy fire, for the engine tests."""
import math
from datetime import datetime, timezone
from typing import List

from src.engine import market
from src.strategy.strategy1 import Candle
from tests.sim_exchange import MIN, SWEEP_END_MS, default_markets

M15 = 15 * MIN


def ts(y, mo, d, h, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def rows(candles: List[Candle], forming_ts: int, forming_price: float) -> List[List[str]]:
    """Bybit-style rows, newest first, with the still-forming candle on top."""
    out = [[str(c.timestamp), str(c.open), str(c.high), str(c.low), str(c.close), "1", "1"] for c in candles]
    out.append([str(forming_ts), str(forming_price), str(forming_price + 5), str(forming_price - 5), str(forming_price), "1", "1"])
    return out[::-1]


def flip(c: Candle, k: float = 204000.0) -> Candle:
    return Candle(c.timestamp, k - c.open, k - c.low, k - c.high, k - c.close)


def one_minute_rows(end_ms: int, price: float) -> List[List[str]]:
    """Quiet 1m candles (not used by the new strategies, but the engine may ask for them)."""
    cs = [Candle(end_ms - (i + 1) * MIN, price, price + 1, price - 1, price) for i in range(60)][::-1]
    return rows(cs, end_ms, price)


def with_markets(c15: List[Candle], forming_ts: int, price: float, base=None) -> dict:
    """default_markets' 1h (an UP trend) and 1m, with the given 15m candles."""
    mk = dict(base or default_markets())
    mk["15"] = rows(c15, forming_ts, price)
    return mk


# ---------------------------------------------------------------- trend pullback (long)
def pullback_candles(end_ms: int = SWEEP_END_MS) -> List[Candle]:
    """60 rising 15m candles, the last one dipping to the EMA20 and closing back above it."""
    n = 60
    first = end_ms - n * M15
    closes = [100000.0 + 40 * i for i in range(n - 1)]
    out = [Candle(first + i * M15, c - 30, c + 10, c - 600, c) for i, c in enumerate(closes)]
    fast = market.ema(closes + [closes[-1] + 40], 20)[-1]
    close = fast + 60
    out.append(Candle(first + (n - 1) * M15, fast - 10, close + 10, fast - 120, close))
    return out


# ---------------------------------------------------------------- breakout and retest (long)
def flat_candles(end_ms: int, n: int, mid: float = 102000.0, half: float = 50.0) -> List[Candle]:
    """n sideways 15m candles ending at end_ms; highs reach mid+half, lows mid-half."""
    first = end_ms - n * M15
    out = []
    for i in range(n):
        up = i % 2 == 0
        out.append(Candle(first + i * M15, mid - 20 if up else mid + 20, mid + half, mid - half, mid + 20 if up else mid - 20))
    return out


# ---------------------------------------------------------------- range reversion (long)
def reversion_candles(end_ms: int, slide_step: float = 45.0) -> List[Candle]:
    """A sideways market, then a slide that ends in a bullish candle with a long lower wick."""
    n_osc, n_slide = 40, 12
    n = n_osc + n_slide + 1
    first = end_ms - n * M15
    closes = [102000 + 120 * math.sin(i * 0.9) for i in range(n_osc)]
    price = closes[-1]
    for i in range(n_slide):
        price -= slide_step
        closes.append(price)
    out = []
    prev = 102000.0
    for i, c in enumerate(closes):
        out.append(Candle(first + i * M15, prev, max(prev, c) + 8, min(prev, c) - 8, c))
        prev = c
    # final candle: stabs well below the lower band, closes back inside and bullish
    last_close = closes[-1] + 25
    out.append(Candle(first + (n - 1) * M15, closes[-1] - 5, last_close + 8, closes[-1] - 450, last_close))
    return out
