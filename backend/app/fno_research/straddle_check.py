"""How far the straddle study's ESTIMATED option prices sit from real ones: NSE bhavcopy end-of-day closes.

For each in-sample day that has a bhavcopy file (app.fno_research.bhavcopy) and full 5-minute bars:
  - the real ATM straddle close (call + put at the strike nearest the 15:30 index close, nearest weekly expiry,
    both legs traded that day),
  - the estimate at 15:30 under each clock with VIX as the IV (the study's 0-shift scenarios),
  - the IV the real straddle implies (calendar time, the market's convention) minus VIX: the "IV gap" that the
    study's +-2 point scenarios stand in for.
It also checks the study's expiry rule (weekday, moved to the session before a holiday) against the expiries
the bhavcopy itself lists.

End-of-day prices cannot show how the premium melts DURING the day, which is what decides the study (the clock).
Only the chain recorder's intraday snapshots can.

Run: `.venv/bin/python -m app.fno_research.straddle_check` from backend/.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

import numpy as np

from app.fno_research import straddle as sd

LAST_BAR = "15:25"           # its close is 15:30


def straddle_iv(spot: float, strike: float, t: float, price: float) -> float | None:
    """The single vol (calendar time) at which Black-76 call + put equals `price`; None when out of range."""
    lo, hi = 0.005, 3.0
    f = lambda v: sd.b76(True, spot, strike, t, v) + sd.b76(False, spot, strike, t, v)   # noqa: E731
    if t <= 0 or not (f(lo) <= price <= f(hi)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) < price else (lo, mid)
    return (lo + hi) / 2


def compare(day: sd.Day, close_spot: float, close_vix: float, rows, sessions: set[dt.date], mixed: dict) -> dict | None:
    expiry = sd.weekly_expiry(day.date, sessions)
    if expiry == day.date:
        return None                                  # 0 DTE: no time value left at 15:30 to compare
    k = round(close_spot / sd.STRIKE_STEP) * sd.STRIKE_STEP
    legs = {r.kind: r for r in rows if r.instrument.startswith("OPT") and r.expiry == expiry and r.strike == k}
    if set(legs) != {"CE", "PE"} or min(legs["CE"].volume, legs["PE"].volume) <= 0:
        return None
    real = legs["CE"].close + legs["PE"].close
    ts = dt.datetime.combine(day.date, dt.time(15, 30), sd.IST).timestamp()
    est = {}
    for c in ("trading", "mixed", "calendar"):
        tt, scale = sd.clock(day, expiry, sessions, sd.Scenario(0.0, c), mixed)
        t, tc, vol = tt(ts), sd.calendar_years(ts, expiry), close_vix / 100 * scale
        est[c] = sd.b76(True, close_spot, k, t, vol, tc) + sd.b76(False, close_spot, k, t, vol, tc)
    iv = straddle_iv(close_spot, k, sd.calendar_years(ts, expiry), real)
    return {"date": day.date.isoformat(), "dte": (expiry - day.date).days, "real": real, "est": est,
            "iv_gap": None if iv is None else iv * 100 - close_vix}


def run() -> dict:
    from app.fno_research import bhavcopy
    from app.market import store
    days, sessions, cut, _ = sd.load()
    mixed = sd.calibrate_mixed(days)
    by_day = {d.date: d for d in days}
    closes = {}
    for sym in ("NIFTY", "INDIAVIX"):
        for b in store.read(sym, "5m"):
            t = dt.datetime.fromtimestamp(b.t, sd.IST)
            if t.date() < cut and t.strftime("%H:%M") == LAST_BAR:
                closes[(sym, t.date())] = b.c
    out, expiry_mismatch, checked = [], [], 0
    for d in bhavcopy.stored_days():
        if d >= cut or d not in by_day or ("NIFTY", d) not in closes or ("INDIAVIX", d) not in closes:
            continue
        rows = bhavcopy.read_day(d) or []
        listed = sorted({r.expiry for r in rows if r.instrument.startswith("OPT") and r.expiry >= d})
        if listed:
            checked += 1
            if listed[0] != sd.weekly_expiry(d, sessions):
                expiry_mismatch.append((d.isoformat(), sd.weekly_expiry(d, sessions).isoformat(), listed[0].isoformat()))
        r = compare(by_day[d], closes[("NIFTY", d)], closes[("INDIAVIX", d)], rows, sessions, mixed)
        if r:
            out.append(r)
    return {"label": sd.ESTIMATE, "days": len(out), "expiry_days_checked": checked, "expiry_mismatches": expiry_mismatch,
            "summary": summarize(out), "rows": out}


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {}
    s: dict = {}
    for c in ("trading", "mixed", "calendar"):
        err = np.array([r["est"][c] / r["real"] - 1 for r in rows]) * 100
        s[f"error_pct_{c}"] = {"mean": round(float(err.mean()), 1), "median": round(float(np.median(err)), 1),
                               "mean_abs": round(float(np.abs(err).mean()), 1)}
    gaps = [r["iv_gap"] for r in rows if r["iv_gap"] is not None]
    s["iv_gap_points"] = {"mean": round(float(np.mean(gaps)), 2), "median": round(float(np.median(gaps)), 2),
                          "p10": round(float(np.percentile(gaps, 10)), 2), "p90": round(float(np.percentile(gaps, 90)), 2)}
    by: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r["iv_gap"] is not None:
            by[r["dte"]].append(r["iv_gap"])
    s["iv_gap_by_dte"] = {k: {"n": len(v), "median": round(float(np.median(v)), 2)} for k, v in sorted(by.items())}
    by_y: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["iv_gap"] is not None:
            by_y[r["date"][:4]].append(r["iv_gap"])
    s["iv_gap_by_year"] = {k: {"n": len(v), "median": round(float(np.median(v)), 2)} for k, v in sorted(by_y.items())}
    s["real_straddle_mean"] = round(float(np.mean([r["real"] for r in rows])), 1)
    return s


def main() -> int:
    import json
    res = run()
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
