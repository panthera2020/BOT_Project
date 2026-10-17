"""
Position sizing, loss-per-BTC friction calculations, and lot-size quantization.
"""
from dataclasses import dataclass
from math import floor
from typing import Optional
from src.core.models import PairConfig, OrderSide


@dataclass(frozen=True)
class PositionSizingResult:
    is_valid: bool
    qty: float
    notional: float
    nominal_distance_d: float
    loss_per_unit_l: float
    max_loss: float
    rejection_reason: str = ""


def calculate_loss_per_unit(
    entry_price: float,
    stop_price: float,
    s_in: float,
    s_out: float,
    r_taker: float,
) -> tuple[float, float]:
    """
    Computes nominal stop distance d and total loss per asset unit L.
    Formula from PRD:
    d = |entry_price - stop_price|
    L = d + s_in + s_out + r_taker * (entry_price + stop_price - s_out)
    """
    d = abs(entry_price - stop_price)
    # Stop-out fee per unit accounts for taker fee across entry + stop adjusted for slippage
    stop_out_fee = r_taker * (entry_price + stop_price - s_out)
    l = d + s_in + s_out + stop_out_fee
    return d, l


def calculate_position_size(
    r_trade: float,
    entry_price: float,
    stop_price: float,
    config: PairConfig,
    s_in: float = 5.0,
    s_out: float = 10.0,
    r_taker: float = 0.00055,
) -> PositionSizingResult:
    """
    Computes quantized order size strictly rounded DOWN to lot_size.
    Rejects setups where qty < lot_size or notional < min_notional_value.
    """
    d, l = calculate_loss_per_unit(entry_price, stop_price, s_in, s_out, r_taker)

    if l <= 0:
        return PositionSizingResult(
            is_valid=False,
            qty=0.0,
            notional=0.0,
            nominal_distance_d=d,
            loss_per_unit_l=l,
            max_loss=0.0,
            rejection_reason="INVALID_LOSS_PER_UNIT",
        )

    # Raw quantity from budget divided by loss per unit
    raw_qty = r_trade / l

    # Quantize strictly DOWN to lot size step
    num_lots = floor(raw_qty / config.lot_size)
    qty = round(num_lots * config.lot_size, 8)

    if qty < config.min_order_qty:
        return PositionSizingResult(
            is_valid=False,
            qty=qty,
            notional=round(qty * entry_price, 2),
            nominal_distance_d=d,
            loss_per_unit_l=l,
            max_loss=round(qty * l, 4),
            rejection_reason="QTY_BELOW_MINIMUM_LOT",
        )

    notional = round(qty * entry_price, 2)
    if notional < config.min_notional_value:
        return PositionSizingResult(
            is_valid=False,
            qty=qty,
            notional=notional,
            nominal_distance_d=d,
            loss_per_unit_l=l,
            max_loss=round(qty * l, 4),
            rejection_reason="NOTIONAL_BELOW_EXCHANGE_MINIMUM",
        )

    max_loss = round(qty * l, 4)

    return PositionSizingResult(
        is_valid=True,
        qty=qty,
        notional=notional,
        nominal_distance_d=d,
        loss_per_unit_l=l,
        max_loss=max_loss,
        rejection_reason="",
    )
