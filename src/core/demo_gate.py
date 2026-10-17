"""
Demo qualification gate: live trading unlocks only after the demo run has lasted at
least 24 hours AND at least 20 clean demo trades have been recorded.

Pure functions only (no I/O), so the rule is easy to test at its boundaries.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

MIN_DEMO_HOURS = 24
MIN_CLEAN_TRADES = 20


@dataclass(frozen=True)
class DemoGateStatus:
    elapsed_hours: float
    clean_trades: int
    has_24h_run: bool
    has_20_clean_trades: bool
    is_unlocked: bool


def evaluate_demo_gate(
    started_at: Optional[datetime],
    clean_trades: int,
    now: Optional[datetime] = None,
    min_hours: float = MIN_DEMO_HOURS,
    min_trades: int = MIN_CLEAN_TRADES,
) -> DemoGateStatus:
    """Return the gate status.

    started_at is when demo testing was first started; None means it has never been
    started, so the clock reads zero and the gate stays locked. Naive datetimes are
    treated as UTC.
    """
    now = now or datetime.now(timezone.utc)
    if started_at is None:
        elapsed = timedelta(0)
    else:
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = max(now - started_at, timedelta(0))

    has_hours = elapsed >= timedelta(hours=min_hours)
    has_trades = clean_trades >= min_trades
    return DemoGateStatus(
        elapsed_hours=round(elapsed.total_seconds() / 3600.0, 2),
        clean_trades=clean_trades,
        has_24h_run=has_hours,
        has_20_clean_trades=has_trades,
        is_unlocked=has_hours and has_trades,
    )
