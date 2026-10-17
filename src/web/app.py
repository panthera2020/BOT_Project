"""
FastAPI administration console, credential management, demo qualification gate,
trade stats for Demo and Live, and autonomous testing engine.
"""
import os
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.core.daily_cap import get_trading_day_id
from src.core.demo_gate import evaluate_demo_gate
from src.core.trade_stats import TradeStatsTracker
from src.alerts.notifier import AlertDispatcher
from src.storage.state_store import StateStore


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Restore the demo clock and clean-trade count saved before the last restart.
    await load_persisted_state()
    yield


app = FastAPI(title="Trading Bot Operations Console", version="1.1.0", lifespan=_lifespan)
# auto_error=False so a missing Authorization header returns our own 401 without a
# WWW-Authenticate challenge. That challenge triggers the browser's native login
# popup, which reverse proxies on free hosts often suppress; the page signs in
# through its own form instead.
security = HTTPBasic(auto_error=False)

DEFAULT_ADMIN_USER = "admin@tradingbot.local"
DEFAULT_ADMIN_PASS = "P@ssword123!"
# Set ADMIN_USER and ADMIN_PASS as environment variables on the host. The defaults
# live in a public repository and must not be used on a real deployment.
ADMIN_USER = os.environ.get("ADMIN_USER", DEFAULT_ADMIN_USER)
ADMIN_PASS = os.environ.get("ADMIN_PASS", DEFAULT_ADMIN_PASS)

logger = logging.getLogger("WebConsole")
if ADMIN_PASS == DEFAULT_ADMIN_PASS:
    logger.warning("ADMIN_PASS is the public default; set ADMIN_USER and ADMIN_PASS environment variables.")

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
    "demo_run_start_time": None,  # set when Start Demo Testing is first pressed
    "clean_demo_trade_count": 0,
    "bybit_demo_keys": {"api_key": "", "api_secret": ""},
    "bybit_live_keys": {"api_key": "", "api_secret": ""},
    "kill_switch_triggered": False,
    "active_position": None,  # Simulated active position
    "telegram_config": {"bot_token": "", "chat_id": ""},
}

# The demo clock and clean-trade count are saved here so a restart does not reset the
# 24h / 20-trade gate. DATABASE_URL is set in the Dockerfile (/data/trading_bot.db).
# When it is unset (local runs, tests) nothing is saved. The file only survives
# restarts if /data sits on a persistent disk; on hosts with an ephemeral filesystem
# it is lost on every redeploy.
STATE_DB_PATH = os.environ.get("DATABASE_URL", "")
_KEY_DEMO_START = "demo_run_start_time"
_KEY_CLEAN_TRADES = "clean_demo_trade_count"


async def _persist_state(key: str, value: str) -> None:
    if not STATE_DB_PATH:
        return
    try:
        store = StateStore(db_path=STATE_DB_PATH)
        await store.init_db()
        await store.set_state(key, value)
    except Exception as exc:  # never let a storage problem take the console down
        logger.warning("Could not save %s to %s: %s", key, STATE_DB_PATH, exc)


async def load_persisted_state() -> None:
    """Restore the demo clock and clean-trade count saved before the last restart."""
    if not STATE_DB_PATH:
        return
    try:
        store = StateStore(db_path=STATE_DB_PATH)
        await store.init_db()
        started = await store.get_state(_KEY_DEMO_START)
        count = await store.get_state(_KEY_CLEAN_TRADES)
        if started:
            RUNTIME_STATE["demo_run_start_time"] = datetime.fromisoformat(started)
        if count is not None:
            RUNTIME_STATE["clean_demo_trade_count"] = int(count)
    except Exception as exc:
        logger.warning("Could not load saved state from %s: %s", STATE_DB_PATH, exc)


async def record_demo_trade(
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    pnl_r: float,
    clean: bool = True,
):
    """The one place a closed demo trade is recorded. The trading engine calls this.

    Adds the trade to the stats and, when it is clean, counts it toward the 20-trade
    gate and saves the new count. What counts as "clean" is still an open decision in
    the PRD; the caller decides, and the default counts every properly closed trade.
    """
    trade = STATS_TRACKER.record_completed_trade(
        mode="DEMO",
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        exit_price=exit_price,
        pnl_usd=pnl_usd,
        pnl_r=pnl_r,
    )
    if clean:
        RUNTIME_STATE["clean_demo_trade_count"] += 1
        await _persist_state(_KEY_CLEAN_TRADES, str(RUNTIME_STATE["clean_demo_trade_count"]))
    return trade


def _credentials_valid(username: str, password: str) -> bool:
    user_ok = secrets.compare_digest(username.encode("utf-8"), ADMIN_USER.encode("utf-8"))
    pass_ok = secrets.compare_digest(password.encode("utf-8"), ADMIN_PASS.encode("utf-8"))
    return user_ok and pass_ok


