"""
FastAPI administration console, credential management, demo qualification gate,
trade stats for Demo and Live, and autonomous testing engine.
"""
import os
import json
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.core.console_state import (
    apply_resume_policy,
    has_any_secret,
    restore_plain,
    restore_secrets,
    snapshot_plain,
    snapshot_secrets,
)
from src.core.daily_cap import get_trading_day_id
from src.core.demo_gate import evaluate_demo_gate
from src.core.trade_stats import TradeStatsTracker
from src.alerts.notifier import AlertDispatcher
from src.storage.secrets_box import SecretsBox
from src.engine.demo_engine import DemoEngine
from src.exchange.bybit_adapter import BybitV5Adapter
from src.storage.state_store import StateStore


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Bring back everything saved before the last restart (flags, clock, trades, keys...).
    await load_persisted_state()
    await sync_engine()  # demo testing that was running before the restart carries on
    try:
        yield
    finally:
        await _stop_engine()


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

# Global trade tracker instance. It starts empty: stats only ever show real trades
# (a made-up seed trade used to live here and would have polluted the 24h demo test).
STATS_TRACKER = TradeStatsTracker()

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
    # What the trading engine must remember across restarts (day's starting capital, the last
    # 15m candle it looked at, an armed sweep setup). Saved with the rest of the state.
    "engine_state": {},
}

# Everything the console needs to carry on after a restart is saved to one SQLite file:
# flags, kill switch, the demo clock and clean-trade count, the open position, the
# Telegram chat id, closed trades, and (encrypted) API keys and the Telegram token.
#
#   DATABASE_URL          path of the SQLite file (the Dockerfile sets /data/trading_bot.db;
#                         on a phone or PC point it at a file on storage that survives
#                         restarts). Unset = nothing is saved (tests, quick local runs).
#   STATE_ENCRYPTION_KEY  passphrase (12+ characters) that encrypts the secrets. Without
#                         it, secrets are NEVER written to disk and must be re-entered
#                         after a restart.
#   AUTO_RESUME_LIVE      "1" lets live trading restart by itself after a restart. Off
#                         by default; demo testing always resumes by itself.
#
# The file only survives a restart if it sits on storage that survives restarts.
STATE_DB_PATH = os.environ.get("DATABASE_URL", "")
_KEY_STATE = "console_state"
_KEY_SECRETS = "console_secrets"

# Saves are serialized so two near-simultaneous changes can never write an older snapshot
# over a newer one: each save builds its snapshot only once it holds the lock.
_SAVE_LOCK = asyncio.Lock()

# What the last startup restored and what did not resume, shown in /api/status.
PERSISTENCE_INFO: Dict[str, Any] = {"resumed": [], "notes": [], "last_saved_utc": None}


@lru_cache(maxsize=4)
def _box_for(passphrase: str) -> SecretsBox:
    return SecretsBox(passphrase)  # key stretching is slow, so build it once per passphrase


def _secrets_box() -> SecretsBox:
    return _box_for(os.environ.get("STATE_ENCRYPTION_KEY", ""))


def _auto_resume_live() -> bool:
    return os.environ.get("AUTO_RESUME_LIVE", "").strip().lower() in {"1", "true", "yes"}


def _plain_json() -> str:
    return json.dumps(snapshot_plain(RUNTIME_STATE))


