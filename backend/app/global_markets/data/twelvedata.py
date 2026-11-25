"""Twelve Data: live XAU/USD spot bars (needs TWELVEDATA_API_KEY in backend/.env). The free plan is reported as 8 calls a
minute and 800 a day, so `feed.py` caches and this module counts. The key travels in a header so it can never appear in
a logged URL. Not yet tried against the real service: written from its documented format, and tested with a fake answer.
"""

from __future__ import annotations

import datetime as dt
import threading

import httpx

from app.market.models import Bar

BASE = "https://api.twelvedata.com/time_series"
SYMBOLS = {"XAUUSD": "XAU/USD"}
INTERVALS = {"1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "1d": "1day"}
DAILY_CALLS = 780           # keep a margin under 800


class TwelveDataError(Exception):
    pass


_lock = threading.Lock()
_calls = {"day": None, "count": 0}


def calls_today(now: dt.datetime | None = None) -> int:
    day = (now or dt.datetime.now(dt.timezone.utc)).date()
    with _lock:
        return _calls["count"] if _calls["day"] == day else 0


def _spend(now: dt.datetime) -> None:
    with _lock:
        if _calls["day"] != now.date():
            _calls.update(day=now.date(), count=0)
        if _calls["count"] >= DAILY_CALLS:
            raise TwelveDataError(f"today's {DAILY_CALLS} calls are used up")
        _calls["count"] += 1


def parse(doc: dict, tf: str, now: float) -> list[Bar]:
    if doc.get("status") != "ok":
        raise TwelveDataError(str(doc.get("message") or "unexpected answer")[:200])
    seconds = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}[tf]
    bars = []
    for row in doc.get("values") or []:
        text = row["datetime"]
        fmt = "%Y-%m-%d" if len(text) == 10 else "%Y-%m-%d %H:%M:%S"
        t = int(dt.datetime.strptime(text, fmt).replace(tzinfo=dt.timezone.utc).timestamp())
        bars.append(Bar(t=t, o=float(row["open"]), h=float(row["high"]), l=float(row["low"]), c=float(row["close"]),
                        v=int(float(row.get("volume") or 0)), complete=t + seconds <= now))
    return sorted(bars, key=lambda b: b.t)


def fetch(symbol: str, tf: str, api_key: str, outputsize: int = 1500) -> list[Bar]:
    now = dt.datetime.now(dt.timezone.utc)
    _spend(now)
    try:
        r = httpx.get(BASE, params={"symbol": SYMBOLS[symbol], "interval": INTERVALS[tf], "outputsize": outputsize,
                                    "timezone": "UTC", "order": "ASC"},
                      headers={"Authorization": f"apikey {api_key}"}, timeout=20)
        doc = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise TwelveDataError(f"no answer ({type(e).__name__})")
    return parse(doc, tf, now.timestamp())


PRICE_URL = "https://api.twelvedata.com/price"


def price(symbol: str, api_key: str) -> float:
    """The latest price, one call. Used when you close a paper trade, so the exit fills at the market you saw."""
    now = dt.datetime.now(dt.timezone.utc)
    _spend(now)
    try:
        r = httpx.get(PRICE_URL, params={"symbol": SYMBOLS[symbol]}, headers={"Authorization": f"apikey {api_key}"}, timeout=8)
        doc = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise TwelveDataError(f"no answer ({type(e).__name__})")
    try:
        return float(doc["price"])
    except (KeyError, TypeError, ValueError):
        raise TwelveDataError(str(doc.get("message") or "unexpected answer")[:200]) from None
