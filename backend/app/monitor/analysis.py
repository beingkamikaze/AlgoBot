"""Reads an open trade against the live chart: is the move going the way the strategy expected?

Pure functions over bars (no I/O), so the rules are testable. Every check is a fixed rule with the
numbers it used, never a model's opinion:

  progress   where price is, in R (risk = entry to stop): now, best (MFE) and worst (MAE) since entry
  thesis     the condition that made the strategy take the trade, re-checked on the latest completed bar:
               ORB      price stays outside the opening range
               PDH/PDL  price stays beyond yesterday's high / low
               VWAP     price stays on its side of VWAP
               Supertrend  the 5-minute Supertrend has not flipped
  trend      EMA 20 and Supertrend on 5-minute bars agree with the trade
  momentum   the last 15 minutes moved with the trade, not against it
  shock      no 1-minute bar against the trade bigger than 2.5x the average range (a sudden move)
  stop       price is not within 0.4R of the stop
  room       no support/resistance level sits between price and target close to price
  path       not behind the strategy's usual timing (its winners' median minutes to target, from the backtest)
  time       more than 20 minutes before the 15:15 exit

Health: "on track", "watch" (one warning), "at risk" (a serious warning or several), "thesis broken"
(the reason for the trade no longer holds while the trade is losing).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np

from app.core.calendar import IST
from app.indicators import core as ind

SHOCK_ATR = 2.5
NEAR_STOP_R = -0.6
FLAT_WARN_MIN = 20
SEVERITY = {"high": 3, "medium": 2, "low": 1}


@dataclass
class Setup:
    symbol: str                 # what the chart shows: RELIANCE, NIFTY, BANKNIFTY
    direction: int              # +1 long the underlying, -1 short
    entry: float                # underlying level at entry
    stop: float | None          # the ORIGINAL stop: R is always measured on the risk taken at entry
    target: float | None
    entry_t: int                # unix seconds
    strategy: str | None = None
    flat_by: dt.time = dt.time(15, 15)
    current_stop: float | None = None   # where the stop is now, when trade management has moved it

    @property
    def live_stop(self) -> float | None:
        return self.current_stop if self.current_stop is not None else self.stop


@dataclass
class Bars:
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    session: np.ndarray         # "2026-11-02" per bar (IST)
    minute: np.ndarray          # minutes since 9:15 IST

    def __len__(self) -> int:
        return len(self.t)


def _num(x) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def r_of(price: float, s: Setup) -> float | None:
    if s.stop is None or s.entry == s.stop:
        return None
    return s.direction * (price - s.entry) / abs(s.entry - s.stop)


def support_resistance(b5: Bars, price: float, today: str) -> list[dict]:
    """Yesterday's high / low / close, classic pivots, today's opening range and today's swing highs and lows,
    merged when within 0.1% of each other; the nearest three below and above price."""
    lv: list[tuple[float, str]] = []
    days = sorted(set(b5.session.tolist()))
    prev = [d for d in days if d < today]
    if prev:
        m = b5.session == prev[-1]
        ph, pl, pc = float(b5.h[m].max()), float(b5.l[m].min()), float(b5.c[m][-1])
        p = (ph + pl + pc) / 3
        lv += [(ph, "prev day high"), (pl, "prev day low"), (pc, "prev close"), (p, "pivot"),
               (2 * p - pl, "R1"), (2 * p - ph, "S1")]
    t = b5.session == today
    if t.any():
        first = np.where(t & (b5.minute < 15))[0]
        if len(first):
            lv += [(float(b5.h[first].max()), "opening range high"), (float(b5.l[first].min()), "opening range low")]
        idx = np.where(t)[0]
        for k in range(2, len(idx) - 2):          # 5-minute fractals: a bar higher / lower than two on each side
            i = idx[k]
            if b5.h[i] == b5.h[idx[k - 2]:idx[k + 2] + 1].max():
                lv.append((float(b5.h[i]), "swing high"))
            if b5.l[i] == b5.l[idx[k - 2]:idx[k + 2] + 1].min():
                lv.append((float(b5.l[i]), "swing low"))
    lv.sort()
    rank = {"prev day high": 0, "prev day low": 0, "prev close": 1, "opening range high": 1, "opening range low": 1,
            "pivot": 2, "R1": 2, "S1": 2, "swing high": 3, "swing low": 3}
    groups: list[list[tuple[float, str]]] = []
    for x, name in lv:                               # levels within 0.1% are one zone, named by its strongest member
        if groups and abs(x - groups[-1][0][0]) / x < 0.001:
            groups[-1].append((x, name))
        else:
            groups.append([(x, name)])
    merged = []
    for g in groups:
        names = sorted({n for _, n in g}, key=lambda n: rank.get(n, 9))
        merged.append((sum(x for x, _ in g) / len(g), names[0] + (f" (+{len(names) - 1})" if len(names) > 1 else "")))
    below = [m for m in merged if m[0] < price][-3:]
    above = [m for m in merged if m[0] >= price][:3]
    return [{"price": round(x, 2), "label": name, "kind": "support"} for x, name in below] + \
           [{"price": round(x, 2), "label": name, "kind": "resistance"} for x, name in above]


def _thesis(s: Setup, b5: Bars, i: int, today: str, vwap_ok: bool, st_dir: float) -> dict | None:
    """The strategy's own reason for the trade, on the last completed 5-minute bar."""
    c, d = float(b5.c[i]), s.direction
    if s.strategy == "orb":
        first = np.where((b5.session == today) & (b5.minute < 15))[0]
        if not len(first):
            return None
        hi, lo = float(b5.h[first].max()), float(b5.l[first].min())
        ok = c > hi if d > 0 else c < lo
        edge = hi if d > 0 else lo
        return {"key": "thesis", "name": "Breakout holds", "ok": ok, "severity": "high",
                "detail": f"5m close {c:,.2f} {'above' if c > edge else 'below'} the opening range {'high' if d > 0 else 'low'} {edge:,.2f}"
                          + ("" if ok else ": back inside the range, the breakout is failing")}
    if s.strategy == "pdh_pdl":
        prev = sorted(d_ for d_ in set(b5.session.tolist()) if d_ < today)
        if not prev:
            return None
        m = b5.session == prev[-1]
        edge = float(b5.h[m].max()) if d > 0 else float(b5.l[m].min())
        ok = c > edge if d > 0 else c < edge
        return {"key": "thesis", "name": "Beyond yesterday's " + ("high" if d > 0 else "low"), "ok": ok, "severity": "high",
                "detail": f"5m close {c:,.2f} vs {edge:,.2f}" + ("" if ok else ": back inside yesterday's range")}
    if s.strategy == "vwap_pullback" and vwap_ok:
        vw = ind.vwap(b5.h, b5.l, b5.c, b5.v, b5.session)[i]
        ok = c > vw if d > 0 else c < vw
        return {"key": "thesis", "name": "Holds VWAP", "ok": ok, "severity": "high",
                "detail": f"5m close {c:,.2f} {'above' if c > vw else 'below'} VWAP {vw:,.2f}"}
    if s.strategy == "supertrend" and not math.isnan(st_dir):
        ok = st_dir == d
        return {"key": "thesis", "name": "Supertrend has not flipped", "ok": ok, "severity": "high",
                "detail": "Supertrend still " + ("up" if st_dir > 0 else "down") + ("" if ok else ": it flipped against the trade")}
    return None


