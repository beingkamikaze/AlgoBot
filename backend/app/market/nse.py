"""NSE's own reference data: the equity list (for search) and the holiday list.
Both are cached in backend/data/ so the app works when NSE's site is slow."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import time
from pathlib import Path

import httpx

from app.core.calendar import add_holidays
from app.market.symbols import INDICES

DATA = Path(__file__).resolve().parents[2] / "data"
EQUITY_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
WEEK = 7 * 86400


def _fetch(url: str) -> bytes:
    last: Exception | None = None
    for attempt in range(3):            # NSE drops connections now and then
        try:
            r = httpx.get(url, headers=NSE_HEADERS, timeout=30)
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"NSE did not answer: {last}")


def _fresh(path: Path, max_age: int) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < max_age


# ---------------------------------------------------------------- equity list

_equities: list[dict] | None = None


def parse_equity_list(text: str) -> list[dict]:
    """EQUITY_L.csv -> [{symbol, name}] for the EQ series (the ones you can trade intraday)."""
    rows = list(csv.reader(io.StringIO(text)))
    head = [h.strip().upper() for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = dict(zip(head, (c.strip() for c in r)))
        if d.get("SERIES") == "EQ" and d.get("SYMBOL"):
            out.append({"symbol": d["SYMBOL"].upper(), "name": d.get("NAME OF COMPANY", ""), "kind": "equity"})
    return out


def equities(refresh: bool = False) -> list[dict]:
    global _equities
    path = DATA / "equity_list.csv"
    if refresh or not _fresh(path, WEEK):
        try:
            DATA.mkdir(exist_ok=True)
            path.write_bytes(_fetch(EQUITY_URL))
        except RuntimeError:
            if not path.exists():
                raise
    if _equities is None or refresh:
        _equities = parse_equity_list(path.read_text(encoding="utf-8", errors="replace"))
    return _equities


def search(query: str, limit: int = 20) -> list[dict]:
    q = query.strip().upper()
    if not q:
        return []
    items = [{"symbol": k, "name": n, "kind": "index"} for k, (_, n) in INDICES.items()] + equities()
    exact = [i for i in items if i["symbol"] == q]
    prefix = sorted((i for i in items if i["symbol"].startswith(q) and i not in exact),
                    key=lambda i: (i["kind"] != "index", len(i["symbol"]), i["symbol"]))
    named = [i for i in items if q in i["name"].upper() and i not in exact and i not in prefix]
    return (exact + prefix + named)[:limit]


# ---------------------------------------------------------------- holidays

def parse_holidays(doc: dict) -> dict[str, str]:
    out = {}
    for h in doc.get("CM") or []:
        try:
            d = dt.datetime.strptime(h["tradingDate"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if d.weekday() < 5:
            out[d.isoformat()] = " ".join(str(h.get("description", "")).replace("*", "").split())
    return out


def load_holidays(refresh: bool = False) -> dict[str, str]:
    """NSE's holiday list for the current year (and next, once NSE publishes it)."""
    path = DATA / "holidays.json"
    if refresh or not _fresh(path, WEEK):
        try:
            got = parse_holidays(json.loads(_fetch(HOLIDAY_URL)))
            if got:
                DATA.mkdir(exist_ok=True)
                path.write_text(json.dumps(got, indent=2))
        except (RuntimeError, ValueError):
            pass                        # keep the built-in list and any older file
    if path.exists():
        got = json.loads(path.read_text())
        add_holidays(got)
        return got
    return {}


# ---------------------------------------------------------------- index constituents

INDEX_LISTS = {
    "NIFTY50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTYBANK": "https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv",
}


def parse_index_list(text: str) -> list[str]:
    rows = list(csv.reader(io.StringIO(text)))
    head = [h.strip().upper() for h in rows[0]]
    i = head.index("SYMBOL")
    return [r[i].strip().upper() for r in rows[1:] if len(r) > i and r[i].strip()]


def constituents(index: str) -> list[str]:
    """NSE's own list of an index's stocks (cached a week)."""
    index = index.upper()
    if index not in INDEX_LISTS:
        raise ValueError(f"unknown index list {index!r}; known: {', '.join(INDEX_LISTS)}")
    path = DATA / f"{index.lower()}.csv"
    if not _fresh(path, WEEK):
        try:
            DATA.mkdir(exist_ok=True)
            path.write_bytes(_fetch(INDEX_LISTS[index]))
        except RuntimeError:
            if not path.exists():
                raise
    return parse_index_list(path.read_text(encoding="utf-8", errors="replace"))
