"""
FastAPI administration console, credential management, demo qualification gate, and kill switch.
"""
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from src.core.daily_cap import get_trading_day_id


app = FastAPI(title="Trading Bot Operations Console", version="1.0.0")
security = HTTPBasic()

# In-memory session mock for administrative credentials (production binds to hashed DB records)
ADMIN_USER = "admin@tradingbot.local"
ADMIN_PASS = "P@ssword123!"

# Global runtime state container
RUNTIME_STATE = {
    "is_bot_running": True,
    "current_mode": "DEMO",  # DEMO or LIVE
    "is_live_trading_unlocked": False,
    "demo_run_start_time": datetime.now(timezone.utc),
    "clean_demo_trade_count": 0,
    "bybit_demo_keys": {"api_key": "", "api_secret": ""},
    "bybit_live_keys": {"api_key": "", "api_secret": ""},
    "kill_switch_triggered": False,
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


@app.get("/api/status")
async def get_system_status(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    # Check demo qualification gate: >= 24 hours AND >= 20 clean trades
    now = datetime.now(timezone.utc)
    demo_duration = now - RUNTIME_STATE["demo_run_start_time"]
    has_24h = demo_duration >= timedelta(hours=24)
    has_20_trades = RUNTIME_STATE["clean_demo_trade_count"] >= 20
    is_qualified = has_24h and has_20_trades

    RUNTIME_STATE["is_live_trading_unlocked"] = is_qualified

    return {
        "status": "RUNNING" if RUNTIME_STATE["is_bot_running"] else "STOPPED",
        "current_mode": RUNTIME_STATE["current_mode"],
        "kill_switch_active": RUNTIME_STATE["kill_switch_triggered"],
        "demo_qualification": {
            "elapsed_hours": round(demo_duration.total_seconds() / 3600.0, 2),
            "clean_trades": RUNTIME_STATE["clean_demo_trade_count"],
            "has_24h_run": has_24h,
            "has_20_clean_trades": has_20_trades,
            "is_live_unlocked": is_qualified,
        },
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


@app.post("/api/live/enable")
async def enable_live_trading(user: str = Depends(authenticate_admin)) -> Dict[str, str]:
    # Strictly enforce 24-hour and 20 clean demo trades gate
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

    RUNTIME_STATE["current_mode"] = "LIVE"
    return {"status": "SUCCESS", "message": "Live trading mode activated."}


@app.post("/api/emergency/kill")
async def trigger_emergency_kill(user: str = Depends(authenticate_admin)) -> Dict[str, str]:
    RUNTIME_STATE["is_bot_running"] = False
    RUNTIME_STATE["kill_switch_triggered"] = True
    return {"status": "HALTED", "message": "Kill switch engaged. System halted and new orders blocked."}
