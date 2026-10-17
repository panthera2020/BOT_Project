"""
Exchange adapter interface and backtest FakeAdapter implementation.
"""
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from src.core.models import OrderSide, PairConfig


class ExchangeAdapter(ABC):
    @abstractmethod
    async def place_order(self, order_req: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool: ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    async def get_balance(self) -> float: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool: ...


class FakeExchangeAdapter(ExchangeAdapter):
    """
    Deterministic simulated exchange adapter for historical backtests & unit tests.
    """
    def __init__(self, initial_balance: float = 1000.0):
        self.balance = initial_balance
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.leverage_settings: Dict[str, int] = {}
        self.order_counter = 1

    async def place_order(self, order_req: Dict[str, Any]) -> Dict[str, Any]:
        order_id = f"fake_ord_{self.order_counter}"
        self.order_counter += 1

        order = {
            "order_id": order_id,
            "symbol": order_req["symbol"],
            "side": order_req["side"],
            "qty": order_req["qty"],
            "price": order_req.get("price", 0.0),
            "order_type": order_req.get("order_type", "Market"),
            "reduce_only": order_req.get("reduce_only", False),
            "status": "FILLED",
        }
        self.orders[order_id] = order

        # If not reduce only, update active position
        if not order["reduce_only"]:
            self.positions[order["symbol"]] = {
                "symbol": order["symbol"],
                "side": order["side"],
                "qty": order["qty"],
                "entry_price": order["price"],
            }
        else:
            # Flatten or reduce position
            if order["symbol"] in self.positions:
                pos = self.positions[order["symbol"]]
                remaining = round(pos["qty"] - order["qty"], 8)
                if remaining <= 0:
                    del self.positions[order["symbol"]]
                else:
                    pos["qty"] = remaining

        return order

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        if order_id in self.orders:
            self.orders[order_id]["status"] = "CANCELLED"
            return True
        return False

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self.positions.get(symbol)

    async def get_balance(self) -> float:
        return self.balance

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        self.leverage_settings[symbol] = leverage
        return True
