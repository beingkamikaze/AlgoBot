"""Swing research runner: the fixed grids on the first 70% of sessions only.

    .venv/bin/python -m app.swing.research                       # in-sample grids, point-in-time liquid 100
    .venv/bin/python -m app.swing.research --universe nifty100   # today's Nifty 100 (survivor-biased)
    .venv/bin/python -m app.swing.research --only swing_rsi2

The held-back 30% is never loaded here: every frame is cut at the cutoff before any strategy runs.
Running one setting on the held-back days is a separate, one-time decision (see `held_back_test`), made
by the user, never while choosing settings.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections import defaultdict

import numpy as np

from app.swing import data, universe as uni
from app.swing.engine import SwingConfig, SwingTrade, run, summary
from app.swing.strategies import GRIDS, STRATEGIES, rank_universe

INDEXES = ("NIFTY", "BANKNIFTY")
NOT_TRADED = ("INDIAVIX",)
IN_SAMPLE_SHARE = 0.7
CUTOFF = "2021-10-20"        # fixed 2026-11-11 = NIFTY daily sessions[70%] of 2010-01-04..2026-11-09; never moves
NIFTY100 = "nifty100"        # the 100 stocks of the original download: today's index members (survivors)
LIQUID100 = "liquid100"      # app.swing.universe: top 100 by traded value at each month end, point in time


def nifty100_symbols() -> list[str]:
    """The original download (Nifty 100 + indices): the symbols whose Yahoo actions were fetched first."""
    from app.market import nse
    try:
        return sorted(set(nse.constituents("NIFTY100")) | set(INDEXES))
    except Exception:  # noqa: BLE001 - offline: fall back to the symbols with Yahoo actions saved first
        return sorted(data.load_actions())


def cutoff(nifty: data.Daily) -> str:
    """First held-back session: the session at 70% of NIFTY's daily history up to 2026-11-09 (CUTOFF)."""
    dates = [d for d in nifty.dates if d <= "2026-11-09"]
    return dates[int(len(dates) * IN_SAMPLE_SHARE)]


def load_all(symbols: list[str]) -> tuple[dict[str, data.Daily], dict]:
    acts = data.load_actions()
    frames, reports = {}, {}
    for s in symbols:
        if s in NOT_TRADED:
            continue
        f, r = data.load(s, acts)
        if len(f):
            frames[s], reports[s] = f, r
    return frames, reports


def years_between(a: str, b: str) -> float:
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days / 365.25


def cagr(f: data.Daily, start: str) -> float:
    i = next(k for k, d in enumerate(f.dates) if d >= start)
    return ((f.c[-1] / f.c[i]) ** (1 / years_between(f.dates[i], f.dates[-1])) - 1) * 100


def equal_weight_cagr(frames: dict[str, data.Daily], start: str, end: str) -> float:
    """Hold every stock of the universe in equal amounts, rebalanced daily (blocked days left out)."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for f in frames.values():
        for i in range(1, len(f)):
            if f.dates[i] >= start and not f.blocked[i]:
                by_day[f.dates[i]].append(f.c[i] / f.c[i - 1] - 1)
    growth = math.prod(1 + float(np.mean(v)) for v in by_day.values())
    return (growth ** (1 / years_between(start, end)) - 1) * 100


def drift(frames: dict[str, data.Daily]) -> dict[str, float]:
    """Each stock's average daily return (%) over the frames given (in-sample only): what simply holding it
    earned per session. Today's Nifty 100 are survivors, so this drift is large; a strategy's edge is what it
    earns ABOVE the drift for the days it holds."""
    out = {}
    for s, f in frames.items():
        r = f.c[1:] / f.c[:-1] - 1
        ok = ~f.blocked[1:]
        out[s] = float(r[ok].mean() * 100) if ok.any() else 0.0
    return out


def excess(trades: list[SwingTrade], d: dict[str, float]) -> dict:
    if not trades:
        return {}
    ex = np.array([t.pct - d.get(t.symbol, 0.0) * t.days for t in trades])
    days = sum(t.days for t in trades)
    per_day = ex.sum() / days / 100
    return {"avg_excess_pct": round(float(ex.mean()), 3),
            "excess_pct_per_year_invested": round(((1 + per_day) ** 248 - 1) * 100, 2)}


def vs_universe(trades: list[SwingTrade], ew: dict[str, float], dates: list[str]) -> dict:
    """The Lab's swing baseline: each trade minus holding the whole universe over the same days, same charges."""
    pairs = [(t.pct, uni.baseline_pct(t, ew, dates)) for t in trades]
    d = np.array([a - b for a, b in pairs if b is not None])
    return {"avg_vs_universe_pct": round(float(d.mean()), 3) if len(d) else None,
            "beats_universe_share": round(float((d > 0).mean()), 3) if len(d) else None}


def run_setting(sid: str, params: dict, frames: dict[str, data.Daily], cfg: SwingConfig) -> list[SwingTrade]:
    cls = STRATEGIES[sid]
    trades: list[SwingTrade] = []
    for s, f in frames.items():
        if s in INDEXES and sid != "swing_rsi2":
            continue
        trades += run(cls(**params), f, cfg)
    return trades


