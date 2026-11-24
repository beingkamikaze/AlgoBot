"""Yahoo's COMEX gold futures (`GC=F`): the free fallback when there is no live spot. Futures are a different price from
spot and jump when the front contract rolls, so this is shown as futures and never joined to spot history."""

from __future__ import annotations

import datetime as dt

import httpx

from app.global_markets.clock import NEW_YORK
from app.market.models import Bar

SYMBOLS = {"XAUUSD": "GC=F"}
HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tradingbot/0.1)", "Accept": "application/json"}
# timeframe -> (Yahoo interval, seconds a bar, range)
INTERVALS = {"1m": ("1m", 60, "5d"), "5m": ("5m", 300, "60d"), "15m": ("15m", 900, "60d"), "1h": ("60m", 3600, "2y"),
             "1d": ("1d", 86400, "10y")}


class FuturesError(Exception):
    pass


def parse(doc: dict, tf: str, now: float) -> list[Bar]:
    try:
        res = doc["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        raise FuturesError("no data in Yahoo's answer")
    seconds = INTERVALS[tf][1]
    by_t: dict[int, Bar] = {}
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, lo, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (ts, o, h, lo, c):
            continue
        if tf == "1d":                  # a trading day, stamped like every other daily bar in this setup
            day = dt.datetime.fromtimestamp(ts, NEW_YORK).date()
            ts = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
        elif ts % seconds:              # Yahoo's live tick, off the bar grid
            continue
        by_t[ts] = Bar(t=int(ts), o=round(float(o), 2), h=round(float(h), 2), l=round(float(lo), 2), c=round(float(c), 2), v=int(q["volume"][i] or 0),
                       complete=ts + seconds <= now)
    return [by_t[t] for t in sorted(by_t)]


def fetch(symbol: str, tf: str, rng: str | None = None) -> list[Bar]:
    interval, _, default_range = INTERVALS[tf]
    last = "no answer"
    for host in HOSTS:
        try:
            r = httpx.get(f"https://{host}/v8/finance/chart/{SYMBOLS[symbol]}", params={"interval": interval, "range": rng or default_range},
                          headers=HEADERS, timeout=20)
        except httpx.HTTPError as e:
            last = type(e).__name__
            continue
        if r.status_code == 200:
            return parse(r.json(), tf, dt.datetime.now(dt.timezone.utc).timestamp())
        last = f"HTTP {r.status_code}"
    raise FuturesError(f"Yahoo did not answer ({last})")
