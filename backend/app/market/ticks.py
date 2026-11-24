"""Live ticks from Kite's WebSocket (KiteTicker), turned into 1-, 5- and 15-minute bars.

While someone is logged in to Kite today, `FeedLoop` keeps one WebSocket open from 09:00 to 15:35 IST and
subscribes (full mode, so each tick carries the exchange's timestamp):
  - NIFTY, BANKNIFTY and INDIA VIX, and the Nifty 100 stocks;
  - every instrument of an open paper or live position (futures contracts and option legs too), added and removed
    as positions open and close.
Kite allows 3000 instruments per WebSocket connection (and 3 connections per API key): the list is capped at 3000,
open positions first.

Bars: a tick's price updates the bar its timestamp falls in (bars start at 09:15 and every 1/5/15 minutes after);
volume is the change in Kite's day volume between ticks. A bar the socket did not see from its start (the app
started, or the connection dropped, in the middle of it) is never saved as complete: the Kite historical API fills it.
Completed bars are merged into the bar store every few minutes, and after the close (15:40) today's bars are fetched
again from Kite's historical API and overwrite the tick-built ones, so a dropped tick never stays in the history.

The token is never logged. The login is never automated: no login today, no socket.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from app.core.calendar import IST, is_trading_day
from app.market import kite as K
from app.market.models import Bar

log = logging.getLogger("ticks")

TICK_TFS = {"1m": 60, "5m": 300, "15m": 900}
OPEN_S = 9 * 3600 + 15 * 60
CLOSE_S = 15 * 3600 + 30 * 60
MAX_TOKENS = 3000                          # Kite: instruments per WebSocket connection
SOCKET_FROM = dt.time(9, 0)
SOCKET_UNTIL = dt.time(15, 35)
CORRECT_AFTER = dt.time(15, 40)
PERSIST_EVERY = 1800                       # seconds between writes of tick bars to the bar store (live reads use memory)
FRESH_SECONDS = 120                        # a symbol's tick bars count as live if a tick came this recently


def _midnight(d: dt.date) -> int:
    return int(dt.datetime.combine(d, dt.time(0, 0), IST).timestamp())


class BarBuilder:
    """Today's bars from ticks, per symbol and timeframe. Thread-safe; pure (no network), so tests drive it directly."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.day: dt.date | None = None
        self._reset(None)

    def _reset(self, day: dt.date | None) -> None:
        self.day = day
        self.forming: dict[tuple[str, str], list] = {}         # [t, o, h, l, c, v, partial]
        self.done: dict[tuple[str, str], list[Bar]] = {}
        self.unsaved: set[tuple[str, str]] = set()
        self.cum_vol: dict[str, int] = {}
        self.last: dict[str, tuple[float, float]] = {}          # symbol -> (price, unix time of the tick)
        self.seen_at: dict[str, float] = {}                     # symbol -> monotonic time the last tick arrived
        self.prev_close: dict[str, float] = {}
        self.since = 0.0                                        # unix time of the last (re)connect

    def mark_gap(self, now: dt.datetime) -> None:
        """The socket (re)connected: bars already forming missed ticks, so they are not complete."""
        with self.lock:
            if self.day != now.astimezone(IST).date():
                self._reset(now.astimezone(IST).date())
            for cur in self.forming.values():
                cur[6] = True
            self.since = now.timestamp()

    def on_tick(self, symbol: str, price: float, cum_volume: int | None, when: dt.datetime,
                prev_close: float | None = None) -> None:
        local = when.astimezone(IST)
        with self.lock:
            if self.day != local.date():
                since = self.since
                self._reset(local.date())
                self.since = since
            self.last[symbol] = (float(price), local.timestamp())
            self.seen_at[symbol] = time.monotonic()
            if prev_close:
                self.prev_close[symbol] = float(prev_close)
            dv = 0
            if cum_volume is not None:
                before = self.cum_vol.get(symbol)
                dv = max(int(cum_volume) - before, 0) if before is not None else 0
                self.cum_vol[symbol] = int(cum_volume)
            secs = local.hour * 3600 + local.minute * 60 + local.second
            if not OPEN_S <= secs < CLOSE_S:
                return
            base = _midnight(local.date())
            for tf, step in TICK_TFS.items():
                start = base + (secs // step) * step
                key = (symbol, tf)
                cur = self.forming.get(key)
                if cur is not None and start < cur[0]:
                    continue                                    # a late tick for a bar already closed
                if cur is not None and start > cur[0]:
                    self._close(key)
                    cur = None
                if cur is None:
                    self.forming[key] = [start, price, price, price, price, dv, start < self.since]
                else:
                    cur[2] = max(cur[2], price)
                    cur[3] = min(cur[3], price)
                    cur[4] = price
                    cur[5] += dv

    def _close(self, key: tuple[str, str]) -> None:
        cur = self.forming.pop(key)
        if cur[6]:
            return                                              # not seen from its start: Kite historical fills it
        t, o, h, lo, c, v, _ = cur
        self.done.setdefault(key, []).append(Bar(t=t, o=o, h=h, l=lo, c=c, v=int(v), complete=True))
        self.unsaved.add(key)

    def roll(self, now: dt.datetime) -> None:
        """Close every bar whose time is up, even if no tick came after it (an illiquid stock, or the close)."""
        ts = now.timestamp()
        with self.lock:
            for key in [k for k, cur in self.forming.items() if cur[0] + TICK_TFS[k[1]] <= ts]:
                self._close(key)

    def bars(self, symbol: str, tf: str, forming: bool = True) -> list[Bar]:
        with self.lock:
            out = list(self.done.get((symbol, tf), []))
            cur = self.forming.get((symbol, tf))
            if forming and cur is not None and not cur[6]:
                t, o, h, lo, c, v, _ = cur
                out.append(Bar(t=t, o=o, h=h, l=lo, c=c, v=int(v), complete=False))
            return out

    def missing(self, symbol: str, tf: str, now: dt.datetime) -> bool:
        """True if any bar of today's session up to now is not in the tick bars (late start or a dropped socket)."""
        local = now.astimezone(IST)
        step = TICK_TFS[tf]
        base = _midnight(local.date())
        secs = min(local.hour * 3600 + local.minute * 60 + local.second, CLOSE_S)
        expected = range(base + OPEN_S, base + (secs // step) * step, step)
        with self.lock:
            have = {b.t for b in self.done.get((symbol, tf), [])}
        return any(t not in have for t in expected)

    def fresh(self, symbol: str) -> bool:
        with self.lock:
            seen = self.seen_at.get(symbol)
        return seen is not None and time.monotonic() - seen < FRESH_SECONDS

    def last_price(self, symbol: str) -> tuple[float, float] | None:
        with self.lock:
            return self.last.get(symbol)

    def drain(self) -> dict[tuple[str, str], list[Bar]]:
        """Completed bars not yet written to the bar store."""
        with self.lock:
            out = {k: list(self.done.get(k, [])) for k in self.unsaved}
            self.unsaved.clear()
            return out


builder = BarBuilder()


class Ticker:
    """One KiteTicker connection. Its reconnect (with backoff, up to 50 tries) is KiteTicker's own; when it gives
    up, or Kite refuses the token, `dead` is set and FeedLoop starts a new one later."""

    def __init__(self, kd: K.KiteData, bars: BarBuilder, factory=None):
        self.call = lambda fn: fn()
        if factory is None:
            from kiteconnect import KiteTicker as factory
            from twisted.internet import reactor
            self.call = reactor.callFromThread          # Twisted is not thread-safe: socket writes go through its thread
        self.kd, self.bars = kd, bars
        self.ws = factory(kd.api_key, kd.access_token, reconnect=True, reconnect_max_tries=50, reconnect_max_delay=60)
        self.ws.on_ticks = self._on_ticks
        self.ws.on_connect = self._on_connect
        self.ws.on_close = self._on_close
        self.ws.on_error = self._on_error
        self.ws.on_noreconnect = self._on_noreconnect
        self.ws.on_reconnect = self._on_reconnect
        self.lock = threading.Lock()
        self.symbols: dict[int, str] = {}             # wanted: instrument token -> our symbol
        self.subscribed: set[int] = set()
        self.connected = False
        self.dead = False
        self.reason: str | None = None
        self.ticks = 0

    def start(self) -> None:
        self.ws.connect(threaded=True)

    def close(self) -> None:
        self.dead = True

        def shut():
            try:
                self.ws.close(1000, "token expired or market closed")   # closes this socket; the reactor keeps running
            except Exception:  # noqa: BLE001
                pass
        self.call(shut)

    def want(self, symbols: dict[int, str]) -> None:
        with self.lock:
            self.symbols = dict(symbols)
        if self.connected:
            self.call(self._sync)

    def _sync(self) -> None:
        with self.lock:
            want = set(self.symbols)
            add, drop = sorted(want - self.subscribed), sorted(self.subscribed - want)
        try:
            if add:
                self.ws.subscribe(add)
                self.ws.set_mode(self.ws.MODE_FULL, add)
            if drop:
                self.ws.unsubscribe(drop)
            with self.lock:
                self.subscribed = (self.subscribed | set(add)) - set(drop)
        except Exception as e:  # noqa: BLE001 - the socket closed under us: the reconnect resubscribes
            log.warning("tick subscription change failed: %s", e)

    def _on_connect(self, ws, response) -> None:
        self.connected = True
        self.bars.mark_gap(dt.datetime.now(IST))
        with self.lock:
            self.subscribed = set()
        self._sync()
        log.info("Kite ticks connected: %d instruments", len(self.symbols))

    def _on_reconnect(self, ws, attempts: int) -> None:
        log.info("Kite ticks reconnecting (attempt %d)", attempts)

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        now = dt.datetime.now(IST)
        with self.lock:
            names = dict(self.symbols)
        for t in ticks:
            sym = names.get(t.get("instrument_token"))
            if sym is None or t.get("last_price") is None:
                continue
            when = t.get("exchange_timestamp") or t.get("last_trade_time") or now
            if when.tzinfo is None:
                when = when.astimezone(IST)                 # KiteTicker gives naive local times
            self.bars.on_tick(sym, float(t["last_price"]), t.get("volume_traded"), when, (t.get("ohlc") or {}).get("close"))
            self.ticks += 1

    def _on_close(self, ws, code, reason) -> None:
        self.connected = False
        log.info("Kite ticks closed (%s)", code)

    def _on_error(self, ws, code, reason) -> None:
        self.connected = False
        self.reason = str(reason)[:200] if reason else f"error {code}"
        if code == 403 or "403" in str(reason):              # the token was refused: stop until a new login
            self.dead = True
            try:
                ws.stop_retry()
            except Exception:  # noqa: BLE001
                pass

    def _on_noreconnect(self, ws) -> None:
        self.dead = True
        self.connected = False


# ---------------------------------------------------------------- what to subscribe

BASE_INDICES = ("NIFTY", "BANKNIFTY", "INDIAVIX")


def base_symbols() -> list[str]:
    from app.market import nse
    try:
        stocks = nse.constituents("NIFTY100")
    except Exception as e:  # noqa: BLE001 - NSE down and no cached list: indices only
        log.warning("Nifty 100 list unavailable: %s", e)
        stocks = []
    return list(BASE_INDICES) + [s for s in stocks if s not in K.KITE_INDEX]


def position_instruments() -> list[tuple[str, str]]:
    """(exchange, tradingsymbol) of every open paper or live position, futures contracts and option legs included."""
    from app.db import db
    out: list[tuple[str, str]] = []
    try:
        rows = db().select("trades", {"exit_ts": "is.null"}, columns="symbol,entry_context")
    except Exception as e:  # noqa: BLE001 - no database: no positions to follow
        log.debug("open positions unavailable: %s", e)
        return out
    for r in rows:
        ctx = r.get("entry_context") or {}
        kind = ctx.get("kind", "equity")
        if kind == "option":
            out += [("NFO", lg["tradingsymbol"]) for lg in ctx.get("legs") or [] if lg.get("tradingsymbol")]
        elif kind == "future":
            out.append(("NFO", r["symbol"]))
        elif r.get("symbol"):
            out.append(("NSE", (ctx.get("data_symbol") or r["symbol"]).upper()))
    return list(dict.fromkeys(out))


def wanted(kd: K.KiteData, today: dt.date) -> dict[int, str]:
    """Instrument token -> our symbol (NSE stocks and indices by our name, NFO contracts by Kite's tradingsymbol)."""
    nse_rows = kd.tokens(today)
    positions: dict[int, str] = {}
    for exch, ts in position_instruments():
        row = nse_rows.get(ts) if exch == "NSE" else kd.nfo(ts, today)
        if row:
            positions[int(row["instrument_token"])] = ts
    base = {int(nse_rows[s]["instrument_token"]): s for s in base_symbols() if s in nse_rows}
    out = dict(positions)
    for tok, sym in base.items():
        if len(out) >= MAX_TOKENS:
            log.warning("Kite allows %d instruments per connection: %d left out", MAX_TOKENS, len(base) - len(out))
            break
        out.setdefault(tok, sym)
    return out


# ---------------------------------------------------------------- after the close: Kite's own bars replace tick bars

def correct_day(kd: K.KiteData, symbols: list[str], day: dt.date, tfs=("1m", "5m", "15m"), log_=log.info) -> dict:
    """Overwrite `day`'s bars in the bar store with Kite's historical candles (fills any tick-built gaps)."""
    from app.market import feed, store
    report = {"symbols": 0, "bars": 0, "errors": []}
    nse_rows = kd.tokens(day)
    end = dt.datetime.combine(day, dt.time(15, 30), IST)
    for sym in symbols:
        row = nse_rows.get(sym) or kd.nfo(sym, day)
        if not row:
            continue
        for tf in tfs:
            try:
                rows = kd.historical(int(row["instrument_token"]), dt.datetime.combine(day, dt.time(9, 15)),
                                     dt.datetime.combine(day, dt.time(15, 30)), tf)
            except K.KiteDataError as e:
                report["errors"].append(f"{sym} {tf}: {e}")
                continue
            new = [b for b in feed.to_bars(rows, tf, end + dt.timedelta(minutes=1)) if b.complete]
            if new:
                with store.locked(sym, tf):
                    store.write(sym, tf, store.merge(store.read(sym, tf), new))
                report["bars"] += len(new)
        report["symbols"] += 1
    log_(f"Kite end-of-day correction for {day}: {report['symbols']} symbols, {report['bars']} bars")
    return report


def persist(bars: BarBuilder) -> int:
    from app.market import store
    n = 0
    for (sym, tf), new in bars.drain().items():
        if not new:
            continue
        try:
            with store.locked(sym, tf):
                store.write(sym, tf, store.merge(store.read(sym, tf), new))
            n += len(new)
        except OSError as e:
            log.warning("could not save tick bars for %s %s: %s", sym, tf, e)
    return n


class FeedLoop:
    """Starts the socket after a Kite login, keeps its instrument list current, stops it when the token expires or
    the session ends, saves completed bars, and runs the after-close correction."""

    def __init__(self, every: float = 15.0, ticker_factory=None, bars: BarBuilder | None = None):
        self.every, self._stop = every, threading.Event()
        self.bars = bars or builder
        self.factory = ticker_factory
        self.ticker: Ticker | None = None
        self.failures = 0
        self.next_start = 0.0
        self.last_persist = time.monotonic()
        self.corrected: dt.date | None = None
        self.thread: threading.Thread | None = None
        self.state: dict = {"status": "starting"}

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="kite-ticks")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        persist(self.bars)

    def _close(self) -> None:
        if self.ticker:
            self.ticker.close()
            self.ticker = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step(dt.datetime.now(IST))
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.exception("tick loop failed: %s", e)
                self.state = {"status": "error", "error": str(e)[:200]}
            self._stop.wait(self.every)

    def step(self, now: dt.datetime) -> None:
        now = now.astimezone(IST)
        kd = K.current()
        session_day = is_trading_day(now.date())[0]
        in_window = session_day and SOCKET_FROM <= now.time() < SOCKET_UNTIL
        if self.ticker and (kd is None or not in_window or self.ticker.kd.raw is not getattr(kd, "raw", None)):
            self._close()                                   # token expired, logged out, new login, or after hours
        if self.ticker and self.ticker.dead:
            self.failures += 1
            self.next_start = time.monotonic() + min(30 * 2 ** (self.failures - 1), 600)
            log.warning("Kite ticks stopped (%s): retrying in %ds", self.ticker.reason or "no reconnect",
                        int(self.next_start - time.monotonic()))
            self._close()
        if kd is not None and in_window:
            if self.ticker is None and time.monotonic() >= self.next_start:
                self.ticker = Ticker(kd, self.bars, self.factory)
                self.ticker.want(wanted(kd, now.date()))
                self.ticker.start()
            elif self.ticker is not None:
                self.ticker.want(wanted(kd, now.date()))
                if self.ticker.ticks:
                    self.failures = 0
        self.bars.roll(now)
        if time.monotonic() - self.last_persist >= PERSIST_EVERY or (self.ticker is None and self.bars.unsaved):
            persist(self.bars)
            self.last_persist = time.monotonic()
        if kd is not None and session_day and now.time() >= CORRECT_AFTER and self.corrected != now.date():
            self.corrected = now.date()
            persist(self.bars)
            syms = sorted({s for (s, _tf) in self.bars.done} | set(base_symbols()))
            threading.Thread(target=correct_day, args=(kd, syms, now.date()), daemon=True, name="kite-correct").start()
        self.state = {"status": "live" if self.ticker and self.ticker.connected else
                      ("connecting" if self.ticker else ("no Kite login today" if kd is None else "closed")),
                      "instruments": len(self.ticker.symbols) if self.ticker else 0,
                      "ticks": self.ticker.ticks if self.ticker else 0, "corrected": self.corrected.isoformat() if self.corrected else None}


loop: FeedLoop | None = None


def status() -> dict:
    return dict(loop.state) if loop else {"status": "off"}
