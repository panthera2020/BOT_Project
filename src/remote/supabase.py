"""
Minimal Supabase client over its REST API (PostgREST), using only httpx so it installs on a
phone (no Postgres driver to compile) and in a Vercel function.

Use the project's SECRET key (`sb_secret_...`, or the legacy `service_role` key) with this:
it bypasses row-level security, so it must stay on the phone and in Vercel's environment
variables, never in the browser or the repository. The tables are created with row-level
security ON and no policies, so the public (publishable / anon) key can read and write nothing.
"""
import os
from typing import Any, Dict, List, Optional

import httpx


class SupabaseError(RuntimeError):
    """A Supabase request failed (network error or an error answer)."""


def config_from_env() -> Optional[Dict[str, str]]:
    """SUPABASE_URL and SUPABASE_SERVICE_KEY (the secret key), or None if not both are set."""
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        return None
    return {"url": url, "key": key}


class SupabaseClient:
    def __init__(self, url: str, key: str, client: Optional[httpx.AsyncClient] = None, timeout: Optional[float] = None):
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
            raise ValueError("SUPABASE_URL must start with https://")
        self.base = url.rstrip("/") + "/rest/v1"
        self._client = client
        if timeout is None:
            try:
                timeout = max(5.0, float(os.environ.get("BOT_HTTP_TIMEOUT", "20")))
            except ValueError:
                timeout = 20.0
        self._timeout = timeout
        # New-style keys (sb_secret_...) go on the apikey header only; legacy JWT keys also
        # go on Authorization.
        self._headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):
            self._headers["Authorization"] = f"Bearer {key}"

    async def _request(self, method: str, table: str, *, params: Optional[Dict[str, str]] = None,
                       json: Any = None, prefer: Optional[str] = None) -> Any:
        headers = dict(self._headers)
        if prefer:
            headers["Prefer"] = prefer
        try:
            if self._client is not None:
                resp = await self._client.request(method, f"{self.base}/{table}", params=params, json=json, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.request(method, f"{self.base}/{table}", params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise SupabaseError(f"cannot reach Supabase: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 300:
            raise SupabaseError(f"Supabase {method} {table} failed ({resp.status_code}): {resp.text[:300]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def select(self, table: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Rows matching PostgREST filters, e.g. {"status": "eq.pending", "order": "id.asc"}."""
        return await self._request("GET", table, params={"select": "*", **(params or {})}) or []

    async def upsert(self, table: str, rows: Any, on_conflict: str) -> None:
        await self._request("POST", table, params={"on_conflict": on_conflict}, json=rows,
                            prefer="resolution=merge-duplicates,return=minimal")

    async def insert(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        out = await self._request("POST", table, json=row, prefer="return=representation")
        return out[0] if isinstance(out, list) and out else {}

    async def update(self, table: str, match: Dict[str, str], data: Dict[str, Any]) -> None:
        await self._request("PATCH", table, params=match, json=data, prefer="return=minimal")

    async def delete(self, table: str, match: Dict[str, str]) -> None:
        await self._request("DELETE", table, params=match, prefer="return=minimal")
