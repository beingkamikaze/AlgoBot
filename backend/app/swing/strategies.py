"""Swing strategies with long published track records. Defaults are the published rules, fixed on
2026-11-11 BEFORE any result was seen; the grids below were also fixed then (at most 8 settings each).

All long only, daily bars, signal on the close, entry at the next open (app.swing.engine).
"""

from __future__ import annotations

import itertools

import numpy as np

from app.indicators.core import atr, rsi, sma
from app.swing.data import Daily
from app.swing.engine import Entry, SwingStrategy


def _rolling_max_prior(x: np.ndarray, n: int) -> np.ndarray:
    """Highest value of the n days BEFORE i (not including i)."""
    out = np.full(x.shape, np.nan)
    for i in range(n, len(x)):
        out[i] = x[i - n:i].max()
    return out


def _rolling_min_prior(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    for i in range(n, len(x)):
        out[i] = x[i - n:i].min()
    return out


class RSI2Pullback(SwingStrategy):
    """Connors & Alvarez, "Short Term Trading Strategies That Work" (2008), the 2-period RSI rule (first
    published by Connors in 2004): in an uptrend (close above its 200-day average), buy when RSI(2) closes
    below 10; sell when the close goes back above the 5-day average. The book uses no stop. The time cap
    (10 sessions) is ours, so a stuck position does not tie money up. With no stop, R is measured against a
    notional 3 x ATR(14) below the entry; `stop_atr` > 0 places a real stop there instead."""
    id = "swing_rsi2"
    name = "RSI(2) pullback in an uptrend"
    defaults = {"rsi_below": 10.0, "trend_sma": 200, "exit_sma": 5, "max_hold": 10, "stop_atr": 0.0, "risk_atr": 3.0}

    def prepare(self, f: Daily) -> dict:
        p = self.params
        return {"rsi": rsi(f.c, 2), "trend": sma(f.c, p["trend_sma"]), "exit": sma(f.c, p["exit_sma"]),
                "atr": atr(f.h, f.l, f.c, 14)}

    def entry(self, f, ind, i):
        p = self.params
        r, tr, a = ind["rsi"][i], ind["trend"][i], ind["atr"][i]
        if np.isnan(r) or np.isnan(tr) or np.isnan(a) or not (f.c[i] > tr and r < p["rsi_below"]):
            return None
        k = p["stop_atr"] or p["risk_atr"]
        stop = f.c[i] - p["stop_atr"] * a if p["stop_atr"] > 0 else None
        return Entry(stop=stop, risk=k * a, max_hold=p["max_hold"],
                     rule=f"close {f.c[i]:.2f} above its {p['trend_sma']}-day average {tr:.2f} and RSI(2) {r:.1f} below {p['rsi_below']:g}",
                     features={"rsi2": round(float(r), 2)})

    def exit_on_close(self, f, ind, i, pos):
        return bool(f.c[i] > ind["exit"][i])


class HighBreakout(SwingStrategy):
    """52-week-high breakout. George & Hwang (Journal of Finance, 2004): stocks near their 52-week high keep
    outperforming; as a trading rule, the Turtle-style trend breakout (Faith, "Way of the Turtle", 2007):
    buy a new 252-day closing high, initial stop 2 x ATR(20) ("2N") below the signal close, sell when the
    close drops below the lowest close of the previous 20 days. Holding is capped at 250 sessions."""
    id = "swing_52wk"
    name = "52-week-high breakout"
    defaults = {"lookback": 252, "exit_low": 20, "stop_atr": 2.0, "max_hold": 250}

    def prepare(self, f: Daily) -> dict:
        p = self.params
        return {"hi": _rolling_max_prior(f.c, p["lookback"]), "lo": _rolling_min_prior(f.c, p["exit_low"]),
                "atr": atr(f.h, f.l, f.c, 20)}

    def entry(self, f, ind, i):
        p = self.params
        hi, a = ind["hi"][i], ind["atr"][i]
        if np.isnan(hi) or np.isnan(a) or f.c[i] <= hi:
            return None
        stop = f.c[i] - p["stop_atr"] * a
        return Entry(stop=stop, risk=p["stop_atr"] * a, max_hold=p["max_hold"],
                     rule=f"close {f.c[i]:.2f} is a new {p['lookback']}-day closing high (previous {hi:.2f})",
                     features={"atr_pct": round(float(a / f.c[i] * 100), 2)})

    def exit_on_close(self, f, ind, i, pos):
        lo = ind["lo"][i]
        return bool(not np.isnan(lo) and f.c[i] < lo)


class CrossSectionalMomentum(SwingStrategy):
    """Jegadeesh & Titman (Journal of Finance, 1993) relative-strength momentum, shown for India by Sehgal &
    Balakrishnan (2002) and used by NSE's Nifty 200 Momentum 30 index: on the last session of each month,
    rank the universe by the return from 12 months to 1 month ago (skipping the latest month), buy the top
    decile (10 of the Nifty 100), and at each month end sell what has left it. No stop; R is measured
    against a notional 3 x ATR(14). The ranks come from `rank_universe` (they need every stock)."""
    id = "swing_momentum"
    name = "12-1 month relative momentum"
    defaults = {"lookback": 252, "skip": 21, "top": 10, "market_filter": False, "risk_atr": 3.0}

    def prepare(self, f: Daily) -> dict:
        return {"atr": atr(f.h, f.l, f.c, 14)}

    def _key(self) -> str:
        p = self.params
        return f"top_{p['lookback']}_{p['skip']}_{p['top']}"

    def entry(self, f, ind, i):
        p = self.params
        if not f.extra.get("month_end", np.zeros(len(f), bool))[i] or not f.extra[self._key()][i]:
            return None
        if p["market_filter"] and not f.extra["market_up"][i]:
            return None
        a = ind["atr"][i]
        if np.isnan(a):
            return None
        return Entry(stop=None, risk=p["risk_atr"] * a, max_hold=100000,
                     rule=f"month end: in the top {p['top']} by the {p['lookback']}-{p['skip']} day return",
                     features={"rank_return_pct": round(float(f.extra[self._key() + '_ret'][i]), 2)})

    def exit_on_close(self, f, ind, i, pos):
        if not f.extra["month_end"][i]:
            return False
        if self.params["market_filter"] and not f.extra["market_up"][i]:
            return True
        return not bool(f.extra[self._key()][i])


def month_ends(dates: list[str]) -> np.ndarray:
    """Last session of each calendar month (the final day of the data is never one: the month may go on)."""
    return np.array([i + 1 < len(dates) and dates[i][:7] != dates[i + 1][:7] for i in range(len(dates))], dtype=bool)


def rank_universe(frames: dict[str, Daily], market: Daily, settings: list[tuple[int, int, int]]) -> None:
    """Adds month_end, market_up (NIFTY close above its 200-day average) and, per (lookback, skip, top),
    a bool 'in the top' series plus the ranked return, to every stock frame's `extra`. Each month end uses
    closes up to that day only. A stock with a blocked day inside its lookback is not ranked that month, and when
    frames carry `in_universe` (app.swing.universe), neither is a stock outside the universe that day."""
    me_dates = {market.dates[i] for i in np.flatnonzero(month_ends(market.dates))}
    m_sma = sma(market.c, 200)
    up = {d: bool(not np.isnan(s) and c > s) for d, c, s in zip(market.dates, market.c, m_sma)}
    idx = {s: {d: i for i, d in enumerate(f.dates)} for s, f in frames.items()}
    cum_block = {s: np.cumsum(f.blocked) for s, f in frames.items()}
    for s, f in frames.items():
        f.extra["month_end"] = np.array([d in me_dates for d in f.dates], dtype=bool)
        f.extra["market_up"] = np.array([up.get(d, False) for d in f.dates], dtype=bool)
    for lb, skip, top in settings:
        key = f"top_{lb}_{skip}_{top}"
        for f in frames.values():
            f.extra[key] = np.zeros(len(f), bool)
            f.extra[key + "_ret"] = np.full(len(f), np.nan)
        for d in sorted(me_dates):
            scores = []
            for s, f in frames.items():
                i = idx[s].get(d)
                if i is None or i < lb:
                    continue
                member = f.extra.get("in_universe")
                if member is not None and not member[i]:
                    continue
                cb = cum_block[s]
                if cb[i] - cb[i - lb] > 0:
                    continue
                scores.append((f.c[i - skip] / f.c[i - lb] - 1, s, i))
            scores.sort(reverse=True)
            for ret, s, i in scores[:top]:
                frames[s].extra[key][i] = True
            for ret, s, i in scores:
                frames[s].extra[key + "_ret"][i] = ret * 100


# ---------------------------------------------------------------- the grids, fixed before any result

STRATEGIES = {c.id: c for c in (RSI2Pullback, HighBreakout, CrossSectionalMomentum)}

GRIDS: dict[str, list[dict]] = {
    "swing_rsi2": [{"rsi_below": a, "max_hold": b, "stop_atr": c}
                   for a, b, c in itertools.product((10.0, 5.0), (10, 5), (0.0, 3.0))],
    "swing_52wk": [{"lookback": a, "exit_low": b, "stop_atr": c}
                   for a, b, c in itertools.product((252, 126), (20, 50), (2.0, 3.0))],
    "swing_momentum": [{"lookback": a, "skip": 21, "top": b, "market_filter": c}
                       for a, b, c in itertools.product((252, 126), (10, 20), (False, True))],
}
