"""A small in-memory stand-in for Supabase's REST API (PostgREST), served through
httpx.MockTransport, so the cloud code runs unchanged against it."""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx

from src.remote.supabase import SupabaseClient

PK = {"bot_status": "id", "bot_commands": "id", "state_backup": "key", "trade_backup": "id"}


class FakeSupabase:
    def __init__(self, key: str = "sb_secret_test"):
        self.key = key
        self.tables: Dict[str, List[Dict[str, Any]]] = {t: [] for t in PK}
        self.requests: List[httpx.Request] = []
        self.fail = False        # every request answers 500
        self.offline = False     # every request raises a connection error
        self._next_id = 1
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def api(self) -> SupabaseClient:
        return SupabaseClient("https://fake.supabase.co", self.key, client=self.client)

    @staticmethod
    def _cast(value: str, sample: Any):
        if isinstance(sample, int) and not isinstance(sample, bool):
            try:
                return int(value)
            except ValueError:
                return value
        return value

    def _match(self, row, params) -> bool:
        for col, cond in params.items():
            if col in ("select", "order", "limit", "on_conflict"):
                continue
            op, _, val = cond.partition(".")
            have = row.get(col)
            want = self._cast(val, have)
            if op == "eq" and not have == want:
                return False
            if op == "neq" and have == want:
                return False
            if op == "lt" and not (have is not None and str(have) < val):
                return False
        return True

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.offline:
            raise httpx.ConnectError("offline", request=req)
        if self.fail:
            return httpx.Response(500, text="boom")
        if req.headers.get("apikey") != self.key:
            return httpx.Response(401, text="bad key")
        table = req.url.path.rsplit("/", 1)[-1]
        if table not in self.tables:
            return httpx.Response(404, text="no such table")
        rows = self.tables[table]
        params = dict(req.url.params)
        prefer = req.headers.get("Prefer", "")
        body = json.loads(req.content) if req.content else None

        if req.method == "GET":
            out = [dict(r) for r in rows if self._match(r, params)]
            if "order" in params:
                col, _, direction = params["order"].partition(".")
                out.sort(key=lambda r: r[col], reverse=direction == "desc")
            if "limit" in params:
                out = out[: int(params["limit"])]
            return httpx.Response(200, json=out)
        if req.method == "POST":
            items = body if isinstance(body, list) else [body]
            result = []
            for item in items:
                item = dict(item)
                if "on_conflict" in params:
                    pk = PK[table]
                    existing = next((r for r in rows if r[pk] == item[pk]), None)
                    if existing is not None:
                        existing.update(item)
                        continue
                    rows.append(item)
                else:
                    item.setdefault("status", "pending")
                    item["id"] = self._next_id
                    item.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                    self._next_id += 1
                    rows.append(item)
                    result.append(dict(item))
            if "return=representation" in prefer:
                return httpx.Response(201, json=result)
            return httpx.Response(201)
        if req.method == "PATCH":
            for r in rows:
                if self._match(r, params):
                    r.update(body)
            return httpx.Response(204)
        if req.method == "DELETE":
            self.tables[table] = [r for r in rows if not self._match(r, params)]
            return httpx.Response(204)
        return httpx.Response(405)

    # test helpers
    def add_command(self, command: str, created_at: str = None, status: str = "pending") -> int:
        cid = self._next_id
        self._next_id += 1
        self.tables["bot_commands"].append({
            "id": cid, "command": command, "status": status,
            "created_at": created_at or datetime.now(timezone.utc).isoformat()})
        return cid

    def command(self, cid: int) -> Dict[str, Any]:
        return next(r for r in self.tables["bot_commands"] if r["id"] == cid)
