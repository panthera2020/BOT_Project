"""Bybit rejects requests stamped >1s ahead of its clock (error 10002). The adapter must
measure the gap and stamp requests with Bybit's time."""
import time

import httpx
import pytest

from src.exchange import bybit_adapter
from src.exchange.bybit_adapter import BybitV5Adapter


class FakeBybit:
    """Bybit whose clock is `skew_ms` behind the device's, enforcing its 1s-ahead rule."""

    def __init__(self, skew_ms):
        self.skew_ms = skew_ms
        self.calls = []

    def server_ms(self):
        return time.time() * 1000 - self.skew_ms

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(path)
        if path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result": {"timeSecond": str(int(self.server_ms() // 1000)),
                                                                      "timeNano": str(int(self.server_ms() * 1_000_000))}})
        stamp = int(req.headers["X-BAPI-TIMESTAMP"])
        if not (self.server_ms() - 5000 <= stamp < self.server_ms() + 1000):
            return httpx.Response(200, json={"retCode": 10002, "retMsg": "invalid request, please check your server timestamp"})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [], "ok": True}})


@pytest.fixture
def bybit(monkeypatch):
    def install(skew_ms):
        fake = FakeBybit(skew_ms)
        real = httpx.AsyncClient
        monkeypatch.setattr(bybit_adapter.httpx, "AsyncClient",
                            lambda **kw: real(transport=httpx.MockTransport(fake.handle), **kw))
        return fake
    return install


@pytest.mark.asyncio
async def test_a_phone_clock_one_second_fast_still_works(bybit):
    fake = bybit(skew_ms=1100)
    ad = BybitV5Adapter("k", "s")
    assert await ad.get_position("BTCUSDT") is None
    assert abs(ad._clock_offset_ms + 1100) < 300


@pytest.mark.asyncio
async def test_a_slow_phone_clock_still_works(bybit):
    bybit(skew_ms=-30000)
    assert await BybitV5Adapter("k", "s").get_position("BTCUSDT") is None


@pytest.mark.asyncio
async def test_clock_is_measured_once_not_every_request(bybit):
    fake = bybit(skew_ms=1100)
    ad = BybitV5Adapter("k", "s")
    for _ in range(3):
        await ad.get_position("BTCUSDT")
    assert fake.calls.count("/v5/market/time") == 1


@pytest.mark.asyncio
async def test_clock_that_drifts_later_is_corrected_after_a_10002(bybit):
    fake = bybit(skew_ms=0)
    ad = BybitV5Adapter("k", "s")
    await ad.get_position("BTCUSDT")
    fake.skew_ms = 3000  # device clock jumps ahead after the first measurement
    assert await ad.get_position("BTCUSDT") is None
    assert fake.calls.count("/v5/market/time") == 2


@pytest.mark.asyncio
async def test_orders_and_leverage_use_the_corrected_time_too(bybit):
    bybit(skew_ms=1500)
    ad = BybitV5Adapter("k", "s")
    assert await ad.cancel_order("1", "BTCUSDT") is True
    assert await ad.set_leverage("BTCUSDT", 10) is True


@pytest.mark.asyncio
async def test_if_the_time_endpoint_is_unreachable_requests_still_go_out(monkeypatch):
    real = httpx.AsyncClient

    def handle(req):
        if req.url.path == "/v5/market/time":
            raise httpx.ConnectError("down", request=req)
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

    monkeypatch.setattr(bybit_adapter.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    assert await BybitV5Adapter("k", "s").get_position("BTCUSDT") is None


@pytest.mark.asyncio
async def test_orders_also_recover_from_a_clock_that_drifts(bybit):
    fake = bybit(skew_ms=0)
    ad = BybitV5Adapter("k", "s")
    await ad.cancel_order("1", "BTCUSDT")
    fake.skew_ms = 3000
    assert await ad.cancel_order("1", "BTCUSDT") is True
