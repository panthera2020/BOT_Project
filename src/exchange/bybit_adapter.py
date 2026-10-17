"""
Bybit V5 REST & WebSocket Exchange Adapter.
Supports Bybit Demo Trading (api-demo.bybit.com) and Live Mainnet (api.bybit.com).
"""
import time
import hmac
import hashlib
import json
import httpx
from typing import Optional, Dict, Any, List
from src.exchange.adapter import ExchangeAdapter


class BybitV5Adapter(ExchangeAdapter):
    DEMO_BASE_URL = "https://api-demo.bybit.com"
    LIVE_BASE_URL = "https://api.bybit.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        is_demo: bool = True,
        recv_window: int = 5000,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.is_demo = is_demo
        self.recv_window = recv_window
        self.base_url = self.DEMO_BASE_URL if is_demo else self.LIVE_BASE_URL

    def _generate_signature(self, timestamp: str, payload_str: str) -> str:
        param_str = f"{timestamp}{self.api_key}{self.recv_window}{payload_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _get_headers(self, payload_str: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        signature = self._generate_signature(timestamp, payload_str)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": str(self.recv_window),
            "Content-Type": "application/json",
        }

    async def get_candles(self, symbol: str, interval: str = "15", limit: int = 50) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/v5/market/kline"
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if data.get("retCode") == 0:
                # Bybit returns: [startTime, open, high, low, close, volume, turnover]
                return data.get("result", {}).get("list", [])
            return []

    async def get_balance(self) -> float:
        url = f"{self.base_url}/v5/account/wallet-balance"
        params = "accountType=UNIFIED"
        headers = self._get_headers(params)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{url}?{params}", headers=headers)
            data = resp.json()
            if data.get("retCode") == 0:
                coins = data.get("result", {}).get("list", [{}])[0].get("coin", [])
                for coin in coins:
                    if coin.get("coin") == "USDT":
                        return float(coin.get("walletBalance", 0.0))
            return 0.0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        url = f"{self.base_url}/v5/position/set-leverage"
        payload = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        payload_str = json.dumps(payload)
        headers = self._get_headers(payload_str)
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, content=payload_str)
            data = resp.json()
            # RetCode 0 or 110043 (leverage not modified) is considered OK
            return data.get("retCode") in [0, 110043]

    async def place_order(self, order_req: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/v5/order/create"
        payload = {
            "category": "linear",
            "symbol": order_req["symbol"],
            "side": order_req["side"],
            "orderType": order_req.get("order_type", "Market"),
            "qty": str(order_req["qty"]),
            "reduceOnly": order_req.get("reduce_only", False),
            "isLeverage": 1,
        }
        if "price" in order_req and order_req.get("order_type") == "Limit":
            payload["price"] = str(order_req["price"])

        payload_str = json.dumps(payload)
        headers = self._get_headers(payload_str)
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, content=payload_str)
            return resp.json()

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        url = f"{self.base_url}/v5/order/cancel"
        payload = {"category": "linear", "symbol": symbol, "orderId": order_id}
        payload_str = json.dumps(payload)
        headers = self._get_headers(payload_str)
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, content=payload_str)
            return resp.json().get("retCode") == 0

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/v5/position/list"
        params = f"category=linear&symbol={symbol}"
        headers = self._get_headers(params)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{url}?{params}", headers=headers)
            data = resp.json()
            if data.get("retCode") == 0:
                positions = data.get("result", {}).get("list", [])
                for pos in positions:
                    if float(pos.get("size", 0.0)) > 0:
                        return pos
            return None
