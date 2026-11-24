"""Zerodha charges, slippage and margin for Nifty option research (multi-leg, held to expiry or closed early).

Order charges come from the app's single source, app.core.costs.options_charges (brokerage ₹20 per order,
STT 0.15% of premium on sells, NSE transaction charge, SEBI fee, stamp duty on buys, GST). Today's rates are
used for every year: older rates were lower (STT on option sells was 0.0625% before Oct 2024), so older
years are charged more than they really paid. That is on purpose: the question is what this costs now.

At expiry nothing is traded: Nifty options are cash-settled. A long option that expires in the money pays
STT on exercise on its intrinsic value (EXERCISE_STT_PCT). The short side pays nothing at settlement.

Slippage: each leg fills worse than the bhavcopy close by max(SLIP_MIN points, SLIP_PCT of the price),
on entry and on any early exit. Margin follows app.options.structures: for a hedged short (an iron condor
or credit spread) the worst-case loss plus NSE's exposure margin on each short leg. It is an estimate,
not Zerodha's SPAN calculator.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.costs import options_charges
from app.core.instruments import INDEX_FUTURES
from app.options.structures import EXPOSURE_PCT

LOT = INDEX_FUTURES["NIFTY"]["lot_size"]   # today's Nifty lot (65), used for every year so results compare
EXERCISE_STT_PCT = 0.0015      # on intrinsic value of exercised long options; 0.125% before the 2026 Budget,
                               # assumed raised with the premium rate (check zerodha.com/charges)
SLIP_MIN = 0.20                # points per leg per fill
SLIP_PCT = 0.01                # of the leg's price


def slippage(price: float) -> float:
    return max(SLIP_MIN, SLIP_PCT * max(price, 0.0))


def fill(price: float, side: str) -> float:
    """The price actually paid ("buy") or received ("sell") for one leg, after slippage (never below 0.05)."""
    s = slippage(price)
    return price + s if side == "buy" else max(price - s, 0.05)


@dataclass(frozen=True)
class Leg:
    kind: str          # "CE" or "PE"
    strike: float
    side: str          # "sell" or "buy" when opening
    lots: int = 1

    @property
    def sign(self) -> int:
        return -1 if self.side == "sell" else 1

    def intrinsic(self, settle_index: float) -> float:
        return max(settle_index - self.strike, 0.0) if self.kind == "CE" else max(self.strike - settle_index, 0.0)


def order_charges(legs: list[Leg], prices: list[float], opening: bool, lot: int = LOT) -> float:
    """Charges for trading every leg once at `prices` (fills, per unit). Opening sells the 'sell' legs and buys the
    'buy' legs; closing does the opposite."""
    buy_v = sell_v = 0.0
    buys = sells = 0
    for lg, p in zip(legs, prices):
        v = p * lg.lots * lot
        selling = (lg.side == "sell") == opening
        if selling:
            sell_v += v
            sells += 1
        else:
            buy_v += v
            buys += 1
    return options_charges(buy_v, sell_v, buys, sells).total


def exercise_charges(legs: list[Leg], settle_index: float, lot: int = LOT) -> float:
    """STT on exercise for long legs that expire in the money (cash settlement: no orders, no brokerage)."""
    return round(sum(lg.intrinsic(settle_index) * lg.lots * lot * EXERCISE_STT_PCT
                     for lg in legs if lg.side == "buy"), 2)


def hedged_margin(max_loss_points: float, short_legs: int, forward: float, lot: int = LOT) -> float:
    """Margin for a defined-risk short structure: worst-case loss plus exposure margin on each short leg."""
    return round(max(max_loss_points, 0.0) * lot + short_legs * lot * forward * EXPOSURE_PCT / 100, 2)
