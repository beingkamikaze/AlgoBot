"""Indicator specs ("ema:20", "supertrend:10:3", ...) -> named series for a chart.

Each indicator says where it is drawn: on the price chart ("overlay") or in its own
pane ("pane"), so the frontend needs no knowledge of the maths.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from app.core.calendar import IST, OPEN
from app.indicators import core
from app.market.models import Candles

INTRADAY_ONLY = {"vwap", "or"}


@dataclass
class Series:
    id: str               # "ema:20", "bb:20:2.upper"
    label: str            # "EMA 20"
    kind: str             # "line", "histogram", "band-upper", "band-lower", "step"
    placement: str        # "overlay" or "pane:rsi", "pane:macd"
    values: list[float | None]
    colors: list[str | None] | None = None   # per-bar colour, e.g. Supertrend up/down


@dataclass
class IndicatorSpec:
    name: str
    params: list[float] = field(default_factory=list)

    @property
    def id(self) -> str:
        return ":".join([self.name] + [_fmt(p) for p in self.params])


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


DEFAULTS = {"sma": [20], "ema": [20], "rsi": [14], "atr": [14], "bb": [20, 2], "macd": [12, 26, 9],
            "supertrend": [10, 3], "vwap": [], "or": [15], "pdl": []}
LIMITS = {"sma": (2, 500), "ema": (2, 500), "rsi": (2, 100), "atr": (2, 100), "bb": (2, 200),
          "macd": (2, 200), "supertrend": (2, 100), "or": (5, 120)}


class SpecError(ValueError):
    pass


def parse_specs(text: str) -> list[IndicatorSpec]:
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
            raise SpecError(f"{name} takes {len(DEFAULTS[name])} parameter(s), e.g. "
                            f"{IndicatorSpec(name, DEFAULTS[name]).id}")
        lo, hi = LIMITS.get(name, (0, 1e9))
        if params and not lo <= params[0] <= hi:
            raise SpecError(f"{name} period must be between {lo} and {hi}")
        specs.append(IndicatorSpec(name, params))
    return specs


def _clean(a: np.ndarray, digits: int = 2) -> list[float | None]:
    return [None if np.isnan(x) else round(float(x), digits) for x in a]


def compute(c: Candles, specs: list[IndicatorSpec], notes: list[str] | None = None) -> list[Series]:
    """`notes` collects why a requested indicator was left out."""
    notes = notes if notes is not None else []
    b = c.bars
    h = np.array([x.h for x in b]); lo = np.array([x.l for x in b])
    cl = np.array([x.c for x in b]); v = np.array([x.v for x in b], dtype=float)
    local = [dt.datetime.fromtimestamp(x.t, IST) for x in b]
    session = np.array([d.date().isoformat() for d in local])
    since_open = np.array([(d.hour * 60 + d.minute) - (OPEN.hour * 60 + OPEN.minute) for d in local])
    intraday = c.tf != "1d"
    out: list[Series] = []
    for s in specs:
        p = s.params
        if s.name in INTRADAY_ONLY and not intraday:
            notes.append(f"{s.id} is intraday only; not drawn on daily bars.")
            continue
        if s.name == "sma":
            out.append(Series(s.id, f"SMA {_fmt(p[0])}", "line", "overlay", _clean(core.sma(cl, int(p[0])))))
        elif s.name == "ema":
            out.append(Series(s.id, f"EMA {_fmt(p[0])}", "line", "overlay", _clean(core.ema(cl, int(p[0])))))
        elif s.name == "vwap":
            if v.sum() == 0:            # indices trade no volume: VWAP needs the futures (Phase 3)
                notes.append("VWAP needs volume; this instrument reports none (indices). "
                             "It returns with Nifty futures data.")
                continue
            out.append(Series(s.id, "VWAP", "line", "overlay", _clean(core.vwap(h, lo, cl, v, session))))
        elif s.name == "bb":
            up, mid, dn = core.bollinger(cl, int(p[0]), p[1])
            label = f"BB {_fmt(p[0])}, {_fmt(p[1])}"
            out += [Series(s.id + ".upper", label + " upper", "band-upper", "overlay", _clean(up)),
                    Series(s.id + ".mid", label + " basis", "line", "overlay", _clean(mid)),
                    Series(s.id + ".lower", label + " lower", "band-lower", "overlay", _clean(dn))]
        elif s.name == "supertrend":
            line, d = core.supertrend(h, lo, cl, int(p[0]), p[1])
            colors = [None if np.isnan(x) else ("up" if x > 0 else "down") for x in d]
            out.append(Series(s.id, f"Supertrend {_fmt(p[0])}, {_fmt(p[1])}", "step", "overlay", _clean(line), colors))
        elif s.name == "or":
            hi_, lo_ = core.opening_range(h, lo, session, since_open, int(p[0]))
            out += [Series(s.id + ".high", f"OR {_fmt(p[0])}m high", "step", "overlay", _clean(hi_)),
                    Series(s.id + ".low", f"OR {_fmt(p[0])}m low", "step", "overlay", _clean(lo_))]
        elif s.name == "pdl":
            ph, pl, pc = core.previous_day_levels(h, lo, cl, session)
            if intraday:
                out += [Series("pdl.high", "Prev day high", "step", "overlay", _clean(ph)),
                        Series("pdl.low", "Prev day low", "step", "overlay", _clean(pl)),
                        Series("pdl.close", "Prev day close", "step", "overlay", _clean(pc))]
        elif s.name == "rsi":
            out.append(Series(s.id, f"RSI {_fmt(p[0])}", "line", "pane:rsi", _clean(core.rsi(cl, int(p[0])))))
        elif s.name == "atr":
            out.append(Series(s.id, f"ATR {_fmt(p[0])}", "line", "pane:atr", _clean(core.atr(h, lo, cl, int(p[0])))))
        elif s.name == "macd":
            line, sig, hist = core.macd(cl, int(p[0]), int(p[1]), int(p[2]))
            label = f"MACD {_fmt(p[0])}, {_fmt(p[1])}, {_fmt(p[2])}"
            out += [Series(s.id + ".hist", label + " histogram", "histogram", "pane:macd", _clean(hist)),
                    Series(s.id + ".line", label, "line", "pane:macd", _clean(line)),
                    Series(s.id + ".signal", label + " signal", "line", "pane:macd", _clean(sig))]
    return out
