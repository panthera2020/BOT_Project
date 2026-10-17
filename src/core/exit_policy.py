"""
Exit policy engine managing multi-stage trade execution:
- TP1 quantization: ceil(0.70 * qty / lot) lots
- TP2: remaining lots at 5R
- Breakeven shift at 1.5R: entry + round-turn fees + slippage buffer
- Early momentum giveback exit: armed at 1.0R, triggers on >= 50% peak gain retracement prior to 1.5R
"""
from dataclasses import dataclass
from math import ceil
from typing import Optional, Tuple
from src.core.models import OrderSide, PairConfig


class ExitActionType:
    NONE = "NONE"
    CLOSE_MARKET = "CLOSE_MARKET"
    MOVE_STOP_BREAKEVEN = "MOVE_STOP_BREAKEVEN"
    TAKE_PROFIT_1 = "TAKE_PROFIT_1"
    TAKE_PROFIT_2 = "TAKE_PROFIT_2"


@dataclass
class TradeExitPlan:
    tp1_lots: float
    tp2_lots: float
    is_single_exit: bool
    tp1_price: float
    tp2_price: float
    initial_stop_price: float
    breakeven_price: float


def build_trade_exit_plan(
    side: OrderSide,
    entry_price: float,
    stop_price: float,
    total_qty: float,
    config: PairConfig,
    s_in: float = 5.0,
    s_out: float = 10.0,
    r_taker: float = 0.00055,
    r_maker: float = 0.00020,
) -> TradeExitPlan:
    """
    Computes quantized TP1 and TP2 lot distribution and trigger levels.
    """
    d = abs(entry_price - stop_price)

    # TP1 at 3R, TP2 at 5R
    if side == OrderSide.BUY:
        tp1_price = entry_price + 3.0 * d
        tp2_price = entry_price + 5.0 * d
        # Breakeven price moves to entry + entry fee + exit fee + slippage buffer
        entry_fee_pts = entry_price * r_taker
        exit_fee_pts = entry_price * r_maker
        slippage_buffer_pts = s_in
        be_price = entry_price + entry_fee_pts + exit_fee_pts + slippage_buffer_pts
    else:
        tp1_price = entry_price - 3.0 * d
        tp2_price = entry_price - 5.0 * d
        entry_fee_pts = entry_price * r_taker
        exit_fee_pts = entry_price * r_maker
        slippage_buffer_pts = s_in
        be_price = entry_price - (entry_fee_pts + exit_fee_pts + slippage_buffer_pts)

    # TP1 quantization rule: ceil(0.70 * total_qty / lot) * lot
    num_lots_tp1 = ceil(0.70 * total_qty / config.lot_size)
    tp1_lots = round(num_lots_tp1 * config.lot_size, 8)

    # If TP1 covers the full position, single exit at 3R
    if tp1_lots >= total_qty:
        tp1_lots = total_qty
        tp2_lots = 0.0
        is_single_exit = True
    else:
        tp2_lots = round(total_qty - tp1_lots, 8)
        is_single_exit = False

    return TradeExitPlan(
        tp1_lots=tp1_lots,
        tp2_lots=tp2_lots,
        is_single_exit=is_single_exit,
        tp1_price=round(tp1_price, 2),
        tp2_price=round(tp2_price, 2),
        initial_stop_price=round(stop_price, 2),
        breakeven_price=round(be_price, 2),
    )


class ExitPolicyMonitor:
    def __init__(
        self,
        side: OrderSide,
        entry_price: float,
        stop_price: float,
        exit_plan: TradeExitPlan,
    ):
        self.side = side
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.exit_plan = exit_plan
        self.d = abs(entry_price - stop_price)

        self.highest_r: float = 0.0
        self.is_early_exit_armed: bool = False
        self.is_breakeven_active: bool = False
        self.tp1_filled: bool = False

    def on_price_update(self, current_price: float) -> Tuple[str, Optional[float], str]:
        """
        Evaluates active market price against exit policy milestones:
        - Arm early exit when current >= 1.0R
        - If early exit armed and price gives back >= 50% of peak before 1.5R -> CLOSE_MARKET
        - If price touches >= 1.5R -> MOVE_STOP_BREAKEVEN
        """
        # Calculate current gain in terms of R
        if self.side == OrderSide.BUY:
            gain = current_price - self.entry_price
        else:
            gain = self.entry_price - current_price

        current_r = gain / self.d if self.d > 0 else 0.0

        if current_r > self.highest_r:
            self.highest_r = current_r

        # Milestone 1: Arm early exit at 1.0R
        if self.highest_r >= 1.0 and not self.is_early_exit_armed:
            self.is_early_exit_armed = True

        # Milestone 2: Breakeven shift at 1.5R
        if current_r >= 1.5 and not self.is_breakeven_active:
            self.is_breakeven_active = True
            return ExitActionType.MOVE_STOP_BREAKEVEN, self.exit_plan.breakeven_price, "REACHED_1_5R_BREAKEVEN_TRIGGER"

        # Early exit check: only active between 1.0R and 1.5R
        if self.is_early_exit_armed and not self.is_breakeven_active:
            # Check 50% peak giveback
            if current_r <= 0.5 * self.highest_r:
                return ExitActionType.CLOSE_MARKET, current_price, "EARLY_EXIT_50_PERCENT_PEAK_GIVEBACK"

        return ExitActionType.NONE, None, ""