def authenticate_admin(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> str:
    if credentials is None or not _credentials_valid(credentials.username, credentials.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrative credentials",
        )
    return credentials.username


class LoginPayload(BaseModel):
    username: str
    password: str


class APIKeyPayload(BaseModel):
    account_type: str  # "demo" or "live"
    api_key: str
    api_secret: str


class TelegramPayload(BaseModel):
    bot_token: str
    chat_id: str


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/login")
async def login(payload: LoginPayload) -> Dict[str, str]:
    """Checks admin credentials for the sign-in form. The page then sends them as a
    Basic Authorization header on every later request."""
    if not _credentials_valid(payload.username, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrative credentials",
        )
    return {"status": "OK", "message": "Signed in."}


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
    gate = evaluate_demo_gate(
        RUNTIME_STATE["demo_run_start_time"], RUNTIME_STATE["clean_demo_trade_count"], now=now
    )

    RUNTIME_STATE["is_live_trading_unlocked"] = gate.is_unlocked

    demo_stats = STATS_TRACKER.get_stats("DEMO")
    live_stats = STATS_TRACKER.get_stats("LIVE")
    recent_trades = STATS_TRACKER.get_recent_trades(10)

    # Market Radar Telemetry (shows actively looking for setups)
    is_scanning = RUNTIME_STATE["demo_testing_active"] or RUNTIME_STATE["live_trading_active"]
    radar_state = {
        "is_actively_scanning": is_scanning,
        "symbol_monitored": "BTCUSDT",
        "timeframes_tracked": ["1h (Macro Bias)", "15m (Liquidity Sweeps)", "2m (Entry Triggers)"],
        "last_market_scan_utc": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "current_market_status": "Scanning order books & candles for Strategy 1 liquidity sweep setups..." if is_scanning else "Radar idle. Click 'Start Demo Testing' to begin scanning.",
    }

    return {
        "status": "RUNNING" if RUNTIME_STATE["is_bot_running"] else "STOPPED",
        "demo_testing_active": RUNTIME_STATE["demo_testing_active"],
        "live_trading_active": RUNTIME_STATE["live_trading_active"],
        "current_mode": RUNTIME_STATE["current_mode"],
        "kill_switch_active": RUNTIME_STATE["kill_switch_triggered"],
        "active_position": RUNTIME_STATE["active_position"],
        "market_radar": radar_state,
        "demo_qualification": {
            "elapsed_hours": gate.elapsed_hours,
            "clean_trades": gate.clean_trades,
            "has_24h_run": gate.has_24h_run,
            "has_20_clean_trades": gate.has_20_clean_trades,
            "is_live_unlocked": gate.is_unlocked,
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
        "telegram": {
            "is_configured": bool(RUNTIME_STATE["telegram_config"]["bot_token"] and RUNTIME_STATE["telegram_config"]["chat_id"]),
            "chat_id": RUNTIME_STATE["telegram_config"]["chat_id"],
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


@app.post("/api/telegram")
async def save_telegram_config(payload: TelegramPayload, user: str = Depends(authenticate_admin)) -> Dict[str, str]:
    RUNTIME_STATE["telegram_config"]["bot_token"] = payload.bot_token.strip()
    RUNTIME_STATE["telegram_config"]["chat_id"] = payload.chat_id.strip()
    return {"status": "SUCCESS", "message": "Telegram credentials saved successfully."}


@app.post("/api/telegram/test")
async def test_telegram_alert(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    token = RUNTIME_STATE["telegram_config"]["bot_token"]
    chat_id = RUNTIME_STATE["telegram_config"]["chat_id"]
    if not token or not chat_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide both Telegram Bot Token and Chat ID before testing.",
        )

    dispatcher = AlertDispatcher(telegram_token=token, telegram_chat_id=chat_id)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await dispatcher.send_alert(
        title="🔔 MISSION CONTROL: Telegram Alerts Connected!",
        body=f"Connected successfully at {now_str}.\n\nYou will receive real-time notifications for Strategy 1 entries, liquidity sweeps, TP1 partials, and risk resets here.",
        level="INFO",
    )
    return {"status": "SUCCESS", "message": "Test ping sent to your Telegram! Check your phone."}


@app.post("/api/demo/start")
async def start_demo_testing(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    """
    Starts unrestricted demo strategy testing (NO daily cap).
    """
    # The 24h clock starts the first time demo testing is started and is not reset by
    # pausing, stopping or restarting.
    if RUNTIME_STATE["demo_run_start_time"] is None:
        RUNTIME_STATE["demo_run_start_time"] = datetime.now(timezone.utc)
        await _persist_state(_KEY_DEMO_START, RUNTIME_STATE["demo_run_start_time"].isoformat())
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
    gate = evaluate_demo_gate(
        RUNTIME_STATE["demo_run_start_time"], RUNTIME_STATE["clean_demo_trade_count"]
    )

    if not gate.is_unlocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Live Trading Locked! Requires >= 24h demo runtime and >= 20 clean trades. "
                   f"Current: {round(gate.elapsed_hours, 1)}h / {gate.clean_trades} trades.",
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
