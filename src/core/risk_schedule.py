"""
Risk schedule calculations and capital tier definitions.
"""
from dataclasses import dataclass
from math import floor
from typing import Tuple
from src.core.models import TrendDirection


@dataclass(frozen=True)
class RiskScheduleResult:
    is_trading_enabled: bool
    base_r: float
    effective_r: float
    leverage_ceiling: int
    rejection_reason: str = ""


def calculate_base_risk(capital: float) -> float:
    """
    Capital Schedule:
    - Below $50: $1
    - $50 to $449.99: $1 + $0.5 for each full $50 above $50
    - $450 to $599.99: $5 (hard cap)
    - $600 and above: $5 + $1 for each full $100 above $500
    """
    if capital < 50.0:
        return 1.0
    elif capital < 450.0:
        step = floor((capital - 50.0) / 50.0)
        return 1.0 + 0.5 * step
    elif capital < 600.0:
        return 5.0
    else:
        step = floor((capital - 500.0) / 100.0)
        return 5.0 + 1.0 * step


def get_leverage_ceiling(current_equity: float) -> Tuple[bool, int, str]:
    """
    Leverage and Trading Status by Real-time Equity:
    - Below $10: Disabled, Notification sent
    - $10 to $50: Enabled, Up to 100x
    - Above $50: Enabled, 50x or less
    """
    if current_equity < 10.0:
        return False, 0, "CAPITAL_BELOW_MINIMUM_EQUITY"
    elif current_equity <= 50.0:
        return True, 100, ""
    else:
        return True, 50, ""


def evaluate_risk_schedule(
    day_start_capital: float,
    current_equity: float,
    macro_trend: TrendDirection,
    micro_trend: TrendDirection,
) -> RiskScheduleResult:
    """
    Evaluates trading authorization, base R, effective R (half-risk on counter-trend),
    and leverage ceiling based on capital and trend alignment.
    """
    is_enabled, lev_ceiling, reason = get_leverage_ceiling(current_equity)
    if not is_enabled:
        return RiskScheduleResult(
            is_trading_enabled=False,
            base_r=0.0,
            effective_r=0.0,
            leverage_ceiling=0,
            rejection_reason=reason,
        )

    # Trend check: Range disables trading
    if macro_trend == TrendDirection.RANGE or micro_trend == TrendDirection.RANGE:
        return RiskScheduleResult(
            is_trading_enabled=False,
            base_r=0.0,
            effective_r=0.0,
            leverage_ceiling=lev_ceiling,
            rejection_reason="TREND_RANGE_STAND_DOWN",
        )

    base_r = calculate_base_risk(day_start_capital)

    # Full risk if aligned, half risk if counter-trend
    if macro_trend == micro_trend:
        effective_r = base_r
    else:
        effective_r = 0.5 * base_r

    return RiskScheduleResult(
        is_trading_enabled=True,
        base_r=base_r,
        effective_r=effective_r,
        leverage_ceiling=lev_ceiling,
        rejection_reason="",
    )
