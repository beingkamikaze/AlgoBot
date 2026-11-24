"""Where the app's NSE bars and prices come from, in this order:

  1. "kite_live": bars built from Kite's WebSocket ticks (app.market.ticks), when this symbol ticked in the last two
     minutes. Earlier days come from the bar store (topped up from Kite's historical API when it is behind).
  2. "kite": Kite's historical-candle API, when you are logged in to Kite today but the symbol is not streaming
     (1h and daily bars, a symbol outside the Nifty 100, or the socket is down).
  3. "yahoo": Yahoo Finance, free and delayed, when you are not logged in to Kite today. This is the fallback only.

Every `Candles` says which one it came from (`source`, `delayed`); the chart, signals and monitor pages show it.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from app.core.calendar import CLOSE, IST, is_trading_day
from app.market import kite as K
from app.market import ticks, yahoo
from app.market.models import TIMEFRAMES, Bar, Candles, Quote
from app.market.symbols import INDICES, normalize

log = logging.getLogger("feed")

LABELS = {"kite_live": "Kite live", "kite": "Kite", "yahoo": "Yahoo, delayed"}
RANK = {"yahoo": 0, "kite": 1, "kite_live": 2}
SESSION_RANGES = {"1d": 1, "5d": 5}
CALENDAR_DAYS = {"1mo": 31, "3mo": 92, "6mo": 183, "1y": 366, "2y": 731, "5y": 1827, "10y": 3653, "60d": 60}
TAIL_DAYS = 40                                          # earlier days of the bar store kept in memory per symbol


def label(source: str) -> str:
    return LABELS.get(source, source)


def worst(sources) -> str | None:
    """The least live of several sources (a scan that mixed Kite and Yahoo is labelled Yahoo)."""
    s = [x for x in sources if x in RANK]
    return min(s, key=RANK.get) if s else None


# ---------------------------------------------------------------- small caches

class _TTL:
    def __init__(self) -> None:
        self.data: dict = {}
        self.lock = threading.Lock()

    def get(self, key, ttl: float):
        with self.lock:
            hit = self.data.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] < ttl else None

    def put(self, key, value) -> None:
        with self.lock:
            if len(self.data) > 2000:
                self.data.clear()
            self.data[key] = (time.monotonic(), value)


_hist = _TTL()
_tails: dict[tuple[str, str], tuple[float, list[Bar]]] = {}
_topped: dict[tuple[str, str], dt.date] = {}
_tail_lock = threading.Lock()


def clear_caches() -> None:
    with _tail_lock:
        _tails.clear()
        _topped.clear()
    with _hist.lock:
        _hist.data.clear()


# ---------------------------------------------------------------- Kite candles -> bars

def to_bars(rows: list[dict], tf: str, now: dt.datetime) -> list[Bar]:
    """Kite candles -> bars; the bar still forming is kept, marked incomplete."""
    step = TIMEFRAMES[tf][1]
    now = now.astimezone(IST)
    out = []
    for r in rows:
        d = r["date"]
        if isinstance(d, str):
            d = dt.datetime.fromisoformat(d)
        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)
        t = int(d.timestamp())
        if tf == "1d":
            day = d.astimezone(IST).date()
            complete = day < now.date() or now.time() >= CLOSE
        else:
            complete = t + step <= now.timestamp()
        out.append(Bar(t=t, o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]),
                       v=int(r.get("volume") or 0), complete=complete))
    return out


def _chunks(since: dt.date, until: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    out, a = [], since
    while a <= until:
        b = min(a + dt.timedelta(days=days - 1), until)
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


def _row(kd: K.KiteData, sym: str, today: dt.date) -> dict:
    row = kd.tokens(today).get(sym) or kd.nfo(sym, today)
    if not row:
        raise K.KiteDataError(f"Kite has no instrument {sym}")
    return row


def kite_bars(kd: K.KiteData, sym: str, tf: str, since: dt.date, until: dt.date, now: dt.datetime) -> list[Bar]:
    token = int(_row(kd, sym, now.date())["instrument_token"])
    bars: list[Bar] = []
    for a, b in _chunks(since, until, K.MAX_DAYS[tf]):
        bars += to_bars(kd.historical(token, a, b, tf), tf, now)
    return bars


def _since(rng: str, today: dt.date) -> dt.date:
    if rng in SESSION_RANGES:
        return today - dt.timedelta(days=SESSION_RANGES[rng] * 2 + 6)      # enough calendar days for N sessions
    return today - dt.timedelta(days=CALENDAR_DAYS.get(rng, 60))


def trim(bars: list[Bar], rng: str, today: dt.date) -> list[Bar]:
    if rng in SESSION_RANGES:
        days = sorted({dt.datetime.fromtimestamp(b.t, IST).date() for b in bars})[-SESSION_RANGES[rng]:]
        keep = set(days)
        return [b for b in bars if dt.datetime.fromtimestamp(b.t, IST).date() in keep]
    first = int(dt.datetime.combine(_since(rng, today), dt.time(0, 0), IST).timestamp())
    return [b for b in bars if b.t >= first]


def _merge(*lists: list[Bar]) -> list[Bar]:
    """Later lists win for the same bar time (complete or not)."""
    by_t: dict[int, Bar] = {}
    for lst in lists:
        by_t.update({b.t: b for b in lst})
    return [by_t[t] for t in sorted(by_t)]


def _prev_session(today: dt.date) -> dt.date:
    d = today - dt.timedelta(days=1)
    while not is_trading_day(d)[0]:
        d -= dt.timedelta(days=1)
    return d


def _stored(kd: K.KiteData, sym: str, tf: str, since: dt.date, now: dt.datetime) -> list[Bar]:
    """Bars before today from the bar store, topped up from Kite's historical API (once a day) when it is behind."""
    from app.market import store
    today = now.date()
    key = (sym, tf)
    if _topped.get(key) != today:
        _topped[key] = today
        stored = store.read(sym, tf)
        last = dt.datetime.fromtimestamp(stored[-1].t, IST).date() if stored else None
        want_to = _prev_session(today)
        if last is None or last < want_to:
            frm = max(last + dt.timedelta(days=1), today - dt.timedelta(days=K.MAX_DAYS[tf])) if last else since
            try:
                new = [b for b in kite_bars(kd, sym, tf, frm, want_to, now) if b.complete]
                if new:
                    with store.locked(sym, tf):
                        store.write(sym, tf, store.merge(store.read(sym, tf), new))
            except K.KiteDataError as e:
                log.info("store top-up for %s %s failed: %s", sym, tf, e)
    p = store.path(sym, tf)
    mtime = p.stat().st_mtime if p.exists() else 0.0
    if (today - since).days > TAIL_DAYS:
        bars = store.read(sym, tf)
    else:
        with _tail_lock:
            hit = _tails.get(key)
        if hit and hit[0] == mtime:
            bars = hit[1]
        else:
            cut = int(dt.datetime.combine(today - dt.timedelta(days=TAIL_DAYS), dt.time(0, 0), IST).timestamp())
            bars = [b for b in store.read(sym, tf) if b.t >= cut]
            with _tail_lock:
                _tails[key] = (mtime, bars)
    midnight = int(dt.datetime.combine(today, dt.time(0, 0), IST).timestamp())
    first = int(dt.datetime.combine(since, dt.time(0, 0), IST).timestamp())
    return [b for b in bars if first <= b.t < midnight]


