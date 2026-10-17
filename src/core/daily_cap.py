"""
Daily trade cap tracker and 02:00 Lagos (01:00 UTC) boundary calculation.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


# Lagos time is UTC+1. 02:00 Lagos is 01:00 UTC.
LAGOS_OFFSET_HOURS = 1
RESET_HOUR_UTC = 1  # 01:00 UTC
MAX_DAILY_TRADES = 2


def get_trading_day_id(dt_utc: Optional[datetime] = None) -> str:
    """
    Returns the deterministic trading day identifier string (YYYY-MM-DD)
    where the boundary resets at 01:00 UTC (02:00 Lagos).
    
    Any timestamp prior to 01:00 UTC belongs to the previous trading day.
    """
    if dt_utc is None:
        dt_utc = datetime.now(timezone.utc)
    elif dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    else:
        dt_utc = dt_utc.astimezone(timezone.utc)

    # If current hour is before 01:00 UTC, trading day began yesterday
    if dt_utc.hour < RESET_HOUR_UTC:
        effective_date = (dt_utc - timedelta(days=1)).date()
    else:
        effective_date = dt_utc.date()

    return effective_date.strftime("%Y-%m-%d")


@dataclass
class DailyTradeTracker:
    max_daily_trades: int = MAX_DAILY_TRADES
    current_trading_day: str = ""
    daily_trade_count: int = 0

    def can_open_trade(self, now_utc: Optional[datetime] = None) -> tuple[bool, str]:
        day_id = get_trading_day_id(now_utc)
        if day_id != self.current_trading_day:
            return True, ""

        if self.daily_trade_count >= self.max_daily_trades:
            return False, f"DAILY_CAP_REACHED ({self.daily_trade_count}/{self.max_daily_trades})"

        return True, ""

    def record_trade(self, now_utc: Optional[datetime] = None) -> bool:
        day_id = get_trading_day_id(now_utc)
        if day_id != self.current_trading_day:
            self.current_trading_day = day_id
            self.daily_trade_count = 0

        if self.daily_trade_count >= self.max_daily_trades:
            return False

        self.daily_trade_count += 1
        return True

    def reset_if_new_day(self, now_utc: Optional[datetime] = None) -> bool:
        day_id = get_trading_day_id(now_utc)
        if day_id != self.current_trading_day:
            self.current_trading_day = day_id
            self.daily_trade_count = 0
            return True
        return False
