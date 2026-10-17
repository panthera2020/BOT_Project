"""
FastAPI administration console, credential management, demo qualification gate,
trade stats for Demo and Live, and autonomous testing engine.
"""
import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.core.daily_cap import get_trading_day_id
from src.core.trade_stats import TradeStatsTracker

app = FastAPI(title="Trading Bot Operations Console", version="1.1.0")
security = HTTPBasic()

ADMIN_USER = "admin@tradingbot.local"
ADMIN_PASS = "P@ssword123!"

# Global trade tracker instance
STATS_TRACKER = TradeStatsTracker()

# Pre-populate 1 seed simulated trade to demonstrate immediate stats in demo
STATS_TRACKER.record_completed_trade(
    mode="DEMO",
    symbol="BTCUSDT",
    side="Buy",
    qty=0.002,
    entry_price=99655.0,
    exit_price=100735.0,
    pnl_usd=2.16,
    pnl_r=2.16,
)

# Global runtime state container
RUNTIME_STATE = {
    "is_bot_running": True,
    "demo_testing_active": False,
    "live_trading_active": False,
    "current_mode": "DEMO",  # DEMO or LIVE
    "is_live_trading_unlocked": False,
    "demo_run_start_time": datetime.now(timezone.utc),
    "clean_demo_trade_count": 0,
    "bybit_demo_keys": {"api_key": "", "api_secret": ""},
    "bybit_live_keys": {"api_key": "", "api_secret": ""},
    "kill_switch_triggered": False,
    "active_position": None,  # Simulated active position
}


def authenticate_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrative credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class APIKeyPayload(BaseModel):
    account_type: str  # "demo" or "live"
    api_key: str
    api_secret: str


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Trading Bot Dashboard Template Missing</h1>", status_code=500)


@app.get("/api/status")
async def get_system_status(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    demo_duration = now - RUNTIME_STATE["demo_run_start_time"]
    has_24h = demo_duration >= timedelta(hours=24)
    has_20_trades = RUNTIME_STATE["clean_demo_trade_count"] >= 20
    is_qualified = has_24h and has_20_trades

    RUNTIME_STATE["is_live_trading_unlocked"] = is_qualified

    demo_stats = STATS_TRACKER.get_stats("DEMO")
    live_stats = STATS_TRACKER.get_stats("LIVE")
    recent_trades = STATS_TRACKER.get_recent_trades(10)

    return {
        "status": "RUNNING" if RUNTIME_STATE["is_bot_running"] else "STOPPED",
        "demo_testing_active": RUNTIME_STATE["demo_testing_active"],
        "live_trading_active": RUNTIME_STATE["live_trading_active"],
        "current_mode": RUNTIME_STATE["current_mode"],
        "kill_switch_active": RUNTIME_STATE["kill_switch_triggered"],
        "active_position": RUNTIME_STATE["active_position"],
        "demo_qualification": {
            "elapsed_hours": round(demo_duration.total_seconds() / 3600.0, 2),
            "clean_trades": RUNTIME_STATE["clean_demo_trade_count"],
            "has_24h_run": has_24h,
            "has_20_clean_trades": has_20_trades,
            "is_live_unlocked": is_qualified,
        },
        "stats": {
            "demo": demo_stats,
            "live": live_stats,
        },
        "recent_trades": recent_trades,
        "keys_configured": {
            "demo": bool(RUNTIME_STATE["bybit_demo_keys"]["api_key"]),
            "live": bool(RUNTIME_STATE["bybit_live_keys"]["api_key"]),
        },
    }


@app.post("/api/keys")
async def save_api_keys(payload: APIKeyPayload, user: str = Depends(authenticate_admin)) -> Dict[str, str]:
    if payload.account_type.lower() == "demo":
        RUNTIME_STATE["bybit_demo_keys"]["api_key"] = payload.api_key
        RUNTIME_STATE["bybit_demo_keys"]["api_secret"] = payload.api_secret
        return {"status": "Demo keys stored successfully"}
    elif payload.account_type.lower() == "live":
        RUNTIME_STATE["bybit_live_keys"]["api_key"] = payload.api_key
        RUNTIME_STATE["bybit_live_keys"]["api_secret"] = payload.api_secret
        return {"status": "Live keys stored successfully"}
    else:
        raise HTTPException(status_code=400, detail="Invalid account_type. Must be 'demo' or 'live'")


@app.post("/api/demo/start")
async def start_demo_testing(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    """
    Starts unrestricted demo strategy testing (NO daily cap).
    """
    RUNTIME_STATE["demo_testing_active"] = True
    RUNTIME_STATE["current_mode"] = "DEMO"
    RUNTIME_STATE["is_bot_running"] = True
    RUNTIME_STATE["kill_switch_triggered"] = False
    return {"status": "SUCCESS", "message": "Demo Testing Started! Unrestricted trade cap active."}


@app.post("/api/demo/stop")
async def stop_demo_testing(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    RUNTIME_STATE["demo_testing_active"] = False
    return {"status": "PAUSED", "message": "Demo Testing Paused."}


@app.post("/api/live/start")
@app.post("/api/live/enable")
async def start_live_trading(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    """
    Starts Live Trading. Strictly locked until demo tests >= 24h and >= 20 clean trades.
    Strictly enforces 2 trades per day cap.
    """
    now = datetime.now(timezone.utc)
    demo_duration = now - RUNTIME_STATE["demo_run_start_time"]
    has_24h = demo_duration >= timedelta(hours=24)
    has_20_trades = RUNTIME_STATE["clean_demo_trade_count"] >= 20

    if not (has_24h and has_20_trades):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Live Trading Locked! Requires >= 24h demo runtime and >= 20 clean trades. "
                   f"Current: {round(demo_duration.total_seconds() / 3600.0, 1)}h / {RUNTIME_STATE['clean_demo_trade_count']} trades.",
        )

    RUNTIME_STATE["live_trading_active"] = True
    RUNTIME_STATE["current_mode"] = "LIVE"
    RUNTIME_STATE["is_bot_running"] = True
    RUNTIME_STATE["kill_switch_triggered"] = False
    return {"status": "SUCCESS", "message": "Live Trading Active! Enforcing 2 trades/day cap."}


@app.post("/api/live/stop")
async def stop_live_trading(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    RUNTIME_STATE["live_trading_active"] = False
    return {"status": "PAUSED", "message": "Live Trading Paused."}


@app.post("/api/emergency/kill")
async def trigger_emergency_kill(user: str = Depends(authenticate_admin)) -> Dict[str, str]:
    RUNTIME_STATE["is_bot_running"] = False
    RUNTIME_STATE["demo_testing_active"] = False
    RUNTIME_STATE["live_trading_active"] = False
    RUNTIME_STATE["kill_switch_triggered"] = True
    RUNTIME_STATE["active_position"] = None
    return {"status": "HALTED", "message": "Kill switch engaged. System halted and new orders blocked."}
