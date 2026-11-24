"""Weekly Nifty iron condor on real end-of-day option prices (NSE bhavcopy). Research only: nothing trades.

The rule, fixed on 2026-11-13 before any result was computed:
  - for every Nifty expiry E, enter at the close N trading days before E (skipped if that session is not after
    the previous expiry, so positions never overlap);
  - the forward F is read from put-call parity at the strike where the call and put closes are closest
    (both must have traded that day); time is calendar time from close to the expiry close (15:30);
  - sell the out-of-the-money call and put whose Black-76 delta (from their own implied vol at the close) is
    nearest the target delta, among options that traded that day; buy wings W points further out, which must
    also have traded that day; otherwise the week is skipped (and counted);
  - fills: each leg at its close, worse by app.fno_research.charges.slippage;
  - each later close before expiry: if buying the condor back at the closes costs at least K x the credit more
    than the credit (loss >= K x credit), close it at that close (with slippage and charges);
  - otherwise hold to expiry: cash-settled at NSE's final settlement price of the index;
  - one lot of today's size (65), today's Zerodha charges; return on margin per app.fno_research.charges.

Defined risk only: every short leg has its wing. Research reads days before the NSE held-back cutoff only
(calendar.CUTOFF): a trade counts in-sample only if it ENTERS and SETTLES before the cutoff.

    .venv/bin/python -m app.fno_research.condor research      # the in-sample grid (12 settings)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.fno_research import bhavcopy, charges as C
from app.fno_research.calendar import CUTOFF, Calendar, HeldBackError, load as load_calendar
from app.options import pricing

DEFAULTS = {"days_before": 2, "short_delta": 0.15, "wing": 300, "stop_k": 2.0}
GRID = [{"days_before": n, "short_delta": dlt, "wing": w, "stop_k": 2.0}
        for n in (1, 2, 3) for dlt in (0.15, 0.20) for w in (200, 300)]
RESULTS = bhavcopy.ROOT.parent / "fno_research"


class Days:
    """Bhavcopy days on demand, with a hard stop at `last_date`: research cannot read held-back prices."""

    def __init__(self, root: Path | None = None, last_date: dt.date | None = None, reader=bhavcopy.read_day):
        self.root, self.last_date, self.reader = root, last_date, reader
        self._cache: dict[dt.date, dict] = {}
        self.deltas: dict = {}           # (entry, expiry, kind) -> [(strike, delta)], shared by settings with the same N

    def options(self, d: dt.date, expiry: dt.date) -> dict[tuple[str, float], bhavcopy.Row]:
        return self._day(d)["opt"].get(expiry, {})

    def _day(self, d: dt.date) -> dict:
        if self.last_date is not None and d > self.last_date:
            raise HeldBackError(f"{d} is on or after the held-back cutoff: research may not read it")
        if d not in self._cache:
            rows = self.reader(d, self.root) or []
            opt: dict[dt.date, dict] = defaultdict(dict)
            for r in rows:
                if r.instrument == "OPT":
                    opt[r.expiry][(r.kind, r.strike)] = r
            self._cache[d] = {"opt": dict(opt)}
            if len(self._cache) > 64:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[d]


@dataclass
class Trade:
    entry: dt.date
    expiry: dt.date
    exit: dt.date
    exit_reason: str                 # "expiry" or "stop"
    forward: float
    strikes: dict                    # short_call, long_call, short_put, long_put
    deltas: dict                     # of the two shorts at entry
    credit: float                    # points, after entry slippage
    exit_cost: float                 # points paid to close (stop) or the settlement value (expiry)
    settle_index: float | None
    charges: float                   # rupees per lot, all of entry, exit and exercise
    pnl: float                       # rupees per lot after charges
    margin: float                    # rupees per lot
    days_held: int                   # calendar days

    @property
    def rom_pct(self) -> float:
        return round(self.pnl / self.margin * 100, 4) if self.margin else 0.0


@dataclass
class Result:
    params: dict
    trades: list[Trade] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)


def years(d: dt.date, e: dt.date) -> float:
    return max((e - d).days, 0) / 365.0


def parity_forward(opts: dict, t: float) -> float | None:
    best = None
    for (kind, k), c in opts.items():
        if kind != "CE" or c.volume <= 0 or c.close <= 0:
            continue
        p = opts.get(("PE", k))
        if not p or p.volume <= 0 or p.close <= 0:
            continue
        gap = abs(c.close - p.close)
        if best is None or gap < best[0]:
            best = (gap, k, c.close, p.close)
    if best is None:
        return None
    _, k, c, p = best
    return pricing.parity_forward(c, p, k, t)


def _price(call: bool, f: float, k: float, t: float, vol: float, df: float) -> float:
    sq = vol * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    return df * (f * pricing._cdf(d1) - k * pricing._cdf(d2)) if call else df * (k * pricing._cdf(-d2) - f * pricing._cdf(-d1))


def delta_of(kind: str, price: float, strike: float, f: float, t: float) -> float | None:
    """Black-76 delta from the option's own implied vol (same formulas as app.options.pricing, without theta)."""
    if t <= 0 or price <= 0:
        return None
    call = kind == "CE"
    df = math.exp(-pricing.RATE * t)
    intrinsic = df * max(f - strike, 0.0) if call else df * max(strike - f, 0.0)
    if price <= intrinsic or _price(call, f, strike, t, 5.0, df) < price:
        return None
    lo, hi = 0.001, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _price(call, f, strike, t, mid, df) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    iv = (lo + hi) / 2
    d1 = (math.log(f / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    q = math.exp(-pricing.DIV_YIELD * t)
    return q * pricing._cdf(d1) if call else -q * pricing._cdf(-d1)


def otm_deltas(opts: dict, kind: str, f: float, t: float, cache: dict | None = None, key=None) -> list[tuple[float, float]]:
    """(strike, delta) of every traded out-of-the-money option of one kind within 15% of the forward."""
    if cache is not None and (key, kind) in cache:
        return cache[(key, kind)]
    out = []
    for (k_kind, k), r in opts.items():
        if k_kind != kind or r.volume <= 0 or r.close <= 0 or abs(k - f) > 0.15 * f:
            continue
        if (kind == "CE" and k <= f) or (kind == "PE" and k >= f):
            continue
        d = delta_of(kind, r.close, k, f, t)
        if d is not None:
            out.append((k, d))
    if cache is not None:
        cache[(key, kind)] = out
    return out


def pick_short(opts: dict, kind: str, f: float, t: float, target: float, cache: dict | None = None,
               key=None) -> tuple[float, float] | None:
    """(strike, delta) of the traded out-of-the-money option whose |delta| is nearest the target (the farther
    strike on a tie)."""
    best = None
    for k, d in otm_deltas(opts, kind, f, t, cache, key):
        gap = abs(abs(d) - target)
        if best is None or gap < best[0] - 1e-12 or (abs(gap - best[0]) <= 1e-12 and abs(k - f) > abs(best[1] - f)):
            best = (gap, k, d)
    return (best[1], best[2]) if best else None


def settlement_index(opts_on_expiry: dict) -> float | None:
    """NSE's final settlement price on expiry day. The bhavcopy settles every expiring option at the index's
    final settlement price in the old format; otherwise read it back from in-the-money settlements (K + C, K - P)."""
    rows = list(opts_on_expiry.values())
    if not rows:
        return None
    strikes = sorted({k for _, k in opts_on_expiry})
    common = defaultdict(int)
    for r in rows:
        common[round(r.settle, 2)] += 1
    val, n = max(common.items(), key=lambda x: x[1])
    if n >= len(rows) * 0.5 and strikes[0] <= val <= strikes[-1]:
        return val
    est = []
    for (kind, k), r in opts_on_expiry.items():
        if r.settle >= 1.0:
            est.append(k + r.settle if kind == "CE" else k - r.settle)
    if not est:
        u = [r.underlying for r in rows if r.underlying]
        return u[0] if u else None
    est.sort()
    return est[len(est) // 2]


def run(params: dict, cal: Calendar, days: Days, until: dt.date | None = None) -> Result:
    """Every expiry in the calendar (that settles on or before `until`, when given)."""
    p = {**DEFAULTS, **params}
    n, target, wing, k_stop = int(p["days_before"]), float(p["short_delta"]), float(p["wing"]), float(p["stop_k"])
    res = Result(params=p)
    skip: dict[str, int] = defaultdict(int)
    for e in cal.expiries:
        if until is not None and e > until:
            break
        entry = cal.sessions_before(e, n)
        prev = cal.previous_expiry(e)
        if entry is None or (prev is not None and entry <= prev):
            skip["entry day is not after the previous expiry"] += 1
            continue
        t = years(entry, e)
        opts = days.options(entry, e)
        f = parity_forward(opts, t)
        if f is None:
            skip["no forward (no strike where both call and put traded)"] += 1
            continue
        key = (entry, e)
        sc, sp = pick_short(opts, "CE", f, t, target, days.deltas, key), pick_short(opts, "PE", f, t, target, days.deltas, key)
        if not sc or not sp:
            skip["no short strike with a usable delta"] += 1
            continue
        lc, lp = sc[0] + wing, sp[0] - wing
        rows = [opts.get(("CE", sc[0])), opts.get(("CE", lc)), opts.get(("PE", sp[0])), opts.get(("PE", lp))]
        if any(r is None or r.volume <= 0 or r.close <= 0 for r in rows):
            skip["a wing did not trade that day"] += 1
            continue
        legs = [C.Leg("CE", sc[0], "sell"), C.Leg("CE", lc, "buy"), C.Leg("PE", sp[0], "sell"), C.Leg("PE", lp, "buy")]
        tr = _hold(legs, [r.close for r in rows], entry, e, f, k_stop, wing, cal, days)
        if tr is None:
            skip["no settlement price on expiry day"] += 1
            continue
        tr.deltas = {"call": round(sc[1], 3), "put": round(sp[1], 3)}
        res.trades.append(tr)
    res.skipped = dict(skip)
    return res


def _hold(legs: list[C.Leg], closes: list[float], entry: dt.date, e: dt.date, f: float, k_stop: float, wing: float,
          cal: Calendar, days: Days) -> Trade | None:
    fills = [C.fill(px, lg.side) for lg, px in zip(legs, closes)]
    credit = sum(-lg.sign * px for lg, px in zip(legs, fills))
    chg = C.order_charges(legs, fills, opening=True)
    margin = C.hedged_margin(wing - credit, 2, f)
    strikes = {"short_call": legs[0].strike, "long_call": legs[1].strike, "short_put": legs[2].strike,
               "long_put": legs[3].strike}
    for d in cal.sessions_between(entry, e):
        o = days.options(d, e)
        rows = [o.get((lg.kind, lg.strike)) for lg in legs]
        if any(r is None or r.close <= 0 for r in rows):
            continue
        mark = sum(-lg.sign * r.close for lg, r in zip(legs, rows))       # cost to buy the condor back
        if mark - credit >= k_stop * credit:
            out = [C.fill(r.close, "sell" if lg.side == "buy" else "buy") for lg, r in zip(legs, rows)]
            cost = sum(-lg.sign * px for lg, px in zip(legs, out))
            chg += C.order_charges(legs, out, opening=False)
            pnl = (credit - cost) * C.LOT - chg
            return Trade(entry, e, d, "stop", round(f, 2), strikes, {}, round(credit, 2), round(cost, 2), None,
                         round(chg, 2), round(pnl, 2), margin, (d - entry).days)
    s = settlement_index(days.options(e, e))
    if s is None:
        return None
    value = sum(-lg.sign * lg.intrinsic(s) for lg in legs)                  # what the shorts owe, net of the wings
    chg += C.exercise_charges(legs, s)
    pnl = (credit - value) * C.LOT - chg
    return Trade(entry, e, e, "expiry", round(f, 2), strikes, {}, round(credit, 2), round(value, 2), round(s, 2),
                 round(chg, 2), round(pnl, 2), margin, (e - entry).days)


# ---------------------------------------------------------------- summaries

def max_drawdown(pnls: list[float]) -> float:
    peak = total = dd = 0.0
    for x in pnls:
        total += x
        peak = max(peak, total)
        dd = min(dd, total - peak)
    return round(dd, 2)


def summary(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    pn = [t.pnl for t in trades]
    by_year: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        by_year[t.entry.year].append(t.pnl)
    return {
        "trades": len(trades),
        "win_rate": round(sum(x > 0 for x in pn) / len(pn), 3),
        "avg_pnl": round(sum(pn) / len(pn), 2),
        "total_pnl": round(sum(pn), 2),
        "avg_rom_pct": round(sum(t.rom_pct for t in trades) / len(trades), 3),
        "avg_margin": round(sum(t.margin for t in trades) / len(trades), 0),
        "avg_credit_pts": round(sum(t.credit for t in trades) / len(trades), 2),
        "avg_charges": round(sum(t.charges for t in trades) / len(trades), 2),
        "stops": sum(t.exit_reason == "stop" for t in trades),
        "worst_week": round(min(pn), 2),
        "best_week": round(max(pn), 2),
        "max_drawdown": max_drawdown(pn),
        "by_year": {y: {"trades": len(v), "total": round(sum(v), 2), "avg": round(sum(v) / len(v), 2)}
                    for y, v in sorted(by_year.items())},
    }


def in_sample_grid(root: Path | None = None, grid: list[dict] | None = None) -> list[dict]:
    """Every setting on trades that enter AND settle before the cutoff. Held-back days are never read."""
    last = CUTOFF - dt.timedelta(days=1)
    cal = load_calendar(root, last_date=last)
    days = Days(root, last_date=last)
    out = []
    for p in grid or GRID:
        r = run(p, cal, days, until=last)
        out.append({"params": r.params, "skipped": r.skipped, **summary(r.trades),
                    "trade_list": [{**asdict(t), "rom_pct": t.rom_pct} for t in r.trades]})
    return out


def _table(rows: list[dict]) -> str:
    head = f"{'N':>2} {'delta':>5} {'wing':>4} {'trades':>6} {'win%':>5} {'avg ₹':>8} {'total ₹':>10} {'RoM%':>6} " \
           f"{'credit':>6} {'chg ₹':>6} {'stops':>5} {'worst ₹':>9} {'maxDD ₹':>9}"
    lines = [head]
    for r in rows:
        p = r["params"]
        lines.append(f"{p['days_before']:>2} {p['short_delta']:>5} {p['wing']:>4} {r['trades']:>6} {r['win_rate'] * 100:>5.1f} "
                     f"{r['avg_pnl']:>8.0f} {r['total_pnl']:>10.0f} {r['avg_rom_pct']:>6.2f} {r['avg_credit_pts']:>6.1f} "
                     f"{r['avg_charges']:>6.0f} {r['stops']:>5} {r['worst_week']:>9.0f} {r['max_drawdown']:>9.0f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("research", help="the in-sample grid (days before the held-back cutoff only)")
    a = ap.parse_args(argv)
    if a.cmd == "research":
        rows = in_sample_grid()
        print(f"in-sample: trades entering and settling before {CUTOFF}; one lot of {C.LOT}, rupees per lot after charges")
        print(_table(rows))
        print("\nyear by year (total ₹ per lot):")
        years_ = sorted({y for r in rows for y in r["by_year"]})
        print(f"{'N':>2} {'delta':>5} {'wing':>4} " + " ".join(f"{y:>9}" for y in years_))
        for r in rows:
            p = r["params"]
            print(f"{p['days_before']:>2} {p['short_delta']:>5} {p['wing']:>4} "
                  + " ".join(f"{r['by_year'].get(y, {}).get('total', 0):>9.0f}" for y in years_))
        skipped = {json.dumps(r["params"]): r["skipped"] for r in rows if r["skipped"]}
        if skipped:
            print("\nskipped weeks:", json.dumps(skipped, indent=1))
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "condor_in_sample.json").write_text(json.dumps(rows, default=str, indent=1))
        print(f"\nsaved to {RESULTS / 'condor_in_sample.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
