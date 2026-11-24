"""Supabase (PostgREST) access for the backend, with the service-role key.

Every query the backend makes on behalf of a user filters on that user's id itself:
the service role bypasses Row Level Security, so this module is where that
discipline lives. Nothing here is reachable from the browser.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


class DBError(Exception):
    pass


class DB:
    def __init__(self) -> None:
        s = get_settings()
        if not (s.supabase_url and s.supabase_service_role_key):
            raise DBError("Supabase is not configured: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in backend/.env")
        self.base = s.supabase_url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": s.supabase_service_role_key, "Authorization": f"Bearer {s.supabase_service_role_key}"}
        # close idle connections before Supabase's proxy does, so a reused one is rarely stale
        self.client = httpx.Client(timeout=15, headers=self.headers,
                                   limits=httpx.Limits(keepalive_expiry=10, max_keepalive_connections=10))

    def _send(self, method: str, url: str, *, retry: bool, **kw) -> httpx.Response:
        """A dropped connection is retried once only when resending cannot duplicate anything
        (reads, and updates/deletes, which set the same values again). Inserts are never resent."""
        for attempt in (1, 2):
            try:
                return self.client.request(method, url, **kw)
            except httpx.TransportError as e:
                if retry and attempt == 1:
                    continue
                raise DBError(f"{method} {url.rsplit('/', 1)[-1]}: connection to Supabase failed ({e.__class__.__name__})") from e
        raise AssertionError("unreachable")

    def _check(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise DBError(f"{r.request.method} {r.request.url.path}: {r.status_code} {msg}")
        return r.json() if r.content else None

    def select(self, table: str, filters: dict[str, str] | None = None, order: str | None = None,
               limit: int | None = None, columns: str = "*") -> list[dict]:
        params: dict[str, str] = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)
        return self._check(self._send("GET", f"{self.base}/{table}", retry=True, params=params))

    def insert(self, table: str, rows: dict | list[dict], upsert_on: str | None = None) -> list[dict]:
        prefer = "return=representation"
        params = {}
        if upsert_on:
            prefer += ",resolution=merge-duplicates"
            params["on_conflict"] = upsert_on
        return self._check(self._send("POST", f"{self.base}/{table}", retry=False, params=params, json=rows,
                                      headers={"Prefer": prefer}))

    def update(self, table: str, filters: dict[str, str], values: dict) -> list[dict]:
        if not filters:
            raise DBError("refusing an update without filters")
        return self._check(self._send("PATCH", f"{self.base}/{table}", retry=True, params=filters, json=values,
                                      headers={"Prefer": "return=representation"}))

    def delete(self, table: str, filters: dict[str, str]) -> None:
        if not filters:
            raise DBError("refusing a delete without filters")
        self._check(self._send("DELETE", f"{self.base}/{table}", retry=True, params=filters))

    def rpc(self, fn: str, args: dict) -> list[dict]:
        return self._check(self._send("POST", f"{self.base}/rpc/{fn}", retry=False, json=args))


_db: DB | None = None


def db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db


def eq(v: Any) -> str:
    return f"eq.{v}"
