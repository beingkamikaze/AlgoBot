"""What is being traded: a stock, an index futures contract, or a stock futures contract.

Everything that sizes, prices or charges a trade (backtester, risk engine, paper
engine, signal cards) asks this module, so a stock and a futures contract go
through the same code with different lot sizes, ticks, charges and margin.

Futures are written NIFTY-FUT / BANKNIFTY-FUT / TITAN-FUT (any NSE F&O stock) and resolve to the contract the app
would trade on a given day: the current month's, rolling to the next month
`ROLL_DAYS` trading days before expiry (e.g. NIFTY26OCTFUT -> NIFTY26NOVFUT).

Until Kite data is connected:
  - prices come from the INDEX (spot); futures trade at a premium or discount to it
    (the basis), so fills and P&L are approximate;
  - margin is an estimate (`FUT_MARGIN_PCT` of the contract value); Kite's margin
    API replaces it;
  - lot sizes and expiries are NSE's published values below; Kite's instrument
    list replaces them.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable

from app.core.calendar import is_trading_day, known_years
from app.core.costs import Charges, equity_intraday_charges, futures_charges, index_derivative_tick, tick_for

ROLL_DAYS = 2
FUT_MARGIN_PCT = 12.0                      # SPAN + exposure, roughly, for index futures
STOCK_FUT_MARGIN_PCT = 20.0                # stock futures need more margin than index futures (Kite's margin API replaces it)

# NSE lot sizes from the January 2026 expiries (NSE circular of 3 Oct 2025): Nifty 75 -> 65, Bank Nifty 35 -> 30
INDEX_FUTURES = {
    "NIFTY": {"name": "Nifty 50", "lot_size": 65},
    "BANKNIFTY": {"name": "Nifty Bank", "lot_size": 30},
}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


@dataclass(frozen=True)
class Instrument:
    kind: str                              # "equity" or "future"
    symbol: str                            # what the app calls it: RELIANCE, NIFTY-FUT
    data_symbol: str                       # where prices come from: RELIANCE, NIFTY (the index, until Kite)
    tradingsymbol: str                     # what the broker calls it: RELIANCE, NIFTY26OCTFUT
    lot_size: int
    margin_pct: float                      # cash blocked as % of position value (100 = no leverage)
    expiry: dt.date | None = None
    name: str | None = None
    price_is_proxy: bool = False           # True while futures are priced from the index
    expiry_note: str | None = None         # set when the holiday list for the expiry's year is not known yet

    @property
    def is_index(self) -> bool:
        return self.data_symbol in INDEX_FUTURES

    def tick(self, price: float) -> float:
        return index_derivative_tick(price) if self.kind == "future" and self.is_index else tick_for(price)

    def charges(self, buy_value: float, sell_value: float) -> Charges:
        fn: Callable = futures_charges if self.kind == "future" else equity_intraday_charges
        return fn(buy_value, sell_value)

    def margin(self, price: float, qty: int) -> float:
        return price * qty * self.margin_pct / 100


def monthly_expiry(year: int, month: int) -> dt.date:
    """Last Tuesday of the month; the trading day before if that Tuesday is a holiday."""
    d = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1))
    while d.weekday() != 1:
        d -= dt.timedelta(days=1)
    while not is_trading_day(d)[0]:
        d -= dt.timedelta(days=1)
    return d


def trading_days_between(a: dt.date, b: dt.date) -> int:
    """Trading days after `a` up to and including `b`."""
    n, d = 0, a
    while d < b:
        d += dt.timedelta(days=1)
        n += is_trading_day(d)[0]
    return n


def active_expiry(today: dt.date, roll_days: int = ROLL_DAYS) -> dt.date:
    exp = monthly_expiry(today.year, today.month)
    if today > exp or trading_days_between(today, exp) < roll_days:
        y, m = (today.year + (today.month == 12), today.month % 12 + 1)
        exp = monthly_expiry(y, m)
    return exp


def is_future(symbol: str) -> bool:
    return symbol.upper().endswith("-FUT")


def resolve(symbol: str, today: dt.date | None = None) -> Instrument:
    s = symbol.strip().upper()
    if not is_future(s):
        return Instrument(kind="equity", symbol=s, data_symbol=s, tradingsymbol=s, lot_size=1, margin_pct=100.0)
    under = s[:-4]
    today = today or dt.date.today()
    exp = active_expiry(today)
    if under not in INDEX_FUTURES:
        from app.market import fno          # stock futures: lot size from NSE's daily F&O file
        try:
            lot = fno.lot_size(under, exp)
        except fno.FnoError as e:
            raise ValueError(f"no futures contract for {under}: {e}")
        return Instrument(kind="future", symbol=s, data_symbol=under, tradingsymbol=f"{under}{exp:%y}{MONTHS[exp.month - 1]}FUT",
                          lot_size=lot, margin_pct=STOCK_FUT_MARGIN_PCT, expiry=exp, name=f"{under} futures", price_is_proxy=True,
                          expiry_note=None if exp.year in known_years() else
                          f"NSE's {exp.year} holidays are not loaded yet: the expiry date may move (run the holiday sync)")
    spec = INDEX_FUTURES[under]
    return Instrument(kind="future", symbol=s, data_symbol=under,
                      tradingsymbol=f"{under}{exp:%y}{MONTHS[exp.month - 1]}FUT", lot_size=spec["lot_size"],
                      margin_pct=FUT_MARGIN_PCT, expiry=exp, name=f"{spec['name']} futures", price_is_proxy=True,
                      expiry_note=None if exp.year in known_years() else
                      f"NSE's {exp.year} holidays are not loaded yet: the expiry date may move (run the holiday sync)")


INDEX_FUTURE_SYMBOLS = [f"{u}-FUT" for u in INDEX_FUTURES]
