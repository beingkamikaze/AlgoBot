"""Bars as arrays, and turning 1-minute bars into longer ones. Intraday bars sit on a UTC grid. A daily bar is one
trading day (it rolls at 17:00 New York), stamped at 00:00 UTC of the date the day ends on."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from app.global_markets.clock import session_bounds, trading_day
from app.market.models import Bar

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


@dataclass
class Ohlcv:
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray

    def __len__(self) -> int:
        return len(self.t)


def _trading_day_stamps(t: np.ndarray) -> np.ndarray:
    """00:00 UTC of each bar's trading day. The day rolls on the hour, so one lookup per hour is enough."""
    hours, inverse = np.unique(t // 3600, return_inverse=True)
    stamps = np.empty(len(hours), dtype=np.int64)
    for i, hour in enumerate(hours):
        day = trading_day(dt.datetime.fromtimestamp(int(hour) * 3600, dt.timezone.utc))
        stamps[i] = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    return stamps[inverse]


def resample(m1: Ohlcv, tf: str) -> Ohlcv:
    """1-minute bars -> `tf` bars. `m1` must be sorted by time."""
    if tf not in TF_SECONDS:
        raise ValueError(f"timeframe must be one of {', '.join(TF_SECONDS)}")
    if tf == "1m" or len(m1) == 0:
        return m1
    seconds = TF_SECONDS[tf]
    labels = _trading_day_stamps(m1.t) if tf == "1d" else m1.t - m1.t % seconds
    starts = np.flatnonzero(np.r_[True, labels[1:] != labels[:-1]])
    ends = np.r_[starts[1:], len(labels)] - 1
    return Ohlcv(t=labels[starts], o=m1.o[starts], h=np.maximum.reduceat(m1.h, starts), l=np.minimum.reduceat(m1.l, starts),
                 c=m1.c[ends], v=np.add.reduceat(m1.v, starts))


def is_complete(t: int, tf: str, now: float) -> bool:
    if tf != "1d":
        return t + TF_SECONDS[tf] <= now
    bounds = session_bounds(dt.datetime.fromtimestamp(t, dt.timezone.utc).date())
    return bounds is None or bounds[1].timestamp() <= now


def to_bars(a: Ohlcv, tf: str, now: float) -> list[Bar]:
    return [Bar(t=int(a.t[i]), o=round(float(a.o[i]), 2), h=round(float(a.h[i]), 2), l=round(float(a.l[i]), 2),
                c=round(float(a.c[i]), 2), v=int(a.v[i]), complete=is_complete(int(a.t[i]), tf, now))
            for i in range(len(a))]
