"""Option pricing for European index options (NSE index options are European, cash-settled).

Black-76 on the forward: F = S * exp((r - q) * T). Prices, Greeks and implied
volatility in pure Python (math.erf), so no scipy is needed.

Greeks are per one unit of the underlying:
  delta  change in option price for a 1-point move
  gamma  change in delta for a 1-point move
  theta  change in price per CALENDAR day (negative for a long option)
  vega   change in price for a 1 percentage-point change in volatility
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RATE = 0.065          # risk-free rate (Indian T-bill area); its effect on intraday option P&L is tiny
DIV_YIELD = 0.012     # index dividend yield


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float      # per calendar day
    vega: float       # per 1 vol point


def forward(spot: float, t: float, r: float = RATE, q: float = DIV_YIELD) -> float:
    return spot * math.exp((r - q) * t)


def black76(kind: str, spot: float, strike: float, t: float, vol: float, r: float = RATE, q: float = DIV_YIELD,
            fwd: float | None = None) -> Greeks:
    """kind 'CE' or 'PE'; t in years; vol as a decimal (0.13 = 13%). `fwd`, when given, is the market's forward
    (read from the chain by put-call parity) and replaces the rate-based estimate."""
    call = kind.upper() in ("CE", "C", "CALL")
    if t <= 0 or vol <= 0:
        intrinsic = max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
        d = (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0)
        return Greeks(intrinsic, d, 0.0, 0.0, 0.0)
    f = fwd if fwd else forward(spot, t, r, q)
    df = math.exp(-r * t)
    sq = vol * math.sqrt(t)
    d1 = (math.log(f / strike) + 0.5 * vol * vol * t) / sq
    d2 = d1 - sq
    if call:
        price = df * (f * _cdf(d1) - strike * _cdf(d2))
        delta = math.exp(-q * t) * _cdf(d1)
    else:
        price = df * (strike * _cdf(-d2) - f * _cdf(-d1))
        delta = -math.exp(-q * t) * _cdf(-d1)
    gamma = math.exp(-q * t) * _n(d1) / (spot * sq)
    vega = spot * math.exp(-q * t) * _n(d1) * math.sqrt(t) / 100
    # theta by bumping time one calendar day (robust and matches the definition above)
    t1 = max(t - 1 / 365, 0.0)
    later = black76(kind, spot, strike, t1, vol, r, q, fwd).price if t1 > 0 else (max(spot - strike, 0.0) if call else max(strike - spot, 0.0))
    return Greeks(price, delta, gamma, later - price, vega)


def implied_vol(kind: str, price: float, spot: float, strike: float, t: float,
                r: float = RATE, q: float = DIV_YIELD, fwd: float | None = None) -> float | None:
    """Volatility that reproduces `price` (bisection: slow but never diverges). None if no solution."""
    if t <= 0 or price <= 0:
        return None
    intrinsic = black76(kind, spot, strike, 0.0, 0.0).price
    if price < intrinsic - 0.01:
        return None
    lo, hi = 0.001, 5.0
    if black76(kind, spot, strike, t, hi, r, q, fwd).price < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if black76(kind, spot, strike, t, mid, r, q, fwd).price > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2


def parity_forward(call_mid: float, put_mid: float, strike: float, t: float, r: float = RATE) -> float:
    """The forward implied by a call and a put at the same strike: C - P = DF * (F - K)."""
    return strike + (call_mid - put_mid) * math.exp(r * t)


def years_to_expiry(now_ts: float, expiry_close_ts: float) -> float:
    """Time to the 15:30 IST close on expiry day, in years (calendar time)."""
    return max(expiry_close_ts - now_ts, 0.0) / (365.0 * 86400)
