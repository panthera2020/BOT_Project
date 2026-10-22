"""
What the console saves and what comes back after a restart.

Pure functions only (no I/O). They turn the console's in-memory state into two parts:

  * plain state  - flags, the demo clock and count, the open position, the Telegram
                   chat id. Safe to store as readable JSON.
  * secrets      - exchange API keys and the Telegram bot token. These are only ever
                   written to disk encrypted (see src/storage/secrets_box.py).

and put them back after a restart, applying a resume policy that decides what is
allowed to start running again on its own.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.core.strategy_progress import DEFAULT_STRATEGY

SNAPSHOT_VERSION = 1
_MODES = {"DEMO", "LIVE"}
_BOOL_FLAGS = ("is_bot_running", "demo_testing_active", "live_trading_active", "kill_switch_triggered")


def snapshot_plain(state: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    started = state.get("demo_run_start_time")
    return {
        "version": SNAPSHOT_VERSION,
        "saved_at": now.isoformat(),
        "flags": {name: bool(state[name]) for name in _BOOL_FLAGS} | {"current_mode": state["current_mode"]},
        "demo_run_start_time": started.isoformat() if started else None,
        "clean_demo_trade_count": int(state["clean_demo_trade_count"]),
        "active_strategy": state.get("active_strategy", DEFAULT_STRATEGY),
        "strategy_progress": {
            sid: {"start": p["start"].isoformat() if p.get("start") else None, "clean": int(p.get("clean", 0))}
            for sid, p in (state.get("strategy_progress") or {}).items()
        },
        "active_position": state.get("active_position"),
        "engine_state": state.get("engine_state") or {},
        "telegram_chat_id": state["telegram_config"]["chat_id"],
    }


def snapshot_secrets(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "bybit_demo_keys": dict(state["bybit_demo_keys"]),
        "bybit_live_keys": dict(state["bybit_live_keys"]),
        "telegram_bot_token": state["telegram_config"]["bot_token"],
    }


def has_any_secret(secrets: Dict[str, Any]) -> bool:
    return bool(
        secrets["bybit_demo_keys"].get("api_key")
        or secrets["bybit_demo_keys"].get("api_secret")
        or secrets["bybit_live_keys"].get("api_key")
        or secrets["bybit_live_keys"].get("api_secret")
        or secrets["telegram_bot_token"]
    )


def restore_plain(state: Dict[str, Any], data: Any) -> List[str]:
    """Apply saved plain state. Each field is checked on its own, so one damaged field
    is skipped (and reported) without losing the rest. Returns problems found."""
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["saved state is not a JSON object; ignored"]

    flags = data.get("flags")
    if isinstance(flags, dict):
        for name in _BOOL_FLAGS:
            if name in flags:
                if isinstance(flags[name], bool):
                    state[name] = flags[name]
                else:
                    problems.append(f"flag {name} is not true/false; ignored")
        mode = flags.get("current_mode")
        if mode in _MODES:
            state["current_mode"] = mode
        elif mode is not None:
            problems.append("current_mode is invalid; ignored")

    if data.get("demo_run_start_time"):
        try:
            started = datetime.fromisoformat(data["demo_run_start_time"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            state["demo_run_start_time"] = started
        except (TypeError, ValueError):
            problems.append("demo_run_start_time is not a valid time; ignored")

    count = data.get("clean_demo_trade_count")
    if count is not None:
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            state["clean_demo_trade_count"] = count
        else:
            problems.append("clean_demo_trade_count is invalid; ignored")

    chosen = data.get("active_strategy")
    if chosen is not None:
        if isinstance(chosen, str) and chosen:
            state["active_strategy"] = chosen   # may name a strategy that is no longer installed; the console falls back
        else:
            problems.append("active_strategy is invalid; keeping the default")

    progress = data.get("strategy_progress")
    if progress is not None:
        if isinstance(progress, dict):
            restored: Dict[str, Any] = {}
            for sid, item in progress.items():
                # Progress of strategies that are not installed right now is kept, so putting the
                # strategy back restores its clock and count.
                clean = item.get("clean", 0) if isinstance(item, dict) else None
                if not (isinstance(clean, int) and not isinstance(clean, bool) and clean >= 0):
                    problems.append(f"progress of strategy {sid!r} is invalid; ignored")
                    continue
                start = None
                if item.get("start"):
                    try:
                        start = datetime.fromisoformat(item["start"])
                        if start.tzinfo is None:
                            start = start.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        problems.append(f"start time of strategy {sid!r} is invalid; ignored")
                restored[sid] = {"start": start, "clean": clean}
            state["strategy_progress"] = restored
        else:
            problems.append("strategy_progress is invalid; ignored")

    if "active_position" in data:
        position = data["active_position"]
        if position is None or isinstance(position, dict):
            state["active_position"] = position
        else:
            problems.append("active_position is invalid; ignored")

    if "engine_state" in data:
        engine_state = data["engine_state"]
        if isinstance(engine_state, dict):
            state["engine_state"] = engine_state
        else:
            problems.append("engine_state is invalid; ignored")

    chat_id = data.get("telegram_chat_id")
    if isinstance(chat_id, str):
        state["telegram_config"]["chat_id"] = chat_id
    return problems


def restore_secrets(state: Dict[str, Any], data: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["saved secrets are not a JSON object; ignored"]
    for name in ("bybit_demo_keys", "bybit_live_keys"):
        keys = data.get(name)
        if isinstance(keys, dict) and all(isinstance(keys.get(k, ""), str) for k in ("api_key", "api_secret")):
            state[name]["api_key"] = keys.get("api_key", "")
            state[name]["api_secret"] = keys.get("api_secret", "")
        elif keys is not None:
            problems.append(f"{name} is invalid; ignored")
    token = data.get("telegram_bot_token")
    if isinstance(token, str):
        state["telegram_config"]["bot_token"] = token
    return problems


def apply_resume_policy(
    state: Dict[str, Any],
    gate_unlocked: bool,
    auto_resume_live: bool,
) -> Tuple[List[str], List[str]]:
    """Decide what may start running again by itself after a restart.

    Returns (resumed, notes): short plain-English lines for the dashboard/API.

      * Kill switch engaged -> everything stays stopped.
      * Demo testing that was running resumes automatically.
      * Live trading resumes ONLY when AUTO_RESUME_LIVE is on, the demo gate is still
        unlocked, live keys are present and the kill switch is off. Otherwise it stays
        paused and needs one deliberate press of Start Live Trading, because live
        trading must not restart unattended before the engine can check Bybit for open
        positions.
    """
    resumed: List[str] = []
    notes: List[str] = []

    if state["kill_switch_triggered"]:
        state["is_bot_running"] = False
        state["demo_testing_active"] = False
        state["live_trading_active"] = False
        notes.append("The kill switch was engaged before the restart, so everything stays stopped.")
        return resumed, notes

    if state["live_trading_active"]:
        allowed = auto_resume_live and gate_unlocked and bool(state["bybit_live_keys"]["api_key"])
        if allowed:
            resumed.append("Live trading resumed (AUTO_RESUME_LIVE is on).")
        else:
            state["live_trading_active"] = False
            state["current_mode"] = "DEMO"
            notes.append(
                "Live trading was active before the restart and has NOT been resumed. "
                "Press Start Live Trading to continue."
            )

    if state["demo_testing_active"]:
        resumed.append("Demo testing resumed.")

    if state["active_position"]:
        notes.append(
            "An open position was restored from saved state. When demo testing runs, the "
            "engine checks it against Bybit before doing anything else."
        )
    return resumed, notes
