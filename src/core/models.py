"""
Core domain models and configuration schemas for Trading Bot.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class OrderSide(str, Enum):
    BUY = "Buy"
    SELL = "Sell"


class TrendDirection(str, Enum):
    UP = "Up"
    DOWN = "Down"
    RANGE = "Range"


@dataclass(frozen=True)
class PairConfig:
    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: float
    lot_size: float
    min_order_qty: float
    min_notional_value: float
    default_stop_buffer: float = 15.0
    max_exchange_leverage: int = 100
    is_active: bool = True


# Standard default config for BTCUSDT perpetual
BTCUSDT_CONFIG = PairConfig(
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    tick_size=0.1,
    lot_size=0.001,
    min_order_qty=0.001,
    min_notional_value=100.0,
    default_stop_buffer=15.0,
    max_exchange_leverage=100,
    is_active=True,
)
