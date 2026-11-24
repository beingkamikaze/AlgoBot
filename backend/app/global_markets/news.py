"""Gold headlines from Google News (US edition), cached for 10 minutes. Headlines are leads, not facts: the card shows them
with their source and time, and nothing trades on them."""

from __future__ import annotations

import datetime as dt
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx

from app.global_markets.clock import IST

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
QUERIES = {"XAUUSD": '(gold price OR XAUUSD OR "gold prices" OR bullion) when:{days}d'}
KEEP = re.compile(r"\bgold\b|xau|bullion", re.I)
JUNK = re.compile(r"gold (?:mining )?stocks?\b|\bminers?\b|price today live|live updates:? gold rate in|gold rate today in \w+|jewell?ery|buy gold coins|ira\b", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) TradingBot/1.0"}
CACHE_SECONDS = 600
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def parse(xml: bytes, limit: int = 10) -> list[dict]:
    items = []
    seen = set()
    for it in ET.fromstring(xml).findall("./channel/item"):
        title = " ".join((it.findtext("title") or "").split())
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        key = title.lower()[:80]
        if not title or key in seen or not KEEP.search(title) or JUNK.search(title):
            continue
        seen.add(key)
        try:
            t = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            t = None
        items.append({"time": t.astimezone(dt.timezone.utc).isoformat() if t else None,
                      "ist": t.astimezone(IST).strftime("%d %b %H:%M IST") if t else None,
                      "title": title, "source": src, "link": (it.findtext("link") or "").strip()})
    items.sort(key=lambda h: h["time"] or "", reverse=True)
    return items[:limit]


def headlines(symbol: str = "XAUUSD", days: int = 1, limit: int = 10, fetch=None) -> dict:
    """{symbol, headlines, error, fetched_at}. An error is a plain sentence, never an exception."""
    key = f"{symbol}|{days}|{limit}"
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    q = QUERIES.get(symbol.upper(), symbol + " when:{days}d").format(days=days)
    out: dict = {"symbol": symbol.upper(), "headlines": [], "error": None,
                 "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "Google News (US)"}
    try:
        if fetch is None:
            r = httpx.get(GNEWS.format(q=urllib.parse.quote_plus(q)), headers=HEADERS, timeout=15, follow_redirects=True)
            r.raise_for_status()
            data = r.content
        else:
            data = fetch(q)
        out["headlines"] = parse(data, limit)
    except Exception as e:  # noqa: BLE001 - no news is a line on the card, not a crash
        out["error"] = f"headlines could not be fetched right now ({type(e).__name__})"
    with _lock:
        _cache[key] = (time.time(), out)
    return out
