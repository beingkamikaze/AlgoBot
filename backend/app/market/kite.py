"""The one throttled Kite Connect data client: quotes, historical candles, the instruments dump and margins.

Every Kite DATA call in the app goes through `KiteData` (orders still go through `app.live.broker`, which has its own
order-rate bucket). Kite Connect's published limits, which this respects:
  - quote / ohlc / ltp: 1 request a second; 500 instruments per quote call, 1000 per ohlc or ltp call;
  - historical candles: 3 requests a second;
  - everything else (instruments, margins): 10 requests a second.

`current()` returns a client while someone is logged in to Kite today, else None: then the app falls back to Yahoo
and NSE exactly as before. The token is read through `app.live.session` (encrypted, never printed or logged).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

from app.core.calendar import IST

log = logging.getLogger("kite")

QUOTE_BATCH = 500                  # instruments per quote() call
OHLC_BATCH = 1000                  # instruments per ohlc() / ltp() call
KITE_INDEX = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "INDIAVIX": "INDIA VIX",
              "FINNIFTY": "NIFTY FIN SERVICE", "NIFTYIT": "NIFTY IT"}
INTERVAL = {"1m": "minute", "5m": "5minute", "15m": "15minute", "1h": "60minute", "1d": "day"}
MAX_DAYS = {"1m": 60, "5m": 100, "15m": 200, "1h": 400, "1d": 2000}     # longest range per historical request
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "kite"
INSTRUMENT_FIELDS = ("instrument_token", "tradingsymbol", "name", "expiry", "strike", "tick_size", "lot_size",
                     "instrument_type", "segment", "exchange")


class KiteDataError(Exception):
    pass


class Throttle:
    """At most `per_second` calls a second, shared by every thread."""

    def __init__(self, per_second: float):
        self.gap, self.last, self.lock = 1.0 / per_second, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


THROTTLES = {"quote": Throttle(1), "historical": Throttle(3), "other": Throttle(10)}


class _Instruments:
    """Kite's instruments dump, once a day per exchange: in memory, and on disk so a restart does not download it again."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_exchange: dict[str, tuple[dt.date, list[dict]]] = {}

    def get(self, exchange: str, fetch: Callable[[], list[dict]], today: dt.date) -> list[dict]:
        with self.lock:
            hit = self.by_exchange.get(exchange)
            if hit and hit[0] == today:
                return hit[1]
            rows = self._load(exchange, today)
            if rows is None:
                rows = [_slim(r) for r in fetch()]
                self._save(exchange, today, rows)
            self.by_exchange[exchange] = (today, rows)
            return rows

    @staticmethod
    def _path(exchange: str, today: dt.date) -> Path:
        return CACHE_DIR / f"instruments_{exchange}_{today.isoformat()}.json.gz"

    def _load(self, exchange: str, today: dt.date) -> list[dict] | None:
        p = self._path(exchange, today)
        if not p.exists():
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            return None
        for r in rows:
            if r.get("expiry"):
                r["expiry"] = dt.date.fromisoformat(r["expiry"])
        return rows

    def _save(self, exchange: str, today: dt.date, rows: list[dict]) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            for old in CACHE_DIR.glob(f"instruments_{exchange}_*.json.gz"):
                old.unlink(missing_ok=True)
            p = self._path(exchange, today)
            tmp = p.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(rows, fh, default=lambda v: v.isoformat() if isinstance(v, dt.date) else str(v))
            tmp.replace(p)
        except OSError as e:
            log.warning("could not cache the %s instruments list: %s", exchange, e)


def _slim(r: dict) -> dict:
    out = {k: r.get(k) for k in INSTRUMENT_FIELDS}
    e = out.get("expiry")
    if isinstance(e, dt.datetime):
        out["expiry"] = e.date()
    elif isinstance(e, str) and e:
        out["expiry"] = dt.date.fromisoformat(e[:10])
    elif not e:
        out["expiry"] = None
    return out


_instruments = _Instruments()


