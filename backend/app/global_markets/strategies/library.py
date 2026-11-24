"""The XAUUSD strategies. Their rules and settings were written on 7 Nov 2026 before any of them ran on any price data. All three
share one exit so that no extra choice is hiding in it: a stop 2 x ATR(14) from the signal close (the failed break uses its own
extreme instead) and a target at 2R, inside the setup's entry window and flat-by time."""

from __future__ import annotations

import numpy as np

from app.domain.signal import Condition
from app.global_markets.strategies.base import Candidate, Frame, Strategy

COMEX_OPEN = 8 * 60 + 20        # New York, minutes into the day
NY_RANGE_MINUTES = 30


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _atr_stop(f: Frame, i: int, side: str, n: int, mult: float) -> tuple[float, float] | None:
    a = float(f.ind("atr", n)[i])
    if np.isnan(a) or a <= 0:
        return None
    return round(float(f.c[i]) - mult * a if side == "buy" else float(f.c[i]) + mult * a, 2), a


def _first_break(f: Frame, i: int, hi: float, lo: float, start: int) -> str | None:
    """"buy" or "sell" if bar i is the day's first close outside hi..lo, measured from bar `start`."""
    earlier = f.c[start:i]
    if np.any(earlier > hi) or np.any(earlier < lo):
        return None
    if f.c[i] > hi:
        return "buy"
    return "sell" if f.c[i] < lo else None


class AsiaBreakout(Strategy):
    id = "asia-breakout"
    version = "1"
    name = "Asian range breakout"
    description = ("Marks the high and low of the quiet Asian session. After London opens, trades the first 5-minute close outside "
                   "that range, in the direction of the break, once a day.")
    defaults = {"atr_n": 14, "stop_atr": 2.0, "target_r": 2.0, "latest_london_hour": 12}
    param_help = {"atr_n": "ATR length for the stop", "stop_atr": "stop distance in ATRs", "target_r": "target in R",
                  "latest_london_hour": "no signal from this hour on, London time: the idea is about the London open"}

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        highs, lows = f.ind("asia")
        if np.isnan(highs[i]) or f.london_hour[i] >= p["latest_london_hour"]:
            return None
        hi, lo = float(highs[i]), float(lows[i])
        same_day = np.flatnonzero((f.day[:i + 1] == f.day[i]) & ~np.isnan(highs[:i + 1]))
        side = _first_break(f, i, hi, lo, int(same_day[0]))
        if side is None:
            return None
        got = _atr_stop(f, i, side, p["atr_n"], p["stop_atr"])
        if got is None:
            return None
        stop, atr = got
        edge = hi if side == "buy" else lo
        word = "above the Asian high" if side == "buy" else "below the Asian low"
        return Candidate(
            side=side, signal_index=i, stop=stop, target_r=p["target_r"],
            rule=f"First 5-minute close {word} of {_money(edge)} after London opened.",
            conditions=[Condition(name=f"Close {word}", met=True, detail=f"{_money(float(f.c[i]))} vs {_money(edge)}"),
                        Condition(name="First break of the day", met=True, detail=f"Asian range {_money(lo)} to {_money(hi)}"),
                        Condition(name="Before the cut-off", met=True, detail=f"before {p['latest_london_hour']}:00 London")],
            invalidation=f"Price returns inside the Asian range ({_money(lo)} to {_money(hi)}) and reaches the stop.",
            stop_reason=f"{p['stop_atr']:g} x ATR({p['atr_n']}) of {_money(atr)} from the close",
            features={"range_width": round(hi - lo, 2), "atr": round(atr, 2)})


def ny_open_range(f: Frame) -> tuple[np.ndarray, np.ndarray]:
    """Each day's high and low of the first 30 minutes from the COMEX open, on the bars after that range has closed. A range
    with missing bars is not used: it would not be the same thing."""
    up, down = np.full(len(f), np.nan), np.full(len(f), np.nan)
    need = NY_RANGE_MINUTES * 60 // f.bar_seconds
    in_range = (f.ny_minute >= COMEX_OPEN) & (f.ny_minute < COMEX_OPEN + NY_RANGE_MINUTES)
    for day in np.unique(f.day[in_range]):
        bars = np.flatnonzero(in_range & (f.day == day))
        if len(bars) != need:
            continue
        after = (f.day == day) & (f.t > f.t[bars[-1]])
        up[after], down[after] = f.h[bars].max(), f.l[bars].min()
    return up, down