def frames_for(universe: str) -> tuple[dict[str, data.Daily], dict, dict[str, list[str]] | None]:
    """All frames of a universe, full history (the caller cuts them), with `in_universe` attached for liquid100."""
    if universe == LIQUID100:
        members = uni.load()
        if not members:
            raise SystemExit("no point-in-time universe yet: run `python -m app.swing.universe` first")
        frames, reports = load_all(sorted(set(uni.ever_members(members)) | set(INDEXES)))
        uni.attach({s: f for s, f in frames.items() if s not in INDEXES}, members)
        return frames, reports, members
    return load_all(nifty100_symbols()) + (None,)


def in_sample(only: list[str] | None = None, cfg: SwingConfig | None = None, universe: str = LIQUID100, log=print) -> dict:
    cfg = cfg or SwingConfig()
    full, reports, members = frames_for(universe)
    cut = cutoff(full["NIFTY"])
    assert cut == CUTOFF, f"the held-back cutoff moved: {cut} != {CUTOFF}"
    frames = {s: f.until(cut) for s, f in full.items()}
    del full                                      # nothing past the cutoff is kept in memory
    stocks = {s: f for s, f in frames.items() if s not in INDEXES}
    rank_universe(stocks, frames["NIFTY"], sorted({(g["lookback"], g["skip"], g["top"]) for g in GRIDS["swing_momentum"]}))
    first = frames["NIFTY"].dates[0]
    last = frames["NIFTY"].dates[-1]
    warm = frames["NIFTY"].dates[252]             # strategies need a year of history before their first signal
    yrs = years_between(warm, last)
    nd = frames["NIFTY"].dates
    if members is not None:
        ew = uni.equal_weight_index(stocks, members, nd)
        ew_cagr = ((ew[last] / ew[warm]) ** (1 / yrs) - 1) * 100
    else:
        ew = None
        ew_cagr = equal_weight_cagr(stocks, warm, last)
    bench = {"nifty_cagr_pct": round(cagr(frames["NIFTY"], warm), 2), "equal_weight_universe_cagr_pct": round(ew_cagr, 2)}
    out = {"universe": universe, "cutoff": cut, "data_from": first, "in_sample_to": last, "first_signal_possible": warm,
           "years": round(yrs, 2), "benchmarks": bench, "strategies": {}}
    dr = drift(frames)
    log(f"in-sample {warm}..{last} ({yrs:.1f} years); held-back from {cut} is not loaded")
    log(f"benchmarks: Nifty {bench['nifty_cagr_pct']}%/yr, equal-weight universe {bench['equal_weight_universe_cagr_pct']}%/yr")
    for sid in (only or list(GRIDS)):
        rows = []
        for g in GRIDS[sid]:
            params = {**STRATEGIES[sid].defaults, **g}
            tr = run_setting(sid, params, frames, cfg)
            s = summary(tr, yrs)
            s.update(excess(tr, dr))
            if ew is not None:
                s.update(vs_universe(tr, ew, nd))
            s["by_year_avg_pct"] = {y: round(float(np.mean([t.pct for t in tr if t.entry_date[:4] == y])), 3)
                                    for y in sorted({t.entry_date[:4] for t in tr})}
            s["indices_only"] = summary([t for t in tr if t.symbol in INDEXES]) if sid == "swing_rsi2" else None
            rows.append({"params": g, "default": all(STRATEGIES[sid].defaults.get(k) == v for k, v in g.items()), **s})
            log(f"{sid} {g}: n={s.get('trades')} avgR={s.get('avg_r')} avg%={s.get('avg_pct')} cost%={s.get('cost_pct')} "
                f"PF={s.get('profit_factor')} win={s.get('win_rate')} days={s.get('avg_days')} "
                f"%/yr invested={s.get('pct_per_year_invested')} excess%={s.get('avg_excess_pct')} "
                f"excess%/yr={s.get('excess_pct_per_year_invested')} vs-universe%={s.get('avg_vs_universe_pct')}")
        out["strategies"][sid] = rows
    out["data_checks"] = {
        "symbols": len(frames), "split_events_from_yahoo": sum(len(r["applied"]) + len(r["rejected"]) for r in reports.values()),
        "applied": sum(len(r["applied"]) for r in reports.values()),
        "blocked_days": sum(len(r["blocked"]) for r in reports.values()),
        "short_history": {s: (f.dates[0] if len(f) else "after the cutoff") for s, f in frames.items()
                          if not len(f) or f.dates[0] > "2010-12-31"},
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="comma-separated strategy ids")
    ap.add_argument("--json", default="", help="also write the full result here")
    ap.add_argument("--universe", default=LIQUID100, choices=(LIQUID100, NIFTY100))
    a = ap.parse_args(argv)
    res = in_sample([s for s in a.only.split(",") if s] or None, universe=a.universe)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res, fh, indent=1, default=float)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
