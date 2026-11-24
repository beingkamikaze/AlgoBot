"""Indicator specs -> chart series, for the global setup. The maths is the shared `indicators/core.py`; what differs from NSE
is the day: a session here is a trading day that rolls at 17:00 New York, and the first range of the day is Asia's, not 9:15's."""

from __future__ import annotations

import datetime as dt

import numpy as np

from app.global_markets.clock import session_name, trading_day
from app.indicators import core
from app.market.models import Bar

DEFAULTS = {"sma": [20], "ema": [20], "rsi": [14], "atr": [14], "bb": [20, 2], "macd": [12, 26, 9],
            "supertrend": [10, 3], "vwap": [], "asia": [], "pdl": []}
LIMITS = {"sma": (2, 500), "ema": (2, 500), "rsi": (2, 100), "atr": (2, 100), "bb": (2, 200), "macd": (2, 200),
          "supertrend": (2, 100)}
INTRADAY_ONLY = {"vwap", "asia", "pdl"}


class SpecError(ValueError):
    pass


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def parse_specs(text: str) -> list[tuple[str, list[float]]]:
    specs = []
    for raw in filter(None, (p.strip().lower() for p in (text or "").split(","))):
        name, *rest = raw.split(":")
        if name not in DEFAULTS:
            raise SpecError(f"unknown indicator {name!r}; known: {', '.join(DEFAULTS)}")
        try:
            params = [float(x) for x in rest] or list(DEFAULTS[name])
        except ValueError:
            raise SpecError(f"bad parameters in {raw!r}")
        if len(params) != len(DEFAULTS[name]):
            raise SpecError(f"{name} takes {len(DEFAULTS[name])} parameter(s)")
        low, high = LIMITS.get(name, (0, 1e9))
        if params and not low <= params[0] <= high:
            raise SpecError(f"{name} period must be between {low} and {high}")
        specs.append((name, params))
    return specs


def _clean(a: np.ndarray) -> list[float | None]:
    return [None if np.isnan(x) else round(float(x), 2) for x in a]


def _series(id_: str, label: str, kind: str, placement: str, values: np.ndarray, colors=None) -> dict:
    return {"id": id_, "label": label, "kind": kind, "placement": placement, "values": _clean(values), "colors": colors}


def asia_range(h: np.ndarray, lo: np.ndarray, days: np.ndarray, sessions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each day's Asian-session high and low, drawn only once that session is over, so it never shows the future."""
    up, down = np.full(len(h), np.nan), np.full(len(h), np.nan)
    for day in np.unique(days):
        in_day = days == day
        asia = in_day & (sessions == "asia")
        after = in_day & (sessions != "asia") & (sessions != "")
        if asia.any() and after.any():
            up[after], down[after] = h[asia].max(), lo[asia].min()
    return up, down


def compute(bars: list[Bar], tf: str, specs: list[tuple[str, list[float]]], notes: list[str]) -> list[dict]:
    h = np.array([b.h for b in bars], dtype=float)
    lo = np.array([b.l for b in bars], dtype=float)
    c = np.array([b.c for b in bars], dtype=float)
    v = np.array([b.v for b in bars], dtype=float)
    intraday = tf != "1d"
    moments = [dt.datetime.fromtimestamp(b.t, dt.timezone.utc) for b in bars] if intraday else []
    days = np.array([trading_day(m).isoformat() for m in moments]) if intraday else np.array([])
    out: list[dict] = []
    for name, p in specs:
        if name in INTRADAY_ONLY and not intraday:
            notes.append(f"{name} is intraday only; not drawn on daily bars.")
            continue
        n = int(p[0]) if p else 0
        id_ = ":".join([name] + [_fmt(x) for x in p])
        if len(c) < 2:
            continue
        if name == "sma":
            out.append(_series(id_, f"SMA {n}", "line", "overlay", core.sma(c, n)))
        elif name == "ema":
            out.append(_series(id_, f"EMA {n}", "line", "overlay", core.ema(c, n)))
        elif name == "vwap":
            if v.sum() == 0:
                notes.append("VWAP needs volume, and this source reports none.")
                continue
            today = days == days[-1]
            if v[today].sum() == 0:
                notes.append("VWAP is not drawn for today: the live spot feed reports no volume.")
            out.append(_series(id_, "VWAP", "line", "overlay", core.vwap(h, lo, c, v, days)))
        elif name == "bb":
            up, mid, down = core.bollinger(c, n, p[1])
            out += [_series(f"{id_}.upper", f"BB {n} upper", "band-upper", "overlay", up),
                    _series(f"{id_}.mid", f"BB {n} basis", "line", "overlay", mid),
                    _series(f"{id_}.lower", f"BB {n} lower", "band-lower", "overlay", down)]
        elif name == "supertrend":
            line, d = core.supertrend(h, lo, c, n, p[1])
            colors = [None if np.isnan(x) else ("up" if x > 0 else "down") for x in d]
            out.append(_series(id_, f"Supertrend {n}, {_fmt(p[1])}", "step", "overlay", line, colors))
        elif name == "asia":
            sessions = np.array([session_name(m) or "" for m in moments])
            up, down = asia_range(h, lo, days, sessions)
            out += [_series("asia.high", "Asia high", "step", "overlay", up), _series("asia.low", "Asia low", "step", "overlay", down)]
        elif name == "pdl":
            ph, pl, pc = core.previous_day_levels(h, lo, c, days)
            out += [_series("pdl.high", "Prev day high", "step", "overlay", ph), _series("pdl.low", "Prev day low", "step", "overlay", pl),
                    _series("pdl.close", "Prev day close", "step", "overlay", pc)]
        elif name == "rsi":
            out.append(_series(id_, f"RSI {n}", "line", "pane:rsi", core.rsi(c, n)))
        elif name == "atr":
            out.append(_series(id_, f"ATR {n}", "line", "pane:atr", core.atr(h, lo, c, n)))
        elif name == "macd":
            line, sig, hist = core.macd(c, n, int(p[1]), int(p[2]))
            out += [_series(f"{id_}.hist", "MACD histogram", "histogram", "pane:macd", hist),
                    _series(f"{id_}.line", "MACD", "line", "pane:macd", line),
                    _series(f"{id_}.signal", "MACD signal", "line", "pane:macd", sig)]
    return out
