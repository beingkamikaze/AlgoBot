"""One chart series from the right source. "spot" is real spot: stored Dukascopy history plus Twelve Data's latest bars.
"futures" is Yahoo's `GC=F`. The two are never joined: they sit at different prices."""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field

from app.config import get_settings
from app.global_markets.clock import market_status, trading_day
from app.global_markets.data import bars as bars_mod
from app.global_markets.data import store, twelvedata, yahoo_futures
from app.market.models import Bar

SOURCES = ("spot", "futures")
HISTORY_DAYS = {"1m": 5, "5m": 30, "15m": 60, "1h": 200, "1d": 3650}    # how far back a chart reaches
LIVE_TTL = {"1m": 120, "5m": 300, "15m": 600, "1h": 1800, "1d": 21600}  # seconds between Twelve Data calls, per timeframe
CLOSED_TTL = 3600
FUTURES_TTL = {"1m": 20, "5m": 30, "15m": 60, "1h": 120, "1d": 300}
STORED_TTL = 300
STALE_AFTER = 900           # seconds: an open market with a last bar older than this is called out


@dataclass
class Series:
    symbol: str
    tf: str
    source: str             # "spot" or "futures"
    provider: str           # plain name for the screen
    bars: list[Bar] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    delayed: bool = True

    @property
    def last_price(self) -> float | None:
        return self.bars[-1].c if self.bars else None

    @property
    def as_of(self) -> int | None:
        return self.bars[-1].t if self.bars else None

    @property
    def prev_close(self) -> float | None:
        """The close of the last bar of the trading day before the latest bar's. On daily bars, the bar before."""
        if len(self.bars) < 2:
            return None
        if self.tf == "1d":
            return self.bars[-2].c
        day = trading_day(dt.datetime.fromtimestamp(self.bars[-1].t, dt.timezone.utc))
        for b in reversed(self.bars):
            if trading_day(dt.datetime.fromtimestamp(b.t, dt.timezone.utc)) != day:
                return b.c
        return None


_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _cached(key: str, ttl: float, make):
    """One caller refreshes at a time, so a page refreshing twice cannot spend two Twelve Data calls."""
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = make()
        _cache[key] = (time.monotonic(), value)
        return value


def default_source() -> str:
    return "spot" if get_settings().twelvedata_api_key else "futures"


def _stored(symbol: str, tf: str, now: float) -> list[Bar]:
    def load() -> list[Bar]:
        columns = store.read_columns(symbol, int(now) - HISTORY_DAYS[tf] * 86400)
        return bars_mod.to_bars(bars_mod.resample(store.mid(columns), tf), tf, now) if columns else []
    return _cached(f"stored|{symbol}|{tf}", STORED_TTL, load)


def _live(symbol: str, tf: str, api_key: str, is_open: bool) -> tuple[list[Bar], str | None]:
    """Latest spot bars, and what went wrong if anything. A failure is remembered for one wait like a success, so a
    broken key or an empty allowance does not spend calls on every page refresh."""
    def load():
        try:
            return twelvedata.fetch(symbol, tf, api_key), None
        except twelvedata.TwelveDataError as e:
            return [], f"Twelve Data: {e}"
    return _cached(f"live|{symbol}|{tf}", LIVE_TTL[tf] if is_open else CLOSED_TTL, load)


def spot(symbol: str, tf: str, now: float | None = None) -> Series:
    now = now if now is not None else time.time()
    moment = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    is_open = market_status(moment).is_open
    key = get_settings().twelvedata_api_key
    s = Series(symbol=symbol, tf=tf, source="spot", provider="Dukascopy history")
    stored = _stored(symbol, tf, now)
    live: list[Bar] = []
    if key:
        live, problem = _live(symbol, tf, key, is_open)
        if problem:
            s.notes.append(problem)
        if live:
            s.provider = "Dukascopy history + Twelve Data" if stored else "Twelve Data"
    by_t = {b.t: b for b in stored}
    if live and stored:
        # stored history has real volume and live bars usually none: a stored bar is kept unless still forming
        by_t.update({b.t: b for b in live if b.t not in by_t or not by_t[b.t].complete})
    elif live:
        by_t = {b.t: b for b in live}
    s.bars = [by_t[t] for t in sorted(by_t)]
    if not s.bars:
        s.notes.append("No spot data yet: the history download is still running, and live spot needs TWELVEDATA_API_KEY in backend/.env.")
    elif is_open and tf != "1d" and now - s.bars[-1].t > STALE_AFTER:
        ended = dt.datetime.fromtimestamp(s.bars[-1].t, dt.timezone.utc).strftime("%d %b %H:%M UTC")
        s.notes.append(f"Spot history ends at {ended}. Live spot needs TWELVEDATA_API_KEY in backend/.env." if not key
                       else f"The last spot bar is from {ended}: live data is not arriving.")
    return s


def futures(symbol: str, tf: str) -> Series:
    bars = _cached(f"futures|{symbol}|{tf}", FUTURES_TTL[tf], lambda: yahoo_futures.fetch(symbol, tf))
    return Series(symbol=symbol, tf=tf, source="futures", provider="Yahoo GC=F", bars=bars,
                  notes=["These are COMEX futures, not spot: the price level differs from XAUUSD, and it can jump when the contract rolls."])


def series(symbol: str, tf: str, source: str | None = None) -> Series:
    return spot(symbol, tf) if (source or default_source()) == "spot" else futures(symbol, tf)


QUOTE_TTL = 5               # seconds: two clicks in a row share one call
FRESH_BAR = 90              # seconds: a futures bar this recent counts as the live price


@dataclass
class Quote:
    price: float
    at: float               # unix seconds the price was read
    provider: str


def quote(symbol: str, source: str | None = None) -> Quote | None:
    """The price right now, or None when there is no fresh one. Spot asks Twelve Data for its latest price (one call);
    futures use Yahoo's newest 1-minute bar when it is recent."""
    if (source or default_source()) == "spot":
        key = get_settings().twelvedata_api_key
        if not key:
            return None

        def load():
            try:
                return Quote(twelvedata.price(symbol, key), time.time(), "Twelve Data")
            except twelvedata.TwelveDataError:
                return None
        return _cached(f"quote|{symbol}", QUOTE_TTL, load)
    bars = futures(symbol, "1m").bars
    if bars and time.time() - bars[-1].t <= FRESH_BAR:
        return Quote(bars[-1].c, time.time(), "Yahoo GC=F")
    return None
