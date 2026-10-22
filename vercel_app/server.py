"""
Remote dashboard for Vercel. It shows what the phone last reported to Supabase and passes
Start / Pause / Kill presses to the phone through the bot_commands table. It does NOT trade,
does not import the phone console, and never sees Bybit API keys.

Environment variables (Vercel project settings):
    ADMIN_USER, ADMIN_PASS      sign-in (ADMIN_PASS: 10+ characters, not the public default)
    SUPABASE_URL                https://<project>.supabase.co
    SUPABASE_SERVICE_KEY        the Supabase SECRET key (server-side only)
"""
import asyncio
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from src.engine.strategies import strategy_catalog
from src.remote.supabase import SupabaseClient, SupabaseError, config_from_env

DEFAULT_ADMIN_PASS = "P@ssword123!"
ONLINE_WITHIN_SECONDS = 60
COMMAND_WAIT_SECONDS = 8.0
COMMAND_POLL_SECONDS = 1.0

app = FastAPI(title="Trading Bot Remote Dashboard")
security = HTTPBasic(auto_error=False)

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "web", "templates", "index.html")


def _credentials() -> Optional[Dict[str, str]]:
    user = os.environ.get("ADMIN_USER", "").strip()
    password = os.environ.get("ADMIN_PASS", "")
    if not user or len(password) < 10 or password == DEFAULT_ADMIN_PASS:
        return None
    return {"user": user, "pass": password}


def _client() -> SupabaseClient:
    cfg = config_from_env()
    if not cfg:
        raise HTTPException(status_code=503, detail="SUPABASE_URL / SUPABASE_SERVICE_KEY are not set on Vercel.")
    try:
        return SupabaseClient(cfg["url"], cfg["key"])
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


def authenticate(credentials: Optional[HTTPBasicCredentials] = Depends(security)) -> str:
    creds = _credentials()
    if creds is None:
        raise HTTPException(status_code=503, detail="ADMIN_USER / ADMIN_PASS are not set on Vercel (password: 10+ characters, not the default).")
    if (credentials is None
            or not secrets.compare_digest(credentials.username.encode(), creds["user"].encode())
            or not secrets.compare_digest(credentials.password.encode(), creds["pass"].encode())):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid administrative credentials")
    return credentials.username


class LoginPayload(BaseModel):
    username: str
    password: str


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "role": "remote-dashboard"}


@app.post("/api/login")
async def login(payload: LoginPayload) -> Dict[str, str]:
    creds = _credentials()
    if creds is None:
        raise HTTPException(status_code=503, detail="ADMIN_USER / ADMIN_PASS are not set on Vercel.")
    ok = (secrets.compare_digest(payload.username.encode(), creds["user"].encode())
          and secrets.compare_digest(payload.password.encode(), creds["pass"].encode()))
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid administrative credentials")
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    try:
        with open(_TEMPLATE, "r", encoding="utf-8") as fh:
            return HTMLResponse(fh.read())
    except OSError:
        return HTMLResponse("<h1>Dashboard template missing from the deployment</h1>", status_code=500)


def _view(updated_at: Any, now: datetime) -> Dict[str, Any]:
    seen = None
    if isinstance(updated_at, str):
        try:
            seen = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
        except ValueError:
            seen = None
    if seen is None:
        return {"online": False, "last_seen": None, "age_seconds": None}
    age = max(0, int((now - seen).total_seconds()))
    return {"online": age <= ONLINE_WITHIN_SECONDS,
            "last_seen": seen.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "age_seconds": age}


@app.get("/api/status")
async def get_status(user: str = Depends(authenticate)) -> Dict[str, Any]:
    client = _client()
    try:
        rows = await client.select("bot_status", {"id": "eq.1"})
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    now = datetime.now(timezone.utc)
    if not rows:
        payload: Dict[str, Any] = {"status": "STOPPED", "persistence": {"enabled": False, "resumed": [], "notes": []}}
        view = _view(None, now)
    else:
        payload = dict(rows[0].get("payload") or {})
        view = _view(rows[0].get("updated_at"), now)
    payload["remote_view"] = view
    return payload


async def _send(command: str, user_message: str, offline_note: str) -> Dict[str, Any]:
    client = _client()
    try:
        status_rows = await client.select("bot_status", {"id": "eq.1"})
        online = bool(status_rows) and _view(status_rows[0].get("updated_at"), datetime.now(timezone.utc))["online"]
        row = await client.insert("bot_commands", {"command": command})
        cid = row.get("id")
        waited = 0.0
        while online and cid is not None and waited < COMMAND_WAIT_SECONDS:
            await asyncio.sleep(COMMAND_POLL_SECONDS)
            waited += COMMAND_POLL_SECONDS
            got = await client.select("bot_commands", {"id": f"eq.{cid}"})
            if got and got[0].get("status") != "pending":
                state = got[0]
                if state["status"] == "done":
                    return {"status": "SUCCESS", "message": state.get("result") or user_message}
                raise HTTPException(status_code=409, detail=f"The phone could not do that: {state.get('result') or state['status']}")
    except SupabaseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not online:
        return {"status": "QUEUED", "message": offline_note}
    return {"status": "QUEUED", "message": "Sent to the phone; it has not confirmed yet. Watch the status."}


@app.post("/api/demo/start")
async def start_demo(user: str = Depends(authenticate)) -> Dict[str, Any]:
    return await _send("start_demo", "Demo testing started.",
                       "Phone is offline. The Start request waits 2 minutes and is then dropped; press it again once the phone is back.")


@app.post("/api/demo/stop")
async def stop_demo(user: str = Depends(authenticate)) -> Dict[str, Any]:
    return await _send("stop_demo", "Demo testing paused.",
                       "Phone is offline. Pause is queued and runs when it is back. Any open trade keeps its Bybit stop-loss and take-profit orders.")


@app.post("/api/emergency/kill")
async def kill(user: str = Depends(authenticate)) -> Dict[str, Any]:
    out = await _send("kill", "Kill switch engaged.",
                      "Phone is offline. The kill is queued and runs the moment it is back. Any open trade keeps its Bybit stop-loss and take-profit orders; to close it now, use the Bybit app.")
    return out


def _phone_only(what: str) -> HTTPException:
    return HTTPException(status_code=403, detail=f"{what} is only available on the phone console (keys never pass through the cloud).")


@app.get("/api/strategies")
async def strategies(user: str = Depends(authenticate)) -> Dict[str, Any]:
    """The rules text is fixed in the code, so this needs no round trip to the phone."""
    return {"active": None, "strategies": strategy_catalog()}


@app.post("/api/strategy")
async def switch_strategy(user: str = Depends(authenticate)):
    raise _phone_only("Switching strategy")


@app.post("/api/keys")
async def keys(user: str = Depends(authenticate)):
    raise _phone_only("Saving API keys")


@app.post("/api/telegram")
async def telegram(user: str = Depends(authenticate)):
    raise _phone_only("Telegram settings")


@app.post("/api/telegram/test")
async def telegram_test(user: str = Depends(authenticate)):
    raise _phone_only("The Telegram test")


@app.post("/api/live/start")
@app.post("/api/live/enable")
@app.post("/api/live/stop")
async def live(user: str = Depends(authenticate)):
    raise _phone_only("Live trading controls")
