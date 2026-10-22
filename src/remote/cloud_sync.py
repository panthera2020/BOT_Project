"""
Copies the phone's saved state to Supabase and runs the commands pressed on the Vercel
dashboard. The phone stays the single source of truth: Supabase holds a mirror (so a lost or
replaced phone can recover) and a mailbox (so the dashboard can ask the phone to do things).

Nothing here talks to Bybit. API keys never go to Supabase unless SUPABASE_SYNC_SECRETS=1, and
then only the already-encrypted blob.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.remote.supabase import SupabaseClient

logger = logging.getLogger("CloudSync")

COMMANDS = ("start_demo", "stop_demo", "kill", "select_strategy")
# A "start" that sat unseen for this long is dropped: the person pressed it minutes ago and
# probably forgot. Stop and kill are safe to run late, so they never expire.
START_EXPIRES_SECONDS = 120
KEEP_DONE_COMMANDS_DAYS = 7


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class CloudSync:
    def __init__(self, client: SupabaseClient, *, sync_secrets: bool = False,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client = client
        self.sync_secrets = sync_secrets
        self._now = now_fn
        self._sent: Dict[str, str] = {}      # state key -> digest of what Supabase already has
        self._sent_trades: Dict[int, str] = {}
        self.last_push_utc: Optional[str] = None
        self.last_mirror_utc: Optional[str] = None
        self.last_error: Optional[str] = None

    def _ok(self) -> None:
        self.last_error = None

    def _fail(self, exc: Exception) -> None:
        self.last_error = str(exc)[:300]
        logger.warning("Supabase: %s", exc)

    # ------------------------------------------------------------------ status
    async def push_status(self, payload: Dict[str, Any]) -> bool:
        try:
            await self.client.upsert(
                "bot_status",
                {"id": 1, "payload": payload, "updated_at": self._now().isoformat()},
                on_conflict="id",
            )
        except Exception as exc:
            self._fail(exc)
            return False
        self.last_push_utc = self._now().strftime("%Y-%m-%d %H:%M:%S UTC")
        self._ok()
        return True

    # ------------------------------------------------------------------ mirror
    async def mirror(self, state_items: Dict[str, str], trades: List[Dict[str, Any]]) -> bool:
        """Upload what changed since the last successful upload. state_items maps a key
        (console_state, console_secrets) to its saved text."""
        import json
        try:
            rows = []
            for key, value in state_items.items():
                if key == "console_secrets" and not self.sync_secrets:
                    continue
                if self._sent.get(key) != _digest(value):
                    rows.append((key, value))
            if rows:
                await self.client.upsert(
                    "state_backup",
                    [{"key": k, "value": v, "updated_at": self._now().isoformat()} for k, v in rows],
                    on_conflict="key",
                )
                for k, v in rows:
                    self._sent[k] = _digest(v)
            new_trades = []
            for trade in trades:
                text = json.dumps(trade, sort_keys=True, default=str)
                if self._sent_trades.get(trade["id"]) != _digest(text):
                    new_trades.append((trade, text))
            if new_trades:
                await self.client.upsert(
                    "trade_backup",
                    [{"id": t["id"], "data": t, "updated_at": self._now().isoformat()} for t, _ in new_trades],
                    on_conflict="id",
                )
                for t, text in new_trades:
                    self._sent_trades[t["id"]] = _digest(text)
        except Exception as exc:
            self._fail(exc)
            return False
        self.last_mirror_utc = self._now().strftime("%Y-%m-%d %H:%M:%S UTC")
        self._ok()
        return True

    async def fetch_backup(self) -> Optional[Dict[str, Any]]:
        """What Supabase holds: {"state": {key: text}, "trades": [trade dicts]}, or None if it
        holds no console_state (a fresh project) or cannot be reached."""
        try:
            state_rows = await self.client.select("state_backup")
            trade_rows = await self.client.select("trade_backup", {"order": "id.asc"})
        except Exception as exc:
            self._fail(exc)
            return None
        state = {r["key"]: r["value"] for r in state_rows if "key" in r and "value" in r}
        if "console_state" not in state:
            return None
        if not self.sync_secrets:
            state.pop("console_secrets", None)
        return {"state": state, "trades": [r["data"] for r in trade_rows if isinstance(r.get("data"), dict)]}

    # ------------------------------------------------------------------ commands
    async def run_pending_commands(self, handlers: Dict[str, Callable[..., Awaitable[Any]]]) -> int:
        """Run every pending command in the order pressed. Returns how many were handled. A
        command may carry an argument (select_strategy: the strategy id); the handler receives
        it as its only argument."""
        try:
            pending = await self.client.select("bot_commands", {"status": "eq.pending", "order": "id.asc", "limit": "10"})
        except Exception as exc:
            self._fail(exc)
            return 0
        handled = 0
        for row in pending:
            cmd, cid = row.get("command"), row.get("id")
            if cid is None:
                continue
            status, result = "done", ""
            created = _parse_ts(row.get("created_at"))
            if cmd not in handlers:
                status, result = "error", f"unknown command {cmd!r}"
            elif cmd == "start_demo" and created and (self._now() - created).total_seconds() > START_EXPIRES_SECONDS:
                status, result = "expired", "Start was not picked up in time (phone offline?). Press it again."
            else:
                try:
                    arg = row.get("arg")
                    out = await (handlers[cmd](arg) if arg is not None else handlers[cmd]())
                    result = str(out.get("message", "")) if isinstance(out, dict) else str(out or "")
                except Exception as exc:
                    status, result = "error", f"{type(exc).__name__}: {getattr(exc, 'detail', exc)}"[:300]
            try:
                await self.client.update(
                    "bot_commands", {"id": f"eq.{cid}"},
                    {"status": status, "result": result[:500], "handled_at": self._now().isoformat()},
                )
            except Exception as exc:
                self._fail(exc)
                return handled
            handled += 1
        if pending:
            self._ok()
        return handled

    async def prune(self) -> None:
        cutoff = (self._now() - timedelta(days=KEEP_DONE_COMMANDS_DAYS)).isoformat()
        try:
            await self.client.delete("bot_commands", {"status": "neq.pending", "created_at": f"lt.{cutoff}"})
        except Exception as exc:
            self._fail(exc)

    def describe(self) -> Dict[str, Any]:
        return {"configured": True, "last_push_utc": self.last_push_utc,
                "last_backup_utc": self.last_mirror_utc, "last_error": self.last_error,
                "secrets_synced": self.sync_secrets}
