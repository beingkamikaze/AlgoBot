"""Backfill the bar store with years of history from Kite's historical-data API (needs a Kite Connect plan
and today's Kite login). Yahoo only gives 60 days of 5-minute bars; Kite gives years, so backtests and the
Lab's held-back test get far more sessions.

Run from backend/ while logged in to Kite today:
    .venv/bin/python -m app.market.backfill                      # Nifty 100 + indices, 5m and 15m, from 2021
    .venv/bin/python -m app.market.backfill --since 2023-01-01 --tf 5m --symbols RELIANCE,TCS

Bars go into the same files as Yahoo's (`store.merge`), so nothing else changes. Kite's prices are NOT
adjusted for splits and bonuses: that only matters across the day of a split (previous-day levels, ATR).
Kite only keeps history for instruments still trading, so expired futures contracts are not available;
index futures backtests use the index's own bars, which are.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
import time

from app.core.calendar import IST
from app.market import nse, store
from app.market.models import TIMEFRAMES, Bar

KITE_INTERVAL = {"5m": "5minute", "15m": "15minute", "1h": "60minute", "1d": "day"}
MAX_DAYS = {"5m": 100, "15m": 200, "1h": 400, "1d": 2000}      # Kite's longest range per request
KITE_INDEX = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "INDIAVIX": "INDIA VIX",
              "FINNIFTY": "NIFTY FIN SERVICE", "NIFTYIT": "NIFTY IT"}
PER_SECOND = 3                                                 # Kite's limit for historical data


class Throttle:
    def __init__(self, per_second: int):
        self.gap, self.last, self.lock = 1.0 / per_second, 0.0, threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


def tokens(kite) -> dict[str, int]:
    """Our symbol -> Kite instrument token, for NSE stocks and the indices."""
    out: dict[str, int] = {}
    by_name = {v: k for k, v in KITE_INDEX.items()}
    for row in kite.instruments("NSE"):
        ts = row["tradingsymbol"]
        if row.get("segment") == "INDICES":
            if ts in by_name:
                out[by_name[ts]] = int(row["instrument_token"])
        elif row.get("instrument_type") == "EQ":
            out[ts] = int(row["instrument_token"])
    return out


def chunks(since: dt.date, until: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    out, a = [], since
    while a <= until:
        b = min(a + dt.timedelta(days=days - 1), until)
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


def to_bars(rows: list[dict], tf: str, now: dt.datetime) -> list[Bar]:
    """Kite candles -> store bars; the bar still forming is dropped."""
    step = TIMEFRAMES[tf][1]
    out = []
    for r in rows:
        d = r["date"]
        if d.tzinfo is None:
            d = d.replace(tzinfo=IST)
        t = int(d.timestamp())
        if t + step > now.timestamp():
            continue
        out.append(Bar(t=t, o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]),
                       v=int(r.get("volume") or 0), complete=True))
    return out


def fetch(kite, token: int, tf: str, since: dt.date, until: dt.date, throttle: Throttle,
          now: dt.datetime) -> list[Bar]:
    bars: list[Bar] = []
    for a, b in chunks(since, until, MAX_DAYS[tf]):
        for attempt in range(3):
            throttle.wait()
            try:
                rows = kite.historical_data(token, a, b, KITE_INTERVAL[tf])
                break
            except Exception:  # noqa: BLE001 - rate limits and timeouts: back off, then give up on the symbol
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        bars += to_bars(rows, tf, now)
    return bars


def backfill(kite, symbols: list[str], tfs: list[str], since: dt.date, log=print) -> dict:
    now = dt.datetime.now(IST)
    tok = tokens(kite)
    from app.market.kite import THROTTLES
    throttle = THROTTLES["historical"]           # shared with the app's live Kite calls: one budget of 3 a second
    report = {"symbols": 0, "bars": 0, "missing": [], "errors": []}
    for n, sym in enumerate(symbols, 1):
        if sym not in tok:
            report["missing"].append(sym)
            continue
        for tf in tfs:
            try:
                new = fetch(kite, tok[sym], tf, since, now.date(), throttle, now)
            except Exception as e:  # noqa: BLE001 - one bad symbol never stops the rest
                report["errors"].append(f"{sym} {tf}: {e}")
                continue
            old = store.read(sym, tf)
            merged = store.merge(old, new)
            store.write(sym, tf, merged)
            report["bars"] += len(merged) - len(old)
            first = dt.datetime.fromtimestamp(merged[0].t, IST).date() if merged else None
            log(f"[{n}/{len(symbols)}] {sym} {tf}: {len(new)} bars from Kite, {len(merged)} stored (from {first})")
        report["symbols"] += 1
    return report


def all_equity_symbols(kite) -> list[str]:
    """Every ordinary NSE stock Kite lists today (EQ series): no bonds, SME/BE series (names with '-') or ETFs.
    For the swing research's point-in-time universe. Kite only has instruments still trading."""
    out = []
    for r in kite.instruments("NSE"):
        ts, name = r["tradingsymbol"], (r.get("name") or "").upper()
        if r.get("instrument_type") != "EQ" or r.get("segment") != "NSE" or "-" in ts:
            continue
        if "ETF" in name or "BEES" in ts or "ETF" in ts:
            continue
        out.append(ts)
    return sorted(set(out))


def default_symbols() -> list[str]:
    stocks = nse.constituents("NIFTY100")
    return ["NIFTY", "BANKNIFTY", "INDIAVIX"] + [s for s in stocks if s not in KITE_INDEX]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", default="2021-01-01", help="first day to download (YYYY-MM-DD)")
    ap.add_argument("--tf", default="5m,15m", help="timeframes, comma-separated: 5m, 15m, 1h, 1d")
    ap.add_argument("--symbols", default="", help="comma-separated, or ALL_EQ for every NSE stock (swing research); "
                                                   "default: NIFTY, BANKNIFTY, INDIAVIX + Nifty 100")
    ap.add_argument("--skip-existing", action="store_true", help="skip symbols that already have bars for this timeframe")
    a = ap.parse_args(argv)
    from app.live.session import any_broker
    b = any_broker()
    if b is None or not hasattr(b, "k"):
        print("No Kite session today: log in to Kite on the Live page first.", file=sys.stderr)
        return 1
    tfs = [t.strip() for t in a.tf.split(",") if t.strip()]
    bad = [t for t in tfs if t not in KITE_INTERVAL]
    if bad:
        print(f"unknown timeframe(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    if a.symbols.strip().upper() == "ALL_EQ":
        syms = all_equity_symbols(b.k)
    else:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or default_symbols()
    if a.skip_existing:
        syms = [s for s in syms if not all(store.read(s, t) for t in tfs)]
    r = backfill(b.k, syms, tfs, dt.date.fromisoformat(a.since))
    print(f"done: {r['symbols']} symbols, {r['bars']:,} new bars")
    if r["missing"]:
        print("not found on Kite:", ", ".join(r["missing"]))
    for e in r["errors"]:
        print("error:", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
