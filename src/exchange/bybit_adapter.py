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
        """Public candles, newest first: [startTime, open, high, low, close, volume, turnover].

        Market data is public and read-only, so it is read from the main Bybit host (the
        demo host does not always serve it) and falls back to the demo host. Network and
        API errors raise, so the caller can tell "no data" from "could not reach Bybit"."""
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
        last_error: Optional[Exception] = None
        for base in (self.LIVE_BASE_URL, self.DEMO_BASE_URL):
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{base}/v5/market/kline", params=params)
                    data = resp.json()
                if data.get("retCode") == 0:
                    rows = data.get("result", {}).get("list", [])
                    if rows:
                        return rows
                    last_error = RuntimeError("Bybit returned no candles")
                else:
                    last_error = RuntimeError(f"Bybit candles error {data.get('retCode')}: {data.get('retMsg')}")
            except Exception as exc:  # network, TLS, JSON...
                last_error = exc
        raise RuntimeError(f"Could not get candles: {last_error}")

    async def get_balance(self) -> float:
        """USDT wallet balance. Raises when Bybit cannot be reached or rejects the keys,
        so an outage is never mistaken for an empty account."""
        data = await self._get("/v5/account/wallet-balance", "accountType=UNIFIED")
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit balance error {data.get('retCode')}: {data.get('retMsg')}")
        accounts = data.get("result", {}).get("list", [])
        for coin in (accounts[0].get("coin", []) if accounts else []):
            if coin.get("coin") == "USDT":
                return float(coin.get("walletBalance") or 0.0)
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
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, content=payload_str)
            data = resp.json()
            # RetCode 0 or 110043 (leverage not modified) is considered OK
            return data.get("retCode") in [0, 110043]

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload_str = json.dumps(payload)
        headers = self._get_headers(payload_str)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{self.base_url}{path}", headers=headers, content=payload_str)
            return resp.json()

    async def _get(self, path: str, params: str) -> Dict[str, Any]:
        headers = self._get_headers(params)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}{path}?{params}", headers=headers)
            return resp.json()

    async def place_order(self, order_req: Dict[str, Any]) -> Dict[str, Any]:
        """Places an order. Optional order_req keys: price (Limit), reduce_only,
        stop_loss and take_profit (set on Bybit itself, so they work even if this app
        is offline). Returns Bybit's reply; the caller checks retCode == 0."""
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
        if order_req.get("stop_loss"):
            payload["stopLoss"] = str(order_req["stop_loss"])
            payload["slTriggerBy"] = "LastPrice"
        if order_req.get("take_profit"):
            payload["takeProfit"] = str(order_req["take_profit"])
            payload["tpTriggerBy"] = "LastPrice"
        if order_req.get("stop_loss") or order_req.get("take_profit"):
            payload["tpslMode"] = "Full"
        return await self._post("/v5/order/create", payload)

    async def set_position_stop(self, symbol: str, stop_price: float) -> bool:
        """Moves the stop-loss of the open position (used for the breakeven shift)."""
        data = await self._post("/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol, "positionIdx": 0,
            "tpslMode": "Full", "stopLoss": str(stop_price), "slTriggerBy": "LastPrice",
        })
        # 34040 = "not modified": the stop is already at that price
        return data.get("retCode") in (0, 34040)

    async def cancel_all_orders(self, symbol: str) -> bool:
        data = await self._post("/v5/order/cancel-all", {"category": "linear", "symbol": symbol})
        return data.get("retCode") == 0

    async def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        data = await self._get("/v5/order/realtime", f"category=linear&symbol={symbol}")
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit open orders error {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result", {}).get("list", [])

    async def get_closed_pnl(self, symbol: str, start_ms: int = 0, limit: int = 50) -> List[Dict[str, Any]]:
        params = f"category=linear&symbol={symbol}&limit={limit}"
        if start_ms:
            params += f"&startTime={int(start_ms)}"
        data = await self._get("/v5/position/closed-pnl", params)
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit closed pnl error {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result", {}).get("list", [])

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        url = f"{self.base_url}/v5/order/cancel"
        payload = {"category": "linear", "symbol": symbol, "orderId": order_id}
        payload_str = json.dumps(payload)
        headers = self._get_headers(payload_str)
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, content=payload_str)
            return resp.json().get("retCode") == 0

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """The open position, or None when flat. Raises if Bybit cannot be reached or
        answers with an error, so "flat" is never guessed."""
        data = await self._get("/v5/position/list", f"category=linear&symbol={symbol}")
        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit position error {data.get('retCode')}: {data.get('retMsg')}")
        for pos in data.get("result", {}).get("list", []):
            if float(pos.get("size") or 0.0) > 0:
                return pos
        return None