async def save_console_state(secrets_changed: bool = False) -> bool:
    """Save the whole console state in one all-or-nothing write. Returns True if saved.

    Never raises: a storage problem is logged and the console keeps running. Secrets are
    rewritten only when secrets_changed is True. If they cannot be encrypted, the old
    saved copy is removed (so stale keys cannot come back) and nothing secret is written.
    """
    if not STATE_DB_PATH:
        return False
    try:
        async with _SAVE_LOCK:
            store = StateStore(db_path=STATE_DB_PATH)
            await store.init_db()
            items = {_KEY_STATE: _plain_json()}
            drop_old_secrets = False
            if secrets_changed:
                box = _secrets_box()
                if box.available:
                    items[_KEY_SECRETS] = box.encrypt(json.dumps(snapshot_secrets(RUNTIME_STATE)))
                else:
                    drop_old_secrets = True
                    if has_any_secret(snapshot_secrets(RUNTIME_STATE)):
                        logger.warning(
                            "API keys / Telegram token are NOT saved to disk: %s. "
                            "They will have to be re-entered after a restart.", box.unavailable_reason,
                        )
            await store.set_many(items)
            if drop_old_secrets:
                await store.delete_state(_KEY_SECRETS)
        PERSISTENCE_INFO["last_saved_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return True
    except Exception as exc:  # never let a storage problem take the console down
        logger.warning("Could not save console state to %s: %s", STATE_DB_PATH, exc)
        return False


async def load_persisted_state() -> None:
    """Bring back everything saved before the last restart, then apply the resume policy."""
    PERSISTENCE_INFO["resumed"] = []
    PERSISTENCE_INFO["notes"] = []
    if not STATE_DB_PATH:
        logger.warning("DATABASE_URL is not set: nothing is saved, so a restart starts from scratch.")
        PERSISTENCE_INFO["notes"].append("Saving is OFF (DATABASE_URL is not set): a restart starts from scratch.")
        return

    notes: List[str] = []
    try:
        store = StateStore(db_path=STATE_DB_PATH)
        await store.init_db()

        raw = await store.get_state(_KEY_STATE)
        if raw:
            try:
                problems = restore_plain(RUNTIME_STATE, json.loads(raw))
            except ValueError:
                problems = ["saved state could not be read; starting from defaults"]
            for problem in problems:
                logger.warning("Saved state: %s", problem)
            if problems:
                notes.append("Some saved state was damaged and was skipped; see the server log.")

        blob = await store.get_state(_KEY_SECRETS)
        if blob:
            box = _secrets_box()
            if not box.available:
                notes.append(
                    f"Saved API keys exist but cannot be unlocked: {box.unavailable_reason}. "
                    "Set STATE_ENCRYPTION_KEY or re-enter the keys."
                )
            else:
                text = box.decrypt(blob)
                if text is None:
                    notes.append("Saved API keys could not be decrypted (wrong STATE_ENCRYPTION_KEY or damaged data). Re-enter the keys.")
                else:
                    try:
                        for problem in restore_secrets(RUNTIME_STATE, json.loads(text)):
                            logger.warning("Saved secrets: %s", problem)
                    except ValueError:
                        notes.append("Saved API keys were damaged. Re-enter the keys.")

        STATS_TRACKER.restore_trades(await store.load_trades())

        gate = evaluate_demo_gate(RUNTIME_STATE["demo_run_start_time"], RUNTIME_STATE["clean_demo_trade_count"])
        resumed, policy_notes = apply_resume_policy(RUNTIME_STATE, gate.is_unlocked, _auto_resume_live())
        PERSISTENCE_INFO["resumed"] = resumed
        PERSISTENCE_INFO["notes"] = notes + policy_notes
        await save_console_state()  # make the disk match what the resume policy decided
    except Exception as exc:
        logger.warning("Could not load saved state from %s: %s", STATE_DB_PATH, exc)
        PERSISTENCE_INFO["notes"] = notes + [f"Saved state could not be loaded ({exc}); starting from defaults."]


# ---------------------------------------------------------------- trading engine
# The engine runs as a background task inside this process while demo testing is active.
#   BOT_ENGINE=off   disables it (used by the tests, or to run the console without trading).
_ENGINE: Optional[DemoEngine] = None
_ENGINE_TASK: Optional["asyncio.Task[None]"] = None
_ENGINE_KEYS: tuple = ("", "")


def _engine_enabled() -> bool:
    return os.environ.get("BOT_ENGINE", "on").strip().lower() not in {"off", "0", "false", "no"}


def _engine_poll_seconds() -> float:
    try:
        return max(2.0, float(os.environ.get("ENGINE_POLL_SECONDS", "15")))
    except ValueError:
        return 15.0


async def _notify_telegram(title: str, body: str, level: str) -> None:
    cfg = RUNTIME_STATE["telegram_config"]
    await AlertDispatcher(telegram_token=cfg["bot_token"], telegram_chat_id=cfg["chat_id"]).send_alert(title, body, level)


def _engine_wanted() -> bool:
    keys = RUNTIME_STATE["bybit_demo_keys"]
    return bool(
        _engine_enabled()
        and RUNTIME_STATE["demo_testing_active"]
        and not RUNTIME_STATE["kill_switch_triggered"]
        and keys["api_key"] and keys["api_secret"]
    )


def _new_engine() -> DemoEngine:
    keys = RUNTIME_STATE["bybit_demo_keys"]
    return DemoEngine(
        BybitV5Adapter(api_key=keys["api_key"], api_secret=keys["api_secret"], is_demo=True),
        RUNTIME_STATE,
        persist=save_console_state,
        record_trade=record_demo_trade,
        notify=_notify_telegram,
        poll_seconds=_engine_poll_seconds(),
    )


async def _stop_engine(timeout: float = 30.0) -> None:
    """Ask the engine to stop and wait for it to finish its current step (so an order is
    never abandoned half-way); cancel it only if it takes too long."""
    global _ENGINE, _ENGINE_TASK
    engine, task = _ENGINE, _ENGINE_TASK
    _ENGINE, _ENGINE_TASK = None, None
    if engine:
        engine.request_stop()
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except Exception:
            task.cancel()


async def sync_engine() -> None:
    """Make the engine match the flags: running while demo testing is active, demo keys are
    saved and the kill switch is off; stopped otherwise. Called after every change."""
    global _ENGINE, _ENGINE_TASK, _ENGINE_KEYS
    wanted = _engine_wanted()
    running = _ENGINE_TASK is not None and not _ENGINE_TASK.done()
    keys = (RUNTIME_STATE["bybit_demo_keys"]["api_key"], RUNTIME_STATE["bybit_demo_keys"]["api_secret"])
    if running and (not wanted or keys != _ENGINE_KEYS):
        await _stop_engine()
        running = False
    if wanted and not running:
        _ENGINE = _new_engine()
        _ENGINE_KEYS = keys
        _ENGINE_TASK = asyncio.create_task(_ENGINE.run())
        logger.info("Trading engine started (demo).")


def _engine_summary() -> Dict[str, Any]:
    running = _ENGINE_TASK is not None and not _ENGINE_TASK.done()
    base: Dict[str, Any] = {"enabled": _engine_enabled(), "running": running}
    if _ENGINE is not None and running:
        base.update({k: _ENGINE.status.get(k) for k in ("text", "last_scan_utc", "last_decision", "last_error", "balance", "trend")})
        base["consecutive_errors"] = _ENGINE.consecutive_errors
        setup = RUNTIME_STATE["engine_state"].get("setup")
        base["armed_setup"] = bool(setup)
    return base


def _engine_idle_reason() -> str:
    if not _engine_enabled():
        return "Trading engine is switched off (BOT_ENGINE=off)."
    if RUNTIME_STATE["kill_switch_triggered"]:
        return "Kill switch engaged: nothing is trading."
    if not RUNTIME_STATE["demo_testing_active"]:
        return "Radar idle. Click 'Start Demo Testing' to begin scanning."
    keys = RUNTIME_STATE["bybit_demo_keys"]
    if not (keys["api_key"] and keys["api_secret"]):
        return "Demo testing is on, but no Bybit demo API keys are saved, so nothing can trade. Save your demo keys."
    return "Starting the trading engine..."


async def _close_open_position_for_kill() -> str:
    """Kill switch: stop the engine's loop (so two tasks never close the same trade), then
    close any open demo trade on Bybit. Returns a short result line."""
    engine = _ENGINE
    await _stop_engine()
    if engine is None and RUNTIME_STATE["active_position"] and RUNTIME_STATE["bybit_demo_keys"]["api_key"]:
        engine = _new_engine()
    if engine is None:
        return ""
    try:
        return await asyncio.wait_for(engine.emergency_close(), timeout=45.0)
    except Exception as exc:
        return f"Could not confirm the position was closed ({exc})."


async def record_trade(
    mode: str,
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    pnl_r: float,
    clean: bool = False,
):
    """Record a closed trade in the stats and save it. A trade that counts toward the
    24h / 20-trade gate is saved together with the new count in one all-or-nothing write,
    so a crash can never record the trade without the count (or the reverse)."""
    trade = STATS_TRACKER.record_completed_trade(
        mode=mode,
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
    if STATE_DB_PATH:
        try:
            async with _SAVE_LOCK:
                store = StateStore(db_path=STATE_DB_PATH)
                await store.init_db()
                await store.save_trade(asdict(trade), {_KEY_STATE: _plain_json()})
        except Exception as exc:
            logger.warning("Could not save trade %s to %s: %s", trade.id, STATE_DB_PATH, exc)
    return trade


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

    The trade is added to the stats and saved; when it is clean it also counts toward the
    20-trade gate. What counts as "clean" is still an open decision in the PRD; the caller
    decides, and the default counts every properly closed trade.
    """
    return await record_trade("DEMO", symbol, side, qty, entry_price, exit_price, pnl_usd, pnl_r, clean=clean)


async def record_live_trade(
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    pnl_r: float,
):
    """The one place a closed live trade is recorded. The trading engine calls this."""
    return await record_trade("LIVE", symbol, side, qty, entry_price, exit_price, pnl_usd, pnl_r)


def _persistence_status() -> Dict[str, Any]:
    box = _secrets_box()
    enabled = bool(STATE_DB_PATH)
    return {
        "enabled": enabled,
        "secrets_saved_encrypted": enabled and box.available,
        "secrets_note": (
            f"API keys and the Telegram token are NOT saved to disk: {box.unavailable_reason}."
            if enabled and not box.available
            else None
        ),
        "last_saved_utc": PERSISTENCE_INFO["last_saved_utc"],
        "resumed": list(PERSISTENCE_INFO["resumed"]),
        "notes": list(PERSISTENCE_INFO["notes"]),
    }


async def _save_secrets_and_describe() -> Dict[str, Any]:
    """Save after a keys or Telegram change and tell the caller what actually reached disk."""
    saved = await save_console_state(secrets_changed=True)
    box = _secrets_box()
    result: Dict[str, Any] = {"saved_to_disk": bool(saved and box.available)}
    if STATE_DB_PATH and not box.available:
        result["note"] = f"Kept in memory only, not saved to disk: {box.unavailable_reason}."
    return result


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

    # Market Radar: what the trading engine is really doing right now
    engine_info = _engine_summary()
    is_scanning = bool(engine_info["running"])  # only true while the trading engine is really running
    radar_state = {
        "is_actively_scanning": is_scanning,
        "symbol_monitored": "BTCUSDT",
        "timeframes_tracked": ["1h (Macro Bias)", "15m (Liquidity Sweeps)", "2m (Entry Triggers)"],
        "last_market_scan_utc": engine_info.get("last_scan_utc") or now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "current_market_status": (engine_info.get("text") if engine_info["running"] and engine_info.get("text") else _engine_idle_reason()),
        "last_decision": engine_info.get("last_decision"),
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
        "persistence": _persistence_status(),
        "engine": engine_info,
    }


@app.post("/api/keys")
async def save_api_keys(payload: APIKeyPayload, user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    if payload.account_type.lower() == "demo":
        RUNTIME_STATE["bybit_demo_keys"]["api_key"] = payload.api_key
        RUNTIME_STATE["bybit_demo_keys"]["api_secret"] = payload.api_secret
        message = "Demo keys stored successfully"
    elif payload.account_type.lower() == "live":
        RUNTIME_STATE["bybit_live_keys"]["api_key"] = payload.api_key
        RUNTIME_STATE["bybit_live_keys"]["api_secret"] = payload.api_secret
        message = "Live keys stored successfully"
    else:
        raise HTTPException(status_code=400, detail="Invalid account_type. Must be 'demo' or 'live'")
    result = await _save_secrets_and_describe()
    await sync_engine()  # demo testing waiting for keys starts trading as soon as they are saved
    return {"status": message, **result}


@app.post("/api/telegram")
async def save_telegram_config(payload: TelegramPayload, user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    RUNTIME_STATE["telegram_config"]["bot_token"] = payload.bot_token.strip()
    RUNTIME_STATE["telegram_config"]["chat_id"] = payload.chat_id.strip()
    return {
        "status": "SUCCESS",
        "message": "Telegram credentials saved successfully.",
        **await _save_secrets_and_describe(),
    }


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
    RUNTIME_STATE["demo_testing_active"] = True
    RUNTIME_STATE["current_mode"] = "DEMO"
    RUNTIME_STATE["is_bot_running"] = True
    RUNTIME_STATE["kill_switch_triggered"] = False
    await save_console_state()
    await sync_engine()
    message = "Demo Testing Started! Unrestricted trade cap active."
    keys = RUNTIME_STATE["bybit_demo_keys"]
    if not (keys["api_key"] and keys["api_secret"]):
        message += " No Bybit demo API keys are saved yet, so nothing will trade until you save them."
    return {"status": "SUCCESS", "message": message, "engine": _engine_summary()}


@app.post("/api/demo/stop")
async def stop_demo_testing(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    RUNTIME_STATE["demo_testing_active"] = False
    await save_console_state()
    await sync_engine()
    message = "Demo Testing Paused."
    if RUNTIME_STATE["active_position"]:
        message += " An open trade stays protected by its stop and take-profit orders on Bybit and is checked again when you resume."
    return {"status": "PAUSED", "message": message}


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
    await save_console_state()
    return {
        "status": "SUCCESS",
        "message": "Live flag set and 2 trades/day cap recorded. Note: automatic live order placement is not built yet, so nothing trades live.",
    }


@app.post("/api/live/stop")
async def stop_live_trading(user: str = Depends(authenticate_admin)) -> Dict[str, Any]:
    RUNTIME_STATE["live_trading_active"] = False
    await save_console_state()
    return {"status": "PAUSED", "message": "Live Trading Paused."}


@app.post("/api/emergency/kill")
async def trigger_emergency_kill(user: str = Depends(authenticate_admin)) -> Dict[str, str]:
    # Stop new trades first, then close whatever is open on Bybit.
    RUNTIME_STATE["is_bot_running"] = False
    RUNTIME_STATE["demo_testing_active"] = False
    RUNTIME_STATE["live_trading_active"] = False
    RUNTIME_STATE["kill_switch_triggered"] = True
    await save_console_state()
    closing = await _close_open_position_for_kill()
    await save_console_state()
    message = "Kill switch engaged. System halted and new orders blocked."
    if closing:
        message += " " + closing
    return {"status": "HALTED", "message": message}
