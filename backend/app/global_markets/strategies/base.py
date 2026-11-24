"""Strategy framework for the global setup. A strategy reads a `Frame` of closed bars and, at bar i, may return a `Candidate`.
It must only read indices <= i; `lookahead_violations` checks that by cutting the data short and comparing. Signals are worked
out on mid prices; fills are on the real bid and ask (`backtest.py`). A trading day here rolls at 17:00 New York."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from app.core.manage import EXIT_DEFAULTS, EXIT_HELP
from app.domain.signal import Condition
from app.global_markets.clock import LONDON, NEW_YORK, session_name, trading_day
from app.global_markets.data.bars import TF_SECONDS, Ohlcv, resample
from app.global_markets.indicators import asia_range
from app.indicators import core


def by_hour(t: np.ndarray, fn) -> np.ndarray:
    """`fn(moment)` for each time. Every session and day boundary falls on a whole UTC hour, so one call per hour is enough."""
    hours, inverse = np.unique(t // 3600, return_inverse=True)
    return np.array([fn(dt.datetime.fromtimestamp(int(h) * 3600, dt.timezone.utc)) for h in hours])[inverse]


def open_mask(t: np.ndarray) -> np.ndarray:
    """False for bars at times the market clock calls closed: stray prints, which no strategy or fill may use."""
    return by_hour(t, session_name) != None  # noqa: E711 - an element-wise comparison


def day_labels(t: np.ndarray) -> np.ndarray:
    return by_hour(t, lambda m: trading_day(m).isoformat()).astype(str)


class Frame:
    """Closed mid-price bars for one symbol, plus a per-frame indicator cache."""

    def __init__(self, symbol: str, tf: str, bars: Ohlcv):
        if tf == "1d":
            raise ValueError("the global backtester works on intraday bars")
        self.symbol, self.tf = symbol, tf
        self.t, self.o, self.h, self.l, self.c, self.v = bars.t, bars.o, bars.h, bars.l, bars.c, bars.v
        self.bar_seconds = TF_SECONDS[tf]
        self.day = day_labels(self.t) if len(self.t) else np.array([], dtype=str)
        self.session = by_hour(self.t, lambda m: session_name(m) or "").astype(str) if len(self.t) else np.array([], dtype=str)
        # time of day where the rules are written: clocks there change on whole hours, so a lookup per hour is exact
        self.ny_minute = by_hour(self.t, lambda m: m.astimezone(NEW_YORK).hour) * 60 + (self.t % 3600) // 60 if len(self.t) else np.array([], dtype=np.int64)
        self.london_hour = by_hour(self.t, lambda m: m.astimezone(LONDON).hour) if len(self.t) else np.array([], dtype=np.int64)
        self._cache: dict[tuple, Any] = {}

    @classmethod
    def from_m1(cls, symbol: str, tf: str, m1: Ohlcv) -> "Frame":
        return cls(symbol, tf, resample(m1, tf))

    def __len__(self) -> int:
        return len(self.t)

    def cut(self, n: int) -> "Frame":
        """The same frame with only its first n bars: what a strategy would have seen back then."""
        return Frame(self.symbol, self.tf, Ohlcv(self.t[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.v[:n]))

    def ind(self, name: str, *p):
        key = (name, *p)
        if key not in self._cache:
            self._cache[key] = self._compute(name, p)
        return self._cache[key]

    def _compute(self, name: str, p: tuple):
        if name == "ema":
            return core.ema(self.c, int(p[0]))
        if name == "sma":
            return core.sma(self.c, int(p[0]))
        if name == "rsi":
            return core.rsi(self.c, int(p[0]))
        if name == "atr":
            return core.atr(self.h, self.l, self.c, int(p[0]))
        if name == "vwap":
            return core.vwap(self.h, self.l, self.c, self.v, self.day)
        if name == "supertrend":
            return core.supertrend(self.h, self.l, self.c, int(p[0]), float(p[1]))
        if name == "pdl":
            return core.previous_day_levels(self.h, self.l, self.c, self.day)
        if name == "asia":
            return asia_range(self.h, self.l, self.day, self.session)
        raise KeyError(name)


@dataclass
class Candidate:
    side: str                         # "buy" or "sell"
    signal_index: int                 # the bar whose close confirmed it
    stop: float
    target_r: float                   # target as a multiple of the risk from the actual entry
    rule: str                         # one sentence with the numbers
    conditions: list[Condition]
    invalidation: str
    stop_reason: str
    features: dict[str, float | str] = field(default_factory=dict)


class Strategy:
    id: ClassVar[str]
    version: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    timeframe: ClassVar[str] = "5m"
    defaults: ClassVar[dict[str, Any]] = {}
    param_help: ClassVar[dict[str, str]] = {}

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        cls.defaults = {**cls.defaults, **EXIT_DEFAULTS}       # the shared stop rules are parameters of every strategy
        cls.param_help = {**cls.param_help, **EXIT_HELP}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"unknown parameter(s) for {self.id}: {', '.join(sorted(unknown))}")
        self.params = {**self.defaults, **params}

    def check(self, f: Frame, i: int) -> Candidate | None:
        raise NotImplementedError


def lookahead_violations(strategy: Strategy, f: Frame, every: int = 25) -> list[int]:
    """Bars where a strategy's answer changes once later bars exist. Anything but an empty list means it reads the future."""
    bad = []
    for n in range(every, len(f), every):
        short = f.cut(n)
        for i in range(max(0, n - every), n):
            a, b = strategy.check(short, i), strategy.check(f, i)
            same = (a is None) == (b is None) and (a is None or (a.side, a.stop, a.target_r) == (b.side, b.stop, b.target_r))
            if not same:
                bad.append(i)
    return bad