def _from_kite(kd: K.KiteData, sym: str, tf: str, rng: str, now: dt.datetime, bars_src: ticks.BarBuilder) -> Candles:
    today = now.date()
    since = _since(rng, today)
    row = _row(kd, sym, today)
    live = tf in ticks.TICK_TFS and bars_src.fresh(sym)
    if live:
        earlier = _stored(kd, sym, tf, since, now)
        today_hist: list[Bar] = []
        if bars_src.missing(sym, tf, now):                  # the socket started late or dropped: Kite fills the gap
            today_hist = _hist.get(("today", sym, tf), 60)
            if today_hist is None:
                today_hist = kite_bars(kd, sym, tf, today, today, now)
                _hist.put(("today", sym, tf), today_hist)
        bars = _merge(earlier, today_hist, bars_src.bars(sym, tf))
        source = "kite_live"
    else:
        ttl = yahoo._ttl_for(tf)
        bars = _hist.get(("range", sym, tf, rng), ttl)
        if bars is None:
            bars = kite_bars(kd, sym, tf, since, today, now)
            _hist.put(("range", sym, tf, rng), bars)
        source = "kite"
    bars = trim(bars, rng, today)
    last = bars_src.last_price(sym) if live else None
    prev = yahoo.previous_close(bars, tf, {}) or bars_src.prev_close.get(sym)
    name = INDICES[sym][1] if sym in INDICES else (row.get("name") or None)
    return Candles(symbol=sym, provider_symbol=K.key_for(sym) if row.get("exchange") != "NFO" else f"NFO:{sym}",
                   name=name, tf=tf, bars=bars, prev_close=prev,
                   last_price=last[0] if last else (bars[-1].c if bars else None),
                   as_of=int(last[1]) if last else (bars[-1].t if bars else None), source=source, delayed=False)


