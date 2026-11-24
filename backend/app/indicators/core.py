"""Indicator maths on numpy arrays. One implementation, used by charts, backtests and
live signals alike. Values before an indicator has enough bars are NaN.

Conventions follow TradingView and TA-Lib: EMA seeded with the SMA of its first n
values, Wilder smoothing for RSI and ATR, population standard deviation for
Bollinger Bands, VWAP reset every session.
"""

from __future__ import annotations

import numpy as np

NaN = np.nan


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(x.shape, NaN)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n: int) -> np.ndarray:
    x = _arr(x)
    out = np.full(x.shape, NaN)
    if n <= 0 or len(x) < n:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def _wilder(x: np.ndarray, n: int, start: int) -> np.ndarray:
    """Wilder's smoothing: first value = mean of n values from `start`, then (prev*(n-1)+x)/n."""
    out = np.full(x.shape, NaN)
    if len(x) < start + n:
        return out
    out[start + n - 1] = x[start:start + n].mean()
    for i in range(start + n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi(close, n: int = 14) -> np.ndarray:
    c = _arr(close)
    out = np.full(c.shape, NaN)
    if len(c) <= n:
        return out
    d = np.diff(c, prepend=NaN)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = _wilder(gain, n, 1)
    al = _wilder(loss, n, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        out = 100 - 100 / (1 + rs)
    out = np.where((al == 0) & ~np.isnan(ag), 100.0, out)
    return out


def true_range(high, low, close) -> np.ndarray:
    h, lo, c = _arr(high), _arr(low), _arr(close)
    prev = np.roll(c, 1)
    tr = np.maximum(h - lo, np.maximum(np.abs(h - prev), np.abs(lo - prev)))
    if len(tr):
        tr[0] = h[0] - lo[0]
    return tr


def atr(high, low, close, n: int = 14) -> np.ndarray:
    return _wilder(true_range(high, low, close), n, 0)


def bollinger(close, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    c = _arr(close)
    mid = sma(c, n)
    sd = np.full(c.shape, NaN)
    if len(c) >= n:
        windows = np.lib.stride_tricks.sliding_window_view(c, n)
        sd[n - 1:] = windows.std(axis=1)          # population std, like TradingView
    return mid + k * sd, mid, mid - k * sd


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    c = _arr(close)
    line = ema(c, fast) - ema(c, slow)
    sig = np.full(c.shape, NaN)
    valid = ~np.isnan(line)
    if valid.sum() >= signal:
        first = int(np.argmax(valid))
        sig[first:] = ema(line[first:], signal)
    return line, sig, line - sig


def supertrend(high, low, close, n: int = 10, mult: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """Returns (line, direction): direction +1 when the trend is up (line below price), -1 when down."""
    h, lo, c = _arr(high), _arr(low), _arr(close)
    a = atr(h, lo, c, n)
    hl2 = (h + lo) / 2
    upper, lower = hl2 + mult * a, hl2 - mult * a
    line = np.full(c.shape, NaN)
    direction = np.zeros(c.shape)
    fu, fl = upper.copy(), lower.copy()
    for i in range(len(c)):
        if np.isnan(a[i]):
            continue
        if i > 0 and not np.isnan(a[i - 1]):
            fu[i] = upper[i] if upper[i] < fu[i - 1] or c[i - 1] > fu[i - 1] else fu[i - 1]
            fl[i] = lower[i] if lower[i] > fl[i - 1] or c[i - 1] < fl[i - 1] else fl[i - 1]
            prev_dir = direction[i - 1]
            if prev_dir == -1:
                direction[i] = 1 if c[i] > fu[i] else -1
            else:
                direction[i] = -1 if c[i] < fl[i] else 1
        else:
            direction[i] = 1 if c[i] >= hl2[i] else -1
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    direction[np.isnan(line)] = NaN
    return line, direction


def vwap(high, low, close, volume, session) -> np.ndarray:
    """Session VWAP of the typical price; `session` labels each bar's trading day."""
    tp = (_arr(high) + _arr(low) + _arr(close)) / 3
    v = _arr(volume)
    s = np.asarray(session)
    out = np.full(tp.shape, NaN)
    pv = cv = 0.0
    for i in range(len(tp)):
        if i == 0 or s[i] != s[i - 1]:
            pv = cv = 0.0
        pv += tp[i] * v[i]
        cv += v[i]
        out[i] = pv / cv if cv > 0 else tp[i]
    return out


def opening_range(high, low, session, minutes_since_open, minutes: int = 15) -> tuple[np.ndarray, np.ndarray]:
    """The high and low of each session's first `minutes`, drawn from the bar after the
    range closes until the end of that session (NaN while the range is still forming)."""
    h, lo = _arr(high), _arr(low)
    s, m = np.asarray(session), _arr(minutes_since_open)
    hi_out, lo_out = np.full(h.shape, NaN), np.full(h.shape, NaN)
    i = 0
    while i < len(h):
        j = i
        while j < len(h) and s[j] == s[i]:
            j += 1
        inside = [k for k in range(i, j) if m[k] < minutes]
        if inside and m[inside[0]] == 0:          # the session's first bar is there
            rh, rl = h[inside].max(), lo[inside].min()
            for k in range(i, j):
                if m[k] >= minutes:
                    hi_out[k], lo_out[k] = rh, rl
        i = j
    return hi_out, lo_out


def previous_day_levels(high, low, close, session) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Previous session's high, low and close on every bar of the next session."""
    h, lo, c = _arr(high), _arr(low), _arr(close)
    s = np.asarray(session)
    ph, pl, pc = (np.full(h.shape, NaN) for _ in range(3))
    prev = None
    i = 0
    while i < len(h):
        j = i
        while j < len(h) and s[j] == s[i]:
            j += 1
        if prev is not None:
            ph[i:j], pl[i:j], pc[i:j] = prev
        prev = (h[i:j].max(), lo[i:j].min(), c[j - 1])
        i = j
    return ph, pl, pc
