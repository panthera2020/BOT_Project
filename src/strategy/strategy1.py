"""
Strategy 1 technical entry engine:
- Multi-timeframe trend detector (1h macro, 15m day)
- 15m candle liquidity sweep validator
- 2m micro-structure 3-candle momentum confirmation trigger
"""
from dataclasses import dataclass
from typing import List, Optional
from src.core.models import TrendDirection, OrderSide


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    side: OrderSide
    entry_price: float
    sweep_pivot_price: float
    stop_price: float
    timestamp: int
    macro_trend: TrendDirection
    micro_trend: TrendDirection


def detect_trend_from_pivots(candles: List[Candle]) -> TrendDirection:
    """
    Evaluates trend direction based on sequential swing highs and swing lows:
    - UP: sequential higher highs and higher lows across candles
    - DOWN: sequential lower highs and lower lows across candles
    - Otherwise: RANGE
    """
    if len(candles) < 3:
        return TrendDirection.RANGE

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    higher_highs = all(highs[i] > highs[i - 1] for i in range(1, len(highs)))
    higher_lows = all(lows[i] > lows[i - 1] for i in range(1, len(lows)))

    lower_highs = all(highs[i] < highs[i - 1] for i in range(1, len(highs)))
    lower_lows = all(lows[i] < lows[i - 1] for i in range(1, len(lows)))

    if higher_highs and higher_lows:
        return TrendDirection.UP
    elif lower_highs and lower_lows:
        return TrendDirection.DOWN
    else:
        return TrendDirection.RANGE


class Strategy1StateMachine:
    def __init__(self, symbol: str = "BTCUSDT", stop_buffer: float = 15.0):
        self.symbol = symbol
        self.stop_buffer = stop_buffer

    def evaluate_15m_sweep(
        self,
        swing_low: float,
        candle_15m: Candle,
    ) -> tuple[bool, str]:
        """
        Validates 15m liquidity sweep:
        - Low must dip below swing_low
        - Close must be strictly back above swing_low
        - If close is below swing_low, setup is cancelled
        """
        if candle_15m.low >= swing_low:
            return False, "NO_SWEEP"

        if candle_15m.close < swing_low:
            return False, "SWEEP_FAILED_CLOSE_BELOW_SWING"

        return True, "SWEEP_CONFIRMED"

    def check_2m_micro_trigger(
        self,
        candles_2m_after_sweep: List[Candle],
        sweep_low_extreme: float,
    ) -> tuple[bool, Optional[float], str]:
        """
        Validates 2m entry trigger:
        - If any 2m candle breaks below sweep_low_extreme, setup is invalidated immediately.
        - Requires exactly 3 consecutive bullish 2m candles immediately following the sweep.
        """
        if not candles_2m_after_sweep:
            return False, None, "AWAITING_2M_CANDLES"

        # Check for invalidation beneath sweep extreme
        for c in candles_2m_after_sweep:
            if c.low < sweep_low_extreme:
                return False, None, "INVALIDATED_BELOW_SWEEP_EXTREME"

        if len(candles_2m_after_sweep) < 3:
            return False, None, "INSUFFICIENT_2M_CANDLES"

        # Check that the last 3 candles are all bullish
        last_three = candles_2m_after_sweep[-3:]
        all_bullish = all(c.is_bullish for c in last_three)

        if not all_bullish:
            return False, None, "LESS_THAN_THREE_CONSECUTIVE_BULLISH"

        entry_price = last_three[-1].close
        return True, entry_price, "TRIGGER_CONFIRMED"

    # ---- short side: exact mirror of the long rules, for the PRD's "Short Only" rows ----

    def evaluate_15m_sweep_short(
        self,
        swing_high: float,
        candle_15m: Candle,
    ) -> tuple[bool, str]:
        """Mirror of evaluate_15m_sweep for a short setup:
        - High must trade strictly above swing_high
        - Close must be strictly back below swing_high
        - A close above swing_high is a structural breakout: setup cancelled
        """
        if candle_15m.high <= swing_high:
            return False, "NO_SWEEP"

        if candle_15m.close > swing_high:
            return False, "SWEEP_FAILED_CLOSE_ABOVE_SWING"

        return True, "SWEEP_CONFIRMED"

    def check_2m_micro_trigger_short(
        self,
        candles_2m_after_sweep: List[Candle],
        sweep_high_extreme: float,
    ) -> tuple[bool, Optional[float], str]:
        """Mirror of check_2m_micro_trigger: any 2m high above the sweep high invalidates the
        setup; the trigger is three consecutive bearish 2m candles."""
        if not candles_2m_after_sweep:
            return False, None, "AWAITING_2M_CANDLES"

        for c in candles_2m_after_sweep:
            if c.high > sweep_high_extreme:
                return False, None, "INVALIDATED_ABOVE_SWEEP_EXTREME"

        if len(candles_2m_after_sweep) < 3:
            return False, None, "INSUFFICIENT_2M_CANDLES"

        last_three = candles_2m_after_sweep[-3:]
        if not all(c.is_bearish for c in last_three):
            return False, None, "LESS_THAN_THREE_CONSECUTIVE_BEARISH"

        return True, last_three[-1].close, "TRIGGER_CONFIRMED"