def candles(symbol: str, tf: str = "5m", rng: str | None = None, now: dt.datetime | None = None) -> Candles:
    """Bars for `symbol` from the best source available right now (see the module docstring)."""
    if tf not in TIMEFRAMES:
        raise yahoo.MarketDataError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
    rng = rng or TIMEFRAMES[tf][2]
    kd = K.current()
    if kd is not None:
        try:
            return _from_kite(kd, normalize(symbol), tf, rng, (now or dt.datetime.now(IST)).astimezone(IST), ticks.builder)
        except K.KiteDataError as e:
            log.info("Kite bars for %s %s failed, using Yahoo: %s", symbol, tf, e)
            if K.is_auth_error(e):
                K.forget()
    return yahoo.candles(symbol, tf, rng)


def quotes(symbols: list[str]) -> list[Quote]:
    """Last price and change: live ticks, else Kite's ohlc call, else Yahoo."""
    kd = K.current()
    if kd is None:
        return yahoo.quotes(symbols)
    out: dict[str, Quote] = {}
    need: list[str] = []
    for s in (normalize(x) for x in symbols):
        hit = ticks.builder.last_price(s) if ticks.builder.fresh(s) else None
        prev = ticks.builder.prev_close.get(s)
        if hit and prev:
            out[s] = Quote(symbol=s, last=hit[0], prev_close=prev, change_pct=round((hit[0] / prev - 1) * 100, 2),
                           as_of=int(hit[1]))
        else:
            need.append(s)
    if need:
        try:
            got = kd.ohlc([K.key_for(s) for s in need])
        except K.KiteDataError as e:
            log.info("Kite quotes failed, using Yahoo: %s", e)
            if K.is_auth_error(e):
                K.forget()
            return yahoo.quotes(symbols)
        for s in need:
            q = got.get(K.key_for(s))
            if not q or q.get("last_price") is None:
                continue
            last, prev = float(q["last_price"]), (q.get("ohlc") or {}).get("close") or None
            out[s] = Quote(symbol=s, last=last, prev_close=prev, change_pct=round((last / prev - 1) * 100, 2) if prev else None,
                           as_of=int(time.time()))
    return [out[s] for s in (normalize(x) for x in symbols) if s in out]


def live_price(symbol: str) -> float | None:
    """The last tick, if this symbol (our name or an NFO tradingsymbol) is streaming right now."""
    s = normalize(symbol)
    if ticks.builder.fresh(s):
        hit = ticks.builder.last_price(s)
        return hit[0] if hit else None
    return None
