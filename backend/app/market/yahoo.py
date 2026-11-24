"""Yahoo Finance chart data for NSE (free, unofficial, can lag). Used until the Kite
feed arrives in Phase 3; the rest of the app only sees `Candles` and `Quote`."""

from __future__ import annotations

import datetime as dt
import threading
import time
import urllib.parse

import httpx

from app.core.calendar import CLOSE, IST, OPEN
from app.market.models import TIMEFRAMES, Bar, Candles, Quote
from app.market.symbols import INDICES, normalize, to_yahoo

HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tradingbot/0.1)", "Accept": "application/json"}
SESSION_OPEN_MIN = OPEN.hour * 60 + OPEN.minute
SESSION_MINUTES = (CLOSE.hour * 60 + CLOSE.minute) - SESSION_OPEN_MIN   # 375


class MarketDataError(Exception):
    pass


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float):
        with self._lock:
            hit = self._data.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)


_cache = _TTLCache()


def _get(path: str) -> dict:
    """query2 first, query1 on a 429 or 5xx; one pause and retry on a 429."""
    last = "no answer"
    for attempt in range(2):
        for host in HOSTS:
            try:
                r = httpx.get(f"https://{host}{path}", headers=HEADERS, timeout=15)
            except httpx.HTTPError as e:
                last = f"network error: {e}"
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                desc = ((body.get("chart") or {}).get("error") or {}).get("description")
                raise MarketDataError(desc or "unknown symbol")
            last = f"HTTP {r.status_code}"
            if r.status_code != 429 and r.status_code < 500:
                raise MarketDataError(f"Yahoo answered {last}")
        time.sleep(2)
    raise MarketDataError(f"Yahoo did not answer ({last}); try again in a minute")


def _ttl_for(tf: str) -> float:
    return {"1m": 20, "5m": 30, "15m": 60, "1h": 120, "1d": 300}[tf]


def _on_grid(local: dt.datetime, minutes: int) -> bool:
    """Intraday bars start at 9:15 and every `minutes` after, and before 15:30. Anything else is
    Yahoo's live tick or its zero-volume closing print."""
    since_open = local.hour * 60 + local.minute - SESSION_OPEN_MIN
    return 0 <= since_open < SESSION_MINUTES and since_open % minutes == 0 and local.second == 0


def parse_chart(doc: dict, symbol: str, tf: str, now: dt.datetime) -> Candles:
    try:
        res = doc["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise MarketDataError(f"no data for {symbol}")
    meta = res.get("meta") or {}
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    seconds = TIMEFRAMES[tf][1]
    now_utc = now.timestamp()
    bars: list[Bar] = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
        if ts is None or c is None:
            continue
        o, h, lo = (q.get(k)[i] if q.get(k) and i < len(q[k]) else None for k in ("open", "high", "low"))
        v = q.get("volume")[i] if q.get("volume") and i < len(q["volume"]) else 0
        local = dt.datetime.fromtimestamp(ts, IST)
        if tf == "1d":
            day_done = local.date() < now.date() or now.time() >= CLOSE
            complete = day_done
        else:
            if not _on_grid(local, seconds // 60):
                continue
            complete = ts + seconds <= now_utc
        bars.append(Bar(t=int(ts), o=float(o if o is not None else c), h=float(h if h is not None else c),
                        l=float(lo if lo is not None else c), c=float(c), v=int(v or 0), complete=complete))
    # Yahoo can repeat a timestamp when the live bar is updated: keep the last
    dedup: dict[int, Bar] = {b.t: b for b in bars}
    ordered = [dedup[t] for t in sorted(dedup)]
    norm = normalize(symbol)
    name = INDICES[norm][1] if norm in INDICES else (meta.get("longName") or meta.get("shortName"))
    return Candles(symbol=norm, provider_symbol=meta.get("symbol") or to_yahoo(symbol), name=name, tf=tf,
                   bars=ordered, prev_close=previous_close(ordered, tf, meta),
                   last_price=meta.get("regularMarketPrice"), as_of=meta.get("regularMarketTime"))


def previous_close(bars: list[Bar], tf: str, meta: dict) -> float | None:
    """Yesterday's close. Yahoo's chartPreviousClose is the close before the whole range
    (five days back on a 5d chart), so take it from the bars when they span two sessions."""
    if tf == "1d":
        return bars[-2].c if len(bars) >= 2 else meta.get("chartPreviousClose")
    if bars:
        last_day = dt.datetime.fromtimestamp(bars[-1].t, IST).date()
        earlier = [b for b in bars if dt.datetime.fromtimestamp(b.t, IST).date() < last_day]
        if earlier:
            return earlier[-1].c
    return meta.get("chartPreviousClose") or meta.get("previousClose")


def candles(symbol: str, tf: str = "5m", rng: str | None = None) -> Candles:
    if tf not in TIMEFRAMES:
        raise MarketDataError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    interval, _, default_range, _ = TIMEFRAMES[tf]
    rng = rng or default_range
    ysym = to_yahoo(symbol)
    key = f"c|{ysym}|{tf}|{rng}"
    hit = _cache.get(key, _ttl_for(tf))
    if hit:
        return hit
    doc = _get(f"/v8/finance/chart/{urllib.parse.quote(ysym)}?range={rng}&interval={interval}")
    out = parse_chart(doc, symbol, tf, dt.datetime.now(IST))
    _cache.put(key, out)
    return out


def quotes(symbols: list[str]) -> list[Quote]:
    """Last price and change for many symbols: Yahoo spark, 20 symbols a request."""
    out: list[Quote] = []
    wanted = {to_yahoo(s): normalize(s) for s in symbols}
    ysyms = list(wanted)
    for i in range(0, len(ysyms), 20):
        batch = ysyms[i:i + 20]
        key = "q|" + ",".join(batch)
        doc = _cache.get(key, 20)
        if doc is None:
            doc = _get("/v7/finance/spark?symbols=" + ",".join(urllib.parse.quote(s) for s in batch)
                       + "&range=1d&interval=5m")
            _cache.put(key, doc)
        for item in (doc.get("spark") or {}).get("result") or []:
            meta = ((item.get("response") or [{}])[0] or {}).get("meta") or {}
            last, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose") or meta.get("previousClose")
            if last is None or item.get("symbol") not in wanted:
                continue
            out.append(Quote(symbol=wanted[item["symbol"]], last=float(last), prev_close=prev,
                             change_pct=round((last / prev - 1) * 100, 2) if prev else None,
                             as_of=meta.get("regularMarketTime")))
    order = {s: i for i, s in enumerate(wanted.values())}
    return sorted(out, key=lambda q: order.get(q.symbol, 0))
