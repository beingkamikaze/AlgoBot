"""Daily bars for swing backtests, adjusted for splits and bonuses, offline and reproducible.

Kite's daily candles (backend/data/bars/1d, from `app.market.backfill --tf 1d`) are raw prices: a 1:1 bonus
shows up as a 50% overnight "crash". For a position held for days that would be a fake stop-out, so:

1. `refresh_actions()` (needs the internet, run once) downloads Yahoo's split events (Yahoo books NSE
   bonuses as splits: a 1:1 bonus is "2:1") and Yahoo's split-adjusted daily closes, and saves both to
   backend/data/bars/corporate_actions.json. Backtests only read that file.
2. `load()` divides every Kite price before an event by its ratio (volume multiplied), but only if Kite
   itself shows the event: the raw close-to-open gap on the ex-date must be nearer 1/ratio than 1 (log
   distance under half of log(ratio)). Events Kite does not show are reported as `rejected`.
   Finding on 2026-11-11: Kite's DAILY candles are already split- and bonus-adjusted (e.g. RELIANCE's
   1:1 bonuses of 2017 and 2024 show no gap), so almost every Yahoo event is rejected, correctly.
3. Then the adjusted Kite close-to-close returns are compared with Yahoo's. A day where they differ by more
   than 3%, both on the day and over the four days around it (so a one-day shift in Yahoo's dates is not
   flagged), is something else: a demerger (Siemens Energy, April 2025), a missed event or bad data. Any
   overnight gap over 20% that Yahoo does not confirm is treated the same way. Such days are `blocked`:
   the swing engine neither enters nor holds a position across them. Real crashes are NOT blocked,
   because Yahoo shows the same fall. Bars with a zero price (before listing) are dropped.

Dividends are not adjusted (on either side): a holder would also receive them, so returns are slightly
understated, and the Nifty comparison uses the price index, which also excludes dividends.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field

import numpy as np

from app.market import store
from app.market.models import Bar

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))   # no app.core import needed for a fixed offset
ACTIONS = store.ROOT / "corporate_actions.json"
CHECK_SHARE = 0.5            # the ex-date log gap must be within half of log(ratio) of -log(ratio)
GAP_LIMIT = 0.2              # overnight gaps larger than this need Yahoo's confirmation
DISAGREE = 0.03              # adjusted Kite vs Yahoo daily return difference that blocks a day


def day_of(t: int) -> str:
    return dt.datetime.fromtimestamp(t, IST).date().isoformat()


@dataclass
class Daily:
    symbol: str
    dates: list[str]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray          # noqa: E741
    c: np.ndarray
    v: np.ndarray
    blocked: np.ndarray    # bool per day: no position may be entered on or held across this day's open
    extra: dict = field(default_factory=dict)   # per-day series a strategy needs from outside (ranks, market filter)

    def __len__(self) -> int:
        return len(self.dates)

    def until(self, last_date: str) -> "Daily":
        """Only the days before `last_date` (the held-back cutoff): later prices are never seen."""
        n = sum(1 for d in self.dates if d < last_date)
        return Daily(self.symbol, self.dates[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.v[:n],
                     self.blocked[:n], {k: v[:n] for k, v in self.extra.items()})


def raw(symbol: str) -> list[Bar]:
    """Stored daily bars, one per IST date (the last one wins if two sources disagree on the timestamp)."""
    by_day: dict[str, Bar] = {}
    for b in store.read(symbol, "1d"):
        by_day[day_of(b.t)] = b
    return [by_day[d] for d in sorted(by_day)]


def frame_from_bars(symbol: str, bars: list[Bar], blocked: set[str] | None = None) -> Daily:
    dates = [day_of(b.t) for b in bars]
    a = lambda k: np.array([getattr(b, k) for b in bars], dtype=float)  # noqa: E731
    bl = np.array([d in (blocked or set()) for d in dates], dtype=bool)
    return Daily(symbol, dates, a("o"), a("h"), a("l"), a("c"), a("v"), bl)


# ---------------------------------------------------------------- corporate actions

def refresh_actions(symbols: list[str], log=print) -> dict:
    """Yahoo split events and split-adjusted closes for each symbol, saved for offline use."""
    from app.market import yahoo
    from app.market.symbols import to_yahoo
    out = load_actions()
    for s in symbols:
        try:
            doc = yahoo._get(f"/v8/finance/chart/{to_yahoo(s)}?period1=1230768000&period2=1900000000"
                             "&interval=1d&events=split")
        except Exception as e:  # noqa: BLE001 - a symbol Yahoo lacks just stays unadjusted (and reported)
            log(f"{s}: {e}")
            continue
        r = doc["chart"]["result"][0]
        splits = [{"date": day_of(int(v["date"])), "ratio": float(v["numerator"]) / float(v["denominator"])}
                  for v in (r.get("events") or {}).get("splits", {}).values()]
        closes = r["indicators"]["quote"][0].get("close") or []
        yc = {day_of(t): c for t, c in zip(r.get("timestamp") or [], closes) if c is not None}
        out[s] = {"splits": sorted(splits, key=lambda x: x["date"]), "yahoo_close": yc}
        log(f"{s}: {len(splits)} split/bonus events, {len(yc)} Yahoo closes")
    ACTIONS.write_text(json.dumps(out, separators=(",", ":")))
    return out


def load_actions() -> dict:
    return json.loads(ACTIONS.read_text()) if ACTIONS.exists() else {}


def adjust(bars: list[Bar], splits: list[dict], yahoo_close: dict[str, float] | None = None) -> tuple[list[Bar], dict]:
    """Split/bonus-adjusted bars plus a report: applied and rejected events, blocked days."""
    report: dict = {"applied": [], "rejected": [], "blocked": [], "unexplained_gaps": []}
    bars = [b for b in bars if min(b.o, b.h, b.l, b.c) > 0]
    dates = [day_of(b.t) for b in bars]
    factor = np.ones(len(bars))
    for ev in splits:
        r = ev["ratio"]
        if r <= 0 or abs(r - 1) < 1e-9:
            continue
        k = next((i for i, d in enumerate(dates) if d >= ev["date"]), None)
        if k is None or k == 0:
            continue
        gap = bars[k].o / bars[k - 1].c
        rec = {"date": dates[k], "ratio": round(r, 4), "raw_gap_pct": round((gap - 1) * 100, 1)}
        if abs(math.log(gap * r)) < CHECK_SHARE * abs(math.log(r)):
            factor[:k] *= r
            report["applied"].append(rec)
        else:
            report["rejected"].append(rec)
    out = [Bar(t=b.t, o=b.o / f, h=b.h / f, l=b.l / f, c=b.c / f, v=int(round(b.v * f)), complete=True)
           for b, f in zip(bars, factor)]
    yc = yahoo_close or {}

    def ret(i: int, j: int) -> tuple[float, float | None]:
        k = math.log(out[j].c / out[i].c)
        a, b = yc.get(dates[i]), yc.get(dates[j])
        return k, (math.log(b / a) if a and b else None)

    blocked: dict[str, dict] = {}
    for i in range(1, len(out)):
        kr, yr = ret(i - 1, i)
        gap = out[i].o / out[i - 1].c - 1
        if abs(gap) > GAP_LIMIT and (yr is None or abs(kr - yr) > DISAGREE):
            report["unexplained_gaps"].append({"date": dates[i], "gap_pct": round(gap * 100, 1)})
            blocked[dates[i]] = {"date": dates[i], "kite_pct": round(kr * 100, 1),
                                 "yahoo_pct": round(yr * 100, 1) if yr is not None else None}
            continue
        if yr is None or abs(kr - yr) <= DISAGREE:
            continue
        kw, yw = ret(max(i - 2, 0), min(i + 1, len(out) - 1))
        if yw is not None and abs(kw - yw) > DISAGREE:
            blocked[dates[i]] = {"date": dates[i], "kite_pct": round(kr * 100, 1), "yahoo_pct": round(yr * 100, 1)}
    report["blocked"] = sorted(blocked.values(), key=lambda x: x["date"])
    return out, report


def load(symbol: str, actions: dict | None = None) -> tuple[Daily, dict]:
    actions = load_actions() if actions is None else actions
    a = actions.get(symbol)
    bars = raw(symbol)
    if a is None:
        d = frame_from_bars(symbol, bars)
        return d, {"applied": [], "rejected": [], "blocked": [], "unexplained_gaps": [], "no_yahoo": True}
    adj, rep = adjust(bars, a["splits"], a["yahoo_close"])
    return frame_from_bars(symbol, adj, {x["date"] for x in rep["blocked"]}), rep
