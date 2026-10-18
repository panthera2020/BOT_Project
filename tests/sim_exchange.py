"""
A small scripted stand-in for Bybit used to test the demo engine: it serves candles,
keeps one position with an exchange-side stop and resting take-profit orders, fills them
when the test moves the price, and publishes closed-PnL rows like Bybit does.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

MIN = 60_000
T0 = datetime(2026, 10, 18, 12, 10, 0, tzinfo=timezone.utc)  # "now": 12:10 UTC
SWEEP_END_MS = int(datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


class Clock:
    def __init__(self, now: datetime = T0):
        self.now_dt = now

    def now(self) -> datetime:
        return self.now_dt

    def advance(self, seconds: float) -> None:
        self.now_dt += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


def _row(ts: int, o: float, h: float, l: float, c: float) -> List[str]:
    return [str(ts), str(o), str(h), str(l), str(c), "1", "1"]


def zigzag(start_ms: int, step_ms: int, pivots: List[float], per_leg: int = 5, wick: float = 3.0) -> List[List[float]]:
    """Candles that walk linearly between pivot prices, so every pivot is a clean swing
    high or low: the candle that ends a leg pokes `wick` beyond the pivot, the one that
    starts the next leg does not. Returns [ts, open, high, low, close] oldest first."""
    prices = [pivots[0]]
    ends = set()
    for a, b in zip(pivots, pivots[1:]):
        prices += [a + (b - a) * k / per_leg for k in range(1, per_leg + 1)]
        ends.add(len(prices) - 1)
    out = []
    for i in range(len(prices) - 1):
        o, c = prices[i], prices[i + 1]
        high, low = max(o, c), min(o, c)
        if (i + 1) in ends and (i + 1) != len(prices) - 1:
            if c > o:
                high += wick
            else:
                low -= wick
        out.append([start_ms + i * step_ms, o, high, low, c])
    return out


def default_markets(sweep_low: float = 100700.0, climb_start: float = 101900.0, climb_step: float = 40.0) -> Dict[str, List[List[str]]]:
    """15m/1h/1m candles (newest first, forming candle included) with an UP trend and a
    15m sweep of the swing low at 101200 that closed at 12:00 UTC, followed by rising 1m candles."""
    # 15m: rising zigzag, then H=102800 and three falling candles, then the sweeping candle.
    pivots15 = [100000, 101500, 100400, 102000, 101000, 102500, 101200, 102800]
    legs = zigzag(0, 15 * MIN, pivots15)
    after_high = [102500.0, 102300.0, 102200.0]  # lower highs after the 102800 swing high
    n_pre = len(legs) + len(after_high)
    first_ts = SWEEP_END_MS - (n_pre + 1) * 15 * MIN  # the sweep candle is the one before 12:00
    legs = zigzag(first_ts, 15 * MIN, pivots15)
    ts = legs[-1][0] + 15 * MIN
    for hi in after_high:
        legs.append([ts, hi, hi, hi - 40, hi - 20])
        ts += 15 * MIN
    sweep = [ts, 102200.0, 102250.0, sweep_low, 101900.0]  # dips under 101200, closes back above
    assert sweep[0] + 15 * MIN == SWEEP_END_MS, (sweep[0], SWEEP_END_MS)
    legs.append(sweep)
    c15 = [_row(int(c[0]), c[1], c[2], c[3], c[4]) for c in legs]
    c15.append(_row(SWEEP_END_MS, 101900, 102000, 101890, 101950))  # forming candle

    # 1h: rising zigzag (UP).
    h1 = zigzag(SWEEP_END_MS - 40 * 60 * MIN, 60 * MIN, [98000, 99500, 98800, 100500, 99800, 101500, 100900, 102200], per_leg=4)
    c1h = [_row(int(c[0]), c[1], c[2], c[3], c[4]) for c in h1]
    c1h.append(_row(SWEEP_END_MS - 60 * MIN + 0, 102000, 102100, 101900, 102050))

    # 1m: filler before the sweep end, then a steady climb (every candle bullish, none under 100700).
    c1 = []
    ts = SWEEP_END_MS - 60 * MIN
    price = 101950.0
    while ts < SWEEP_END_MS:
        c1.append([ts, price, price + 30, price - 30, price + 5]); ts += MIN; price += 5
    price = climb_start
    while ts < SWEEP_END_MS + 10 * MIN:
        c1.append([ts, price, price + climb_step + 5, price - 5, price + climb_step]); ts += MIN; price += climb_step
    c1 = [_row(int(c[0]), c[1], c[2], c[3], c[4]) for c in c1]
    c1.append(_row(SWEEP_END_MS + 10 * MIN, price, price + 5, price - 5, price + 1))  # forming
    return {"15": c15[::-1], "60": c1h[::-1], "1": c1[::-1]}


class SimExchange:
    def __init__(self, balance: float = 1000.0, markets: Optional[Dict[str, List[List[str]]]] = None, clock: Optional[Clock] = None):
        self.balance = balance
        self.markets = markets or default_markets()
        self.clock = clock or Clock()
        self.price = 102300.0
        self.position: Optional[Dict[str, float]] = None
        self.orders: List[Dict[str, Any]] = []
        self.closed: List[Dict[str, Any]] = []
        self.placed: List[Dict[str, Any]] = []
        self.leverage: Optional[int] = None
        self.fail = False
        self.reject_limits = False
        self.reject_market = False
        self.ignore_stop_on_order = False
        self.hide_closed_pnl = 0  # number of closed-pnl calls that return nothing (Bybit lag)

    # ---- helpers for tests
    def _check(self):
        if self.fail:
            raise RuntimeError("simulated network failure")

    def _close(self, qty: float, price: float) -> None:
        pos = self.position
        qty = min(qty, pos["size"])
        self.closed.append({
            "symbol": "BTCUSDT", "closedSize": str(qty), "avgEntryPrice": str(pos["avg"]),
            "avgExitPrice": str(price), "closedPnl": str((price - pos["avg"]) * qty),
            "updatedTime": str(int(self.clock.now().timestamp() * 1000)),
        })
        pos["size"] = round(pos["size"] - qty, 8)
        if pos["size"] <= 0:
            self.position = None
            self.orders = []

    def move_price(self, price: float) -> None:
        """Move the market; fill the exchange-side stop and take-profit orders like Bybit would."""
        self.price = price
        if self.position and self.position.get("stop") and price <= self.position["stop"]:
            self._close(self.position["size"], self.position["stop"])
        for order in list(self.orders):
            if self.position and order["price"] <= price:
                self.orders.remove(order)
                self._close(order["qty"], order["price"])

    # ---- the adapter interface
    async def get_balance(self) -> float:
        self._check()
        return self.balance

    async def get_candles(self, symbol: str, interval: str = "15", limit: int = 50):
        self._check()
        return self.markets[interval][:limit]

    async def get_position(self, symbol: str):
        self._check()
        if not self.position:
            return None
        return {"size": str(self.position["size"]), "avgPrice": str(self.position["avg"]),
                "markPrice": str(self.price), "stopLoss": str(self.position.get("stop") or "")}

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        self._check()
        self.leverage = leverage
        return True

    async def place_order(self, req: Dict[str, Any]) -> Dict[str, Any]:
        self._check()
        self.placed.append(dict(req))
        if req.get("order_type", "Market") == "Limit":
            if self.reject_limits:
                return {"retCode": 10001, "retMsg": "simulated limit rejection"}
            self.orders.append({"qty": req["qty"], "price": req["price"], "reduce_only": req.get("reduce_only", False), "side": req["side"]})
            return {"retCode": 0, "result": {"orderId": f"o{len(self.orders)}"}}
        if self.reject_market:
            return {"retCode": 10001, "retMsg": "simulated rejection"}
        if req["side"] == "Buy":
            stop = None if self.ignore_stop_on_order else req.get("stop_loss")
            self.position = {"size": req["qty"], "avg": self.price, "stop": stop}
        elif self.position:
            self._close(req["qty"], self.price)
        return {"retCode": 0, "result": {"orderId": "m"}}

    async def set_position_stop(self, symbol: str, stop_price: float) -> bool:
        self._check()
        if self.position:
            self.position["stop"] = stop_price
        return True

    async def cancel_all_orders(self, symbol: str) -> bool:
        self._check()
        self.orders = []
        return True

    async def get_closed_pnl(self, symbol: str, start_ms: int = 0, limit: int = 50):
        self._check()
        if self.hide_closed_pnl > 0:
            self.hide_closed_pnl -= 1
            return []
        return [r for r in self.closed if int(r["updatedTime"]) >= start_ms]
