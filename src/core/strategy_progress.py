"""
Per-strategy demo progress: each strategy has its own 24h clock and its own count of clean
demo trades, so each one earns its own live unlock.

The original strategy's progress lives in the fields the console has always saved
(`demo_run_start_time`, `clean_demo_trade_count`), so saved state from earlier versions keeps
meaning the same thing. The other strategies live in `state["strategy_progress"]`.
Pure functions over the state dict (no I/O).
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

DEFAULT_STRATEGY = "sweep_reversal"  # must equal src.engine.strategies.DEFAULT_STRATEGY (tested)


def _entry(state: Dict[str, Any], strategy: str) -> Dict[str, Any]:
    return state.setdefault("strategy_progress", {}).setdefault(strategy, {"start": None, "clean": 0})


def get_progress(state: Dict[str, Any], strategy: str) -> Tuple[Optional[datetime], int]:
    """(when demo testing of this strategy first started, clean trades so far)."""
    if strategy == DEFAULT_STRATEGY:
        return state.get("demo_run_start_time"), int(state.get("clean_demo_trade_count", 0))
    entry = _entry(state, strategy)
    return entry["start"], int(entry["clean"])


def start_clock_if_needed(state: Dict[str, Any], strategy: str, now: Optional[datetime] = None) -> None:
    """The clock starts the first time demo testing is started with this strategy and is
    never reset by pausing, switching away or restarting."""
    now = now or datetime.now(timezone.utc)
    if strategy == DEFAULT_STRATEGY:
        if state.get("demo_run_start_time") is None:
            state["demo_run_start_time"] = now
        return
    entry = _entry(state, strategy)
    if entry["start"] is None:
        entry["start"] = now


def add_clean_trade(state: Dict[str, Any], strategy: str) -> None:
    if strategy == DEFAULT_STRATEGY:
        state["clean_demo_trade_count"] = int(state.get("clean_demo_trade_count", 0)) + 1
    else:
        _entry(state, strategy)["clean"] += 1