class NyOpenRange(Strategy):
    id = "ny-open-range"
    version = "1"
    name = "New York open range"
    description = ("Marks the high and low of the first 30 minutes from the COMEX open at 08:20 New York, the busiest hour of the day. "
                   "Trades the first 5-minute close outside that range, in the direction of the break, once a day.")
    defaults = {"atr_n": 14, "stop_atr": 2.0, "target_r": 2.0}
    param_help = {"atr_n": "ATR length for the stop", "stop_atr": "stop distance in ATRs", "target_r": "target in R"}

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        if "ny_or" not in f._cache:
            f._cache["ny_or"] = ny_open_range(f)
        highs, lows = f._cache["ny_or"]
        if np.isnan(highs[i]):
            return None
        hi, lo = float(highs[i]), float(lows[i])
        same_day = np.flatnonzero((f.day[:i + 1] == f.day[i]) & ~np.isnan(highs[:i + 1]))
        side = _first_break(f, i, hi, lo, int(same_day[0]))
        if side is None:
            return None
        got = _atr_stop(f, i, side, p["atr_n"], p["stop_atr"])
        if got is None:
            return None
        stop, atr = got
        edge = hi if side == "buy" else lo
        word = "above the opening-range high" if side == "buy" else "below the opening-range low"
        return Candidate(
            side=side, signal_index=i, stop=stop, target_r=p["target_r"],
            rule=f"First 5-minute close {word} of {_money(edge)} after the New York open range.",
            conditions=[Condition(name=f"Close {word}", met=True, detail=f"{_money(float(f.c[i]))} vs {_money(edge)}"),
                        Condition(name="First break of the day", met=True, detail=f"range {_money(lo)} to {_money(hi)}, 08:20 to 08:50 New York")],
            invalidation=f"Price returns inside the range ({_money(lo)} to {_money(hi)}) and reaches the stop.",
            stop_reason=f"{p['stop_atr']:g} x ATR({p['atr_n']}) of {_money(atr)} from the close",
            features={"range_width": round(hi - lo, 2), "atr": round(atr, 2)})


class FailedBreak(Strategy):
    id = "failed-break"
    version = "1"
    name = "Failed break of yesterday's high or low"
    description = ("Price closes beyond the previous trading day's high or low, then closes back inside within 30 minutes. Trades back "
                   "into the range, once a day on each side, with the stop just beyond the failed move.")
    defaults = {"max_bars_outside": 6, "stop_buffer_atr": 0.5, "atr_n": 14, "target_r": 2.0}
    param_help = {"max_bars_outside": "the most 5-minute closes beyond the level before coming back",
                  "stop_buffer_atr": "how far beyond the failed move's extreme the stop sits, in ATRs",
                  "atr_n": "ATR length for the stop", "target_r": "target in R"}

    @staticmethod
    def _crossed_back(closes: np.ndarray, level: float, above: bool) -> np.ndarray:
        outside = closes > level if above else closes < level
        return outside[:-1] & ~outside[1:]

    def check(self, f: Frame, i: int) -> Candidate | None:
        p = self.params
        if i < 1 or f.day[i] != f.day[i - 1]:
            return None
        prev_high, prev_low, _ = f.ind("pdl")
        if np.isnan(prev_high[i]):
            return None
        pdh, pdl = float(prev_high[i]), float(prev_low[i])
        if f.c[i - 1] > pdh and f.c[i] <= pdh:
            side, level, above = "sell", pdh, True
        elif f.c[i - 1] < pdl and f.c[i] >= pdl:
            side, level, above = "buy", pdl, False
        else:
            return None
        start = int(np.searchsorted(f.day, f.day[i], side="left"))
        if self._crossed_back(f.c[start:i], level, above).any():    # already happened today on this side
            return None
        j = i - 1
        while j > start and (f.c[j - 1] > level if above else f.c[j - 1] < level):
            j -= 1
        outside = i - j
        if outside > p["max_bars_outside"]:
            return None
        atr = float(f.ind("atr", p["atr_n"])[i])
        if np.isnan(atr) or atr <= 0:
            return None
        extreme = float(f.h[j:i + 1].max()) if above else float(f.l[j:i + 1].min())
        stop = round(extreme + p["stop_buffer_atr"] * atr if above else extreme - p["stop_buffer_atr"] * atr, 2)
        name = "yesterday's high" if above else "yesterday's low"
        return Candidate(
            side=side, signal_index=i, stop=stop, target_r=p["target_r"],
            rule=f"Closed beyond {name} of {_money(level)} for {outside} bar(s), then closed back inside at {_money(float(f.c[i]))}.",
            conditions=[Condition(name=f"Closed beyond {name}", met=True, detail=f"{outside} 5-minute close(s) beyond {_money(level)}"),
                        Condition(name="Closed back inside", met=True, detail=f"{_money(float(f.c[i]))} vs {_money(level)}"),
                        Condition(name="First time today on this side", met=True, detail="once a day on each side")],
            invalidation=f"Price goes back beyond the failed move's extreme of {_money(extreme)} and reaches the stop.",
            stop_reason=f"{p['stop_buffer_atr']:g} x ATR({p['atr_n']}) of {_money(atr)} beyond the extreme of {_money(extreme)}",
            features={"bars_outside": outside, "atr": round(atr, 2)})


STRATEGIES: dict[str, type[Strategy]] = {s.id: s for s in (AsiaBreakout, NyOpenRange, FailedBreak)}


def make(strategy_id: str, params: dict | None = None) -> Strategy:
    if strategy_id not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy_id!r}; known: {', '.join(STRATEGIES)}")
    return STRATEGIES[strategy_id](**(params or {}))


def describe() -> list[dict]:
    return [{"id": s.id, "version": s.version, "name": s.name, "description": s.description, "timeframe": s.timeframe,
             "defaults": s.defaults, "param_help": s.param_help} for s in STRATEGIES.values()]
