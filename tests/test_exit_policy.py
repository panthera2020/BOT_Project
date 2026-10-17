"""
Unit tests for the Exit Policy engine, TP1/TP2 quantization, breakeven, and early exit rules.
"""
import pytest
from src.core.models import OrderSide, BTCUSDT_CONFIG
from src.core.exit_policy import (
    build_trade_exit_plan,
    ExitPolicyMonitor,
    ExitActionType,
)


def test_tp1_lot_quantization_ceil_rule():
    """
    Test PRD Lot Split rules:
    - 2 lots (0.002): ceil(0.7 * 2) = 2 -> Single exit
    - 3 lots (0.003): ceil(0.7 * 3) = 3 -> Single exit
    - 4 lots (0.004): ceil(0.7 * 4) = 3 TP1 + 1 TP2
    - 5 lots (0.005): ceil(0.7 * 5) = 4 TP1 + 1 TP2
    - 10 lots (0.010): ceil(0.7 * 10) = 7 TP1 + 3 TP2
    """
    # 2 lots
    p2 = build_trade_exit_plan(OrderSide.BUY, 100000.0, 99640.0, 0.002, BTCUSDT_CONFIG)
    assert p2.is_single_exit
    assert p2.tp1_lots == 0.002
    assert p2.tp2_lots == 0.0

    # 3 lots
    p3 = build_trade_exit_plan(OrderSide.BUY, 100000.0, 99640.0, 0.003, BTCUSDT_CONFIG)
    assert p3.is_single_exit
    assert p3.tp1_lots == 0.003
    assert p3.tp2_lots == 0.0

    # 4 lots
    p4 = build_trade_exit_plan(OrderSide.BUY, 100000.0, 99640.0, 0.004, BTCUSDT_CONFIG)
    assert not p4.is_single_exit
    assert p4.tp1_lots == 0.003
    assert p4.tp2_lots == 0.001

    # 5 lots
    p5 = build_trade_exit_plan(OrderSide.BUY, 100000.0, 99640.0, 0.005, BTCUSDT_CONFIG)
    assert not p5.is_single_exit
    assert p5.tp1_lots == 0.004
    assert p5.tp2_lots == 0.001

    # 10 lots
    p10 = build_trade_exit_plan(OrderSide.BUY, 100000.0, 99640.0, 0.010, BTCUSDT_CONFIG)
    assert not p10.is_single_exit
    assert p10.tp1_lots == 0.007
    assert p10.tp2_lots == 0.003


def test_early_exit_50_percent_giveback():
    """
    Early exit rule:
    Armed once trade reaches 1.0R.
    If price gives back 50% of peak gain before 1.5R, closes at market.
    """
    entry = 100000.0
    stop = 99000.0  # d = 1000
    plan = build_trade_exit_plan(OrderSide.BUY, entry, stop, 0.004, BTCUSDT_CONFIG)
    monitor = ExitPolicyMonitor(OrderSide.BUY, entry, stop, plan)

    # Price moves to 0.8R (100800) -> not armed yet
    action, _, _ = monitor.on_price_update(100800.0)
    assert action == ExitActionType.NONE
    assert not monitor.is_early_exit_armed

    # Price touches 1.2R (101200) -> armed!
    action, _, _ = monitor.on_price_update(101200.0)
    assert action == ExitActionType.NONE
    assert monitor.is_early_exit_armed
    assert monitor.highest_r == 1.2

    # Price drops to 0.5R (100500) -> gave back > 50% of 1.2R (0.6R) -> triggers market close!
    action, price, reason = monitor.on_price_update(100500.0)
    assert action == ExitActionType.CLOSE_MARKET
    assert price == 100500.0
    assert reason == "EARLY_EXIT_50_PERCENT_PEAK_GIVEBACK"


def test_breakeven_move_at_1_5r():
    entry = 100000.0
    stop = 99000.0  # d = 1000
    plan = build_trade_exit_plan(OrderSide.BUY, entry, stop, 0.004, BTCUSDT_CONFIG)
    monitor = ExitPolicyMonitor(OrderSide.BUY, entry, stop, plan)

    # Price reaches 1.5R (101500)
    action, be_price, reason = monitor.on_price_update(101500.0)
    assert action == ExitActionType.MOVE_STOP_BREAKEVEN
    assert be_price == plan.breakeven_price
    assert be_price > entry  # Includes fees & slippage
    assert monitor.is_breakeven_active