class KiteData:
    """Wraps a kiteconnect.KiteConnect (or a fake with the same methods). Errors become KiteDataError."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def _call(self, bucket: str, fn, *a, **k):
        THROTTLES[bucket].wait()
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 - every SDK failure becomes one error type
            raise KiteDataError(f"Kite: {e}") from e

    # ---- credentials for the WebSocket (never logged)
    @property
    def api_key(self) -> str:
        return getattr(self.raw, "api_key", "")

    @property
    def access_token(self) -> str:
        return getattr(self.raw, "access_token", "")

    # ---- instruments
    def instruments(self, exchange: str, today: dt.date | None = None) -> list[dict]:
        today = today or dt.datetime.now(IST).date()
        return _instruments.get(exchange, lambda: self._call("other", self.raw.instruments, exchange), today)

    def tokens(self, today: dt.date | None = None) -> dict[str, dict]:
        """Our symbol (RELIANCE, NIFTY, INDIAVIX) -> its NSE instrument row."""
        by_name = {v: k for k, v in KITE_INDEX.items()}
        out: dict[str, dict] = {}
        for r in self.instruments("NSE", today):
            ts = r["tradingsymbol"]
            if r.get("segment") == "INDICES":
                if ts in by_name:
                    out[by_name[ts]] = r
            elif r.get("instrument_type") == "EQ":
                out[ts] = r
        return out

    def nfo(self, tradingsymbol: str, today: dt.date | None = None) -> dict | None:
        return next((r for r in self.instruments("NFO", today) if r["tradingsymbol"] == tradingsymbol), None)

    # ---- quotes
    def quote(self, keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(keys), QUOTE_BATCH):
            out.update(self._call("quote", self.raw.quote, keys[i:i + QUOTE_BATCH]) or {})
        return out

    def ohlc(self, keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(keys), OHLC_BATCH):
            out.update(self._call("quote", self.raw.ohlc, keys[i:i + OHLC_BATCH]) or {})
        return out

    # ---- candles
    def historical(self, token: int, since: dt.date | dt.datetime, until: dt.date | dt.datetime, tf: str) -> list[dict]:
        return self._call("historical", self.raw.historical_data, token, since, until, INTERVAL[tf]) or []

    # ---- margins
    def order_margins(self, orders: list[dict]) -> list[dict]:
        return self._call("other", self.raw.order_margins, orders) or []

    def basket_margins(self, orders: list[dict]) -> dict:
        return self._call("other", self.raw.basket_order_margins, orders, consider_positions=False) or {}


def key_for(symbol: str) -> str:
    """Our symbol -> Kite's quote key: NSE:RELIANCE, NSE:NIFTY 50."""
    s = symbol.upper()
    return f"NSE:{KITE_INDEX[s]}" if s in KITE_INDEX else f"NSE:{s}"


# ---------------------------------------------------------------- is anyone logged in to Kite today?

_state = {"client": None, "checked": 0.0}
_state_lock = threading.Lock()
RECHECK_SECONDS = 60


def _default_resolver():
    from app.live import session
    b = session.any_broker()
    return getattr(b, "k", None) if b is not None else None      # a fake broker without `.k` means: no Kite data


resolver: Callable[[], object | None] = _default_resolver        # tests swap this (tests/conftest.py)


def current() -> KiteData | None:
    """Today's Kite data client, or None when nobody is logged in to Kite (checked at most once a minute)."""
    with _state_lock:
        if time.monotonic() - _state["checked"] < RECHECK_SECONDS:
            return _state["client"]
    try:
        raw = resolver()
    except Exception as e:  # noqa: BLE001 - no database, no token: no Kite
        log.debug("no Kite session: %s", e)
        raw = None
    with _state_lock:
        old = _state["client"]
        _state["client"] = KiteData(raw) if raw is not None and (old is None or old.raw is not raw) else (old if raw is not None else None)
        _state["checked"] = time.monotonic()
        return _state["client"]


def forget() -> None:
    """Drop the cached answer (a call failed with an auth error, or tests)."""
    with _state_lock:
        _state["client"], _state["checked"] = None, 0.0


def is_auth_error(e: Exception) -> bool:
    text = str(e).lower()
    return "token" in text and ("invalid" in text or "expired" in text) or "403" in text
