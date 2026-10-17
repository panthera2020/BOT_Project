"""
Pre-trade friction and Net Reward-to-Risk (Net RR) gate logic.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NetRRResult:
    passes_gate: bool
    net_rr: float
    friction_cost_w: float
    loss_per_unit_l: float
    min_required_rr: float = 2.0
    rejection_reason: str = ""


def calculate_net_rr(
    nominal_distance_d: float,
    loss_per_unit_l: float,
    entry_price: float,
    s_in: float = 5.0,
    r_taker: float = 0.00055,
    r_maker: float = 0.00020,
    min_net_rr: float = 2.0,
) -> NetRRResult:
    """
    Computes Net RR:
    w = entry_fee + entry_slippage + maker_exit_fee (per unit)
    w = (entry_price * r_taker) + s_in + ((entry_price + 3*d) * r_maker)
    net_rr = (3*d - w) / loss_per_unit_l
    """
    if loss_per_unit_l <= 0:
        return NetRRResult(
            passes_gate=False,
            net_rr=0.0,
            friction_cost_w=0.0,
            loss_per_unit_l=loss_per_unit_l,
            min_required_rr=min_net_rr,
            rejection_reason="INVALID_LOSS_PER_UNIT",
        )

    # Friction cost per unit
    entry_fee = entry_price * r_taker
    exit_price_tp1 = entry_price + 3.0 * nominal_distance_d
    maker_exit_fee = exit_price_tp1 * r_maker
    w = entry_fee + s_in + maker_exit_fee

    net_payoff = 3.0 * nominal_distance_d - w
    net_rr = round(net_payoff / loss_per_unit_l, 4)

    passes = net_rr >= min_net_rr
    reason = "" if passes else f"NET_RR_BELOW_GATE ({net_rr} < {min_net_rr})"

    return NetRRResult(
        passes_gate=passes,
        net_rr=net_rr,
        friction_cost_w=round(w, 4),
        loss_per_unit_l=loss_per_unit_l,
        min_required_rr=min_net_rr,
        rejection_reason=reason,
    )
