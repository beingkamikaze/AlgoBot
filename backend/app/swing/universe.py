"""A point-in-time stock universe for swing research: the 100 most-traded NSE stocks at each month end,
chosen with data up to that day only. Testing on today's Nifty 100 picks the stocks that went up (they
are in the index today because they rose), which flatters any long or trend strategy.

Rule, fixed on 2026-11-11 before any result on it:
  at the last NIFTY session of each month, every stock with a bar that day, at least 252 sessions of
  history, and bars on at least 80% of the last 126 NIFTY sessions, is ranked by its MEDIAN daily traded
  value (close x volume) over those 126 sessions; the top 100 are the universe from that day's close
  until the next month end's close. (Nifty 100 uses free-float market cap, which we do not have
  historically; traded value is the usual stand-in and also keeps trading costs realistic.)

Kite only keeps instruments still trading, so stocks delisted since (HDFC Ltd after its 2023 merger,
DHFL, ...) are missing: some survivorship remains, much less than with today's index list.

Built from backend/data/bars/1d (`app.market.backfill --tf 1d --symbols ALL_EQ`) and saved to
backend/data/bars/universe_liquid100.json, so backtests are reproducible offline.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json

import numpy as np

from app.market import store
from app.swing import data
from app.swing.data import Daily
from app.swing.strategies import month_ends

SIZE = 100
WINDOW = 126
MIN_HISTORY = 252
MIN_TRADED_SHARE = 0.8
NOT_STOCKS = ("NIFTY", "BANKNIFTY", "INDIAVIX", "FINNIFTY", "NIFTYIT")
FILE = store.ROOT / "universe_liquid100.json"


def stored_symbols() -> list[str]:
    out = sorted(p.name.replace(".json.gz", "").replace("_AND_", "&") for p in (store.ROOT / "1d").glob("*.json.gz"))
    return [s for s in out if s not in NOT_STOCKS]


def scores(dates: list[str], c: np.ndarray, v: np.ndarray, market_dates: list[str], me_idx: list[int]) -> dict[str, float]:
    """Month-end date -> median traded value, for the month ends where the stock is eligible."""
    out = {}
    pos = {d: i for i, d in enumerate(dates)}
    value = c * v
    for k in me_idx:
        d = market_dates[k]
        i = pos.get(d)
        if i is None or i + 1 < MIN_HISTORY or k + 1 < WINDOW:
            continue
        first = market_dates[k + 1 - WINDOW]
        j = bisect.bisect_left(dates, first)
        if i + 1 - j < MIN_TRADED_SHARE * WINDOW:
            continue
        out[d] = float(np.median(value[j:i + 1]))
    return out


def build(symbols: list[str] | None = None, market: str = "NIFTY", log=print) -> dict:
    """Members per month end, from the stored daily bars (each month end uses data up to that day only)."""
    m = data.raw(market)
    mdates = [data.day_of(b.t) for b in m]
    me_idx = [int(i) for i in np.flatnonzero(month_ends(mdates))]
    by_month: dict[str, list[tuple[float, str]]] = {}
    syms = symbols or stored_symbols()
    for n, s in enumerate(syms, 1):
        bars = [b for b in data.raw(s) if min(b.o, b.h, b.l, b.c) > 0]
        if len(bars) < MIN_HISTORY:
            continue
        dates = [data.day_of(b.t) for b in bars]
        c = np.array([b.c for b in bars])
        v = np.array([b.v for b in bars], dtype=float)
        for d, sc in scores(dates, c, v, mdates, me_idx).items():
            by_month.setdefault(d, []).append((sc, s))
        if n % 500 == 0:
            log(f"{n}/{len(syms)} symbols scored")
    members = {d: [s for _, s in sorted(v, reverse=True)[:SIZE]] for d, v in sorted(by_month.items())}
    members = {d: v for d, v in members.items() if len(v) == SIZE}
    out = {"rule": f"top {SIZE} by median traded value over {WINDOW} sessions, >= {MIN_HISTORY} sessions of history, "
                   f"bars on >= {MIN_TRADED_SHARE:.0%} of the window; set at each NIFTY month end",
           "built": dt.date.today().isoformat(), "symbols_scanned": len(syms), "members": members}
    FILE.write_text(json.dumps(out, separators=(",", ":")))
    return out


def load() -> dict[str, list[str]]:
    return json.loads(FILE.read_text())["members"] if FILE.exists() else {}


def ever_members(members: dict[str, list[str]]) -> list[str]:
    return sorted({s for v in members.values() for s in v})


def membership(f: Daily, members: dict[str, list[str]]) -> np.ndarray:
    """In the universe on day i: a member at the latest month end on or before day i."""
    months = sorted(members)
    sets = {d: set(v) for d, v in members.items()}
    out = np.zeros(len(f), bool)
    for i, d in enumerate(f.dates):
        k = bisect.bisect_right(months, d) - 1
        out[i] = k >= 0 and f.symbol in sets[months[k]]
    return out


def attach(frames: dict[str, Daily], members: dict[str, list[str]]) -> None:
    for f in frames.values():
        f.extra["in_universe"] = membership(f, members)


def equal_weight_index(frames: dict[str, Daily], members: dict[str, list[str]], dates: list[str]) -> dict[str, float]:
    """Holding the universe in equal amounts (members as of the previous session), as an index level per date.
    Blocked days (demergers, bad data) are left out of the average."""
    months = sorted(members)
    sets = {d: set(v) for d, v in members.items()}
    idx = {s: {d: i for i, d in enumerate(f.dates)} for s, f in frames.items()}
    level, out = 1.0, {}
    for k, d in enumerate(dates):
        if k > 0:
            m = bisect.bisect_right(months, dates[k - 1]) - 1
            rets = []
            if m >= 0:
                for s in sets[months[m]]:
                    f = frames.get(s)
                    i = idx.get(s, {}).get(d)
                    if f is None or i is None or i == 0 or f.blocked[i] or f.dates[i - 1] != dates[k - 1]:
                        continue
                    rets.append(f.c[i] / f.c[i - 1] - 1)
            if rets:
                level *= 1 + float(np.mean(rets))
        out[d] = level
    return out


def baseline_pct(t, ew: dict[str, float], dates: list[str]) -> float | None:
    """What holding the whole universe in equal amounts earned over the same days as trade `t`, after the
    same charges (in % of the money): from the signal day's close to the exit (the exit day's close for exits
    at the close or on the entry day; the previous session's close for exits at the open). `dates` = sorted(ew).
    This is the bar a stock-picking or timing rule must clear: on a rising market any long-only rule makes money."""
    a = ew.get(t.signal_date)
    if t.exit_reason in ("time", "blocked", "end") or t.entry_date == t.exit_date:
        b = ew.get(t.exit_date)
    else:
        k = bisect.bisect_left(dates, t.exit_date) - 1
        b = ew.get(dates[k]) if k >= 0 else None
    if not a or not b:
        return None
    return (b / a - 1) * 100 - t.costs / (t.entry * t.qty) * 100


def main() -> int:
    """Build the universe file, then fetch Yahoo's corporate-action check data for any member that lacks it."""
    out = build()
    ms = out["members"]
    print(f"{len(ms)} month ends from {min(ms)} to {max(ms)}, {len(ever_members(ms))} stocks were ever members")
    have = data.load_actions()
    missing = [s for s in ever_members(ms) if s not in have]
    if missing:
        print(f"fetching Yahoo split events and closes for {len(missing)} members")
        data.refresh_actions(missing, log=lambda m: None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