def analyze(s: Setup, b1: Bars, b5: Bars, now: dt.datetime, expected: dict | None = None) -> dict:
    if not len(b1) or not len(b5):
        return {"health": "no data", "headline": "No live bars for " + s.symbol + " yet", "checks": [], "sr": []}
    today = now.astimezone(IST).date().isoformat()
    price = float(b1.c[-1])
    since = b1.t >= s.entry_t
    hi = float(b1.h[since].max()) if since.any() else price
    lo = float(b1.l[since].min()) if since.any() else price
    r_now = r_of(price, s)
    best, worst = (hi, lo) if s.direction > 0 else (lo, hi)
    progress = {"price": round(price, 2), "r": _num(r_now), "mfe_r": _num(r_of(best, s)), "mae_r": _num(r_of(worst, s)),
                "to_target_pct": None, "to_stop_pct": None}
    if s.target is not None and s.target != s.entry:
        progress["to_target_pct"] = round(max(0.0, (price - s.entry) / (s.target - s.entry)) * 100, 1)
    if s.stop is not None and s.stop != s.entry:
        progress["to_stop_pct"] = round(max(0.0, (price - s.entry) / (s.stop - s.entry)) * 100, 1)
    if s.live_stop is not None and s.live_stop != s.stop:
        progress["stop_r"] = _num(r_of(s.live_stop, s))      # where the moved stop locks in, in R

    local_now = now.astimezone(IST)
    flat = dt.datetime.combine(local_now.date(), s.flat_by, IST)
    minutes_in = max(0, int((now.timestamp() - s.entry_t) // 60))
    minutes_left = int((flat - local_now).total_seconds() // 60)

    i = len(b5) - 2 if len(b5) >= 2 else len(b5) - 1           # the last completed 5-minute bar
    ema20 = ind.ema(b5.c, 20)[i]
    st_line, st_dir_arr = ind.supertrend(b5.h, b5.l, b5.c, 10, 3.0)
    st_dir = float(st_dir_arr[i])
    has_volume = bool((b5.v[b5.session == today] > 0).any())
    d, c5 = s.direction, float(b5.c[i])
    checks: list[dict] = []

    th = _thesis(s, b5, i, today, has_volume, st_dir)
    if th:
        checks.append(th)
    if not math.isnan(ema20):
        ok = c5 > ema20 if d > 0 else c5 < ema20
        checks.append({"key": "ema", "name": "Trend (EMA 20, 5m)", "ok": ok, "severity": "medium",
                       "detail": f"5m close {c5:,.2f} {'above' if c5 > ema20 else 'below'} EMA 20 {ema20:,.2f}"})
    if not math.isnan(st_dir) and s.strategy != "supertrend":
        checks.append({"key": "supertrend", "name": "Supertrend (5m)", "ok": st_dir == d, "severity": "medium",
                       "detail": "Supertrend " + ("up" if st_dir > 0 else "down") + f", line {st_line[i]:,.2f}"})
    if has_volume and s.strategy != "vwap_pullback":
        vw = ind.vwap(b5.h, b5.l, b5.c, b5.v, b5.session)[i]
        checks.append({"key": "vwap", "name": "VWAP side", "ok": (c5 > vw) if d > 0 else (c5 < vw), "severity": "medium",
                       "detail": f"5m close {c5:,.2f} vs VWAP {vw:,.2f}"})
    if len(b1) >= 16:
        move = float(b1.c[-1] - b1.c[-16]) * d
        checks.append({"key": "momentum", "name": "Momentum (last 15 min)", "ok": move >= 0, "severity": "low",
                       "detail": f"{'+' if move >= 0 else '−'}{abs(move):,.2f} {'with' if move >= 0 else 'against'} the trade"})
    a1 = ind.atr(b1.h, b1.l, b1.c, 14)
    if len(b1) >= 20 and not math.isnan(a1[-2]):
        shock = None
        for k in range(len(b1) - 3, len(b1)):
            rng, body = float(b1.h[k] - b1.l[k]), float(b1.c[k] - b1.o[k]) * d
            if body < 0 and rng > SHOCK_ATR * float(a1[k - 1]):
                shock = (k, rng, float(a1[k - 1]))
        if shock:
            k, rng, a = shock
            tm = dt.datetime.fromtimestamp(int(b1.t[k]), IST).strftime("%H:%M")
            checks.append({"key": "shock", "name": "No sudden move against", "ok": False, "severity": "high",
                           "detail": f"the {tm} bar moved {rng:,.2f} against the trade, {rng / a:.1f}x the usual 1-minute range"})
        else:
            checks.append({"key": "shock", "name": "No sudden move against", "ok": True, "severity": "high",
                           "detail": "no unusual bar against the trade in the last 3 minutes"})
    if r_now is not None:
        stop_r = r_of(s.live_stop, s) if s.live_stop is not None else None
        gap_r = r_now - (stop_r if stop_r is not None else -1.0)   # distance to the stop in force, in R of the original risk
        moved = s.live_stop != s.stop
        ok = gap_r > 1 + NEAR_STOP_R
        checks.append({"key": "stop", "name": "Away from the stop", "ok": ok, "severity": "high",
                       "detail": f"{r_now:+.2f}R" + (f", moved stop at {stop_r:+.2f}R" if moved else "")
                       + ("" if ok else f": within {gap_r:.2f}R of the stop")})
    sr = support_resistance(b5, price, today)
    if s.target is not None:
        between = [x for x in sr if (s.entry < x["price"] < s.target if d > 0 else s.target < x["price"] < s.entry)
                   and (x["price"] - price) * d > 0]
        near = [x for x in between if abs(x["price"] - price) < 0.5 * abs(s.target - price)]
        checks.append({"key": "room", "name": "Room to target", "ok": not near, "severity": "low",
                       "detail": (f"{near[0]['label']} at {near[0]['price']:,.2f} stands before the target {s.target:,.2f}"
                                  if near else "no level close in the way to the target")})
    if expected and expected.get("median_minutes_to_target") and r_now is not None:
        m = expected["median_minutes_to_target"]
        behind = minutes_in > m and r_now < 0.5
        checks.append({"key": "path", "name": "On the usual path", "ok": not behind, "severity": "medium",
                       "detail": (f"{minutes_in} min in at {r_now:+.2f}R; this strategy's winners usually reach target in about {m} min"
                                  if behind else f"{minutes_in} min in; winners usually take about {m} min")})
    checks.append({"key": "time", "name": "Time left", "ok": minutes_left > FLAT_WARN_MIN, "severity": "low",
                   "detail": f"{max(minutes_left, 0)} min to the 15:15 exit"})

    for c in checks:                       # comparisons against indicator values give numpy booleans: make them plain
        c["ok"] = bool(c["ok"])
    failing = [c for c in checks if not c["ok"]]
    worst = max((SEVERITY[c["severity"]] for c in failing), default=0)
    thesis_failed = any(c["key"] == "thesis" for c in failing)
    if (thesis_failed and (r_now or 0) < 0) or (r_now is not None and r_now <= -0.8):
        health = "thesis broken"
    elif worst == 3 or sum(1 for c in failing if c["severity"] == "medium") >= 2:
        health = "at risk"
    elif failing:
        health = "watch"
    else:
        health = "on track"
    top = sorted(failing, key=lambda c: -SEVERITY[c["severity"]])
    headline = {"on track": "Moving as the strategy expects",
                "watch": f"Watch: {top[0]['name'].lower()} — {top[0]['detail']}" if top else "",
                "at risk": f"At risk: {top[0]['detail']}" if top else "",
                "thesis broken": "The reason for this trade no longer holds" + (f": {top[0]['detail']}" if top else "")}[health]
    return {"health": health, "headline": headline, "progress": progress, "checks": checks, "sr": sr,
            "time": {"minutes_in": minutes_in, "minutes_to_flat": max(minutes_left, 0)},
            "indicators": {"ema20": _num(ema20), "supertrend": _num(st_line[i]), "supertrend_dir": None if math.isnan(st_dir) else int(st_dir)},
            "expected": expected}
