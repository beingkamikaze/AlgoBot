"""Strategy framework.

A strategy looks at a `Frame` (one symbol's bars plus cached indicators) and, at bar
i, may return a `Candidate`: a trade idea with its stop, target and the reasons it
fired. It must only read data at indices <= i; `tests/test_strategies.py` checks
that by cutting the data short and comparing.

The same strategy object is used by the backtester, the live signal scanner and,
in Phase 3, the paper trader: the code that passed the backtest is the code that trades.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from app.core.calendar import IST, OPEN
from app.core.manage import EXIT_DEFAULTS, EXIT_HELP
from app.domain.signal import Condition
from app.indicators import core
from app.market.models import Candles


class Frame:
    """Arrays for one symbol, plus a per-frame indicator cache."""

    def __init__(self, c: Candles):
        b = c.bars
        self.symbol, self.tf = c.symbol, c.tf
        self.t = np.array([x.t for x in b], dtype=np.int64)
        self.o = np.array([x.o for x in b]); self.h = np.array([x.h for x in b])
        self.l = np.array([x.l for x in b]); self.c = np.array([x.c for x in b])  # noqa: E741
        self.v = np.array([x.v for x in b], dtype=float)
        local = [dt.datetime.fromtimestamp(int(x), IST) for x in self.t]
        self.local = local
        self.session = np.array([d.date().isoformat() for d in local])
        self.minute = np.array([d.hour * 60 + d.minute - (OPEN.hour * 60 + OPEN.minute) for d in local])
        self.bar_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 375}[c.tf]
        # index of each session's first bar, and the previous session's close for every bar
        self.session_start = np.zeros(len(b), dtype=np.int64)
        self.prev_close = np.full(len(b), np.nan)
        start, last_close = 0, np.nan
        for i in range(len(b)):
            if i == 0 or self.session[i] != self.session[i - 1]:
                if i > 0:
                    last_close = self.c[i - 1]
                start = i
            self.session_start[i] = start
            self.prev_close[i] = last_close
        self._cache: dict[tuple, Any] = {}

    def __len__(self) -> int:
        return len(self.t)

    def ind(self, name: str, *params):
        key = (name, *params)
        if key not in self._cache:
            self._cache[key] = self._compute(name, params)
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
            return core.vwap(self.h, self.l, self.c, self.v, self.session)
        if name == "supertrend":
            return core.supertrend(self.h, self.l, self.c, int(p[0]), float(p[1]))
        if name == "or":
            return core.opening_range(self.h, self.l, self.session, self.minute, int(p[0]))
        if name == "pdl":                  # previous session's high, low, close on every bar
            return core.previous_day_levels(self.h, self.l, self.c, self.session)
        if name == "vol_avg":            # average volume of the previous n bars (excludes bar i)
            out = np.full(len(self.v), np.nan)
            n = int(p[0])
            s = core.sma(self.v, n)
            out[1:] = s[:-1]
            return out
        raise KeyError(name)


@dataclass
class Candidate:
    side: str                         # "buy" or "sell"
    signal_index: int                 # the bar whose close confirmed it
    reference: float                  # price at the signal bar's close
    stop: float
    target_r: float                   # target as a multiple of the risk from the actual entry
    rule: str                         # one sentence with the numbers
    conditions: list[Condition]
    invalidation: str
    stop_reason: str
    features: dict[str, float | str] = field(default_factory=dict)   # for "similar trades"


class Strategy:
    id: ClassVar[str]
    version: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    market: ClassVar[str] = "equity_intraday"
    timeframe: ClassVar[str] = "5m"
    defaults: ClassVar[dict[str, Any]] = {}
    param_help: ClassVar[dict[str, str]] = {}
    auto_scan: ClassVar[bool] = True       # False: backtests and manual scans only, not the automatic scan

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        # the trade-management rules (app/core/manage.py) are parameters of every strategy: off by default
        cls.defaults = {**cls.defaults, **EXIT_DEFAULTS}
        cls.param_help = {**cls.param_help, **EXIT_HELP}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"unknown parameter(s) for {self.id}: {', '.join(sorted(unknown))}")
        self.params = {**self.defaults, **params}

    @property
    def version_key(self) -> str:
        """The version plus any non-default parameters: a changed parameter is a new version."""
        changed = {k: v for k, v in self.params.items() if self.defaults.get(k) != v}
        return self.version + ("" if not changed else "+" + ",".join(f"{k}={v}" for k, v in sorted(changed.items())))

    def check(self, f: Frame, i: int) -> Candidate | None:
        raise NotImplementedError

    # helpers for subclasses
    @staticmethod
    def fmt(x: float) -> str:
        return f"{x:,.2f}"
