"""
Unit tests for ExchangeAdapter interface and FakeExchangeAdapter order lifecycle.
"""
import pytest
from src.exchange.adapter import FakeExchangeAdapter


@pytest.mark.asyncio
async def test_fake_exchange_lifecycle():
    adapter = FakeExchangeAdapter(initial_balance=500.0)
    assert await adapter.get_balance() == 500.0

    # Set leverage
    assert await adapter.set_leverage("BTCUSDT", 50)
    assert adapter.leverage_settings["BTCUSDT"] == 50

    # Place entry buy order
    entry_req = {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "qty": 0.004,
        "price": 100000.0,
        "order_type": "Market",
        "reduce_only": False,
    }
    res = await adapter.place_order(entry_req)
    assert res["status"] == "FILLED"

    pos = await adapter.get_position("BTCUSDT")
    assert pos is not None
    assert pos["qty"] == 0.004

    # Place TP1 partial exit (reduce-only)
    tp1_req = {
        "symbol": "BTCUSDT",
        "side": "Sell",
        "qty": 0.003,
        "price": 103000.0,
        "reduce_only": True,
    }
    await adapter.place_order(tp1_req)
    pos_after_tp1 = await adapter.get_position("BTCUSDT")
    assert pos_after_tp1["qty"] == 0.001

    # Place TP2 full exit (reduce-only)
    tp2_req = {
        "symbol": "BTCUSDT",
        "side": "Sell",
        "qty": 0.001,
        "price": 105000.0,
        "reduce_only": True,
    }
    await adapter.place_order(tp2_req)
    assert await adapter.get_position("BTCUSDT") is None
