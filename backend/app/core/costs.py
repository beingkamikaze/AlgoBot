"""Zerodha charges and NSE price ticks. Ported from the nse-budget skill.

Rates from zerodha.com/charges: equity intraday read 2026-10-23, futures and options
read 2026-10-24 (STT on futures is 0.05% and on options 0.15% of premium since the
2026 Union Budget). Every number here is the single source used by the backtester,
the paper engine and the risk engine; update it when the rates change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EQUITY_INTRADAY = {
    "brokerage_pct": 0.0003,       # 0.03 % or ₹20 per executed order, whichever is lower
    "brokerage_cap": 20.0,
    "stt_sell_pct": 0.00025,       # sell side only
    "exchange_pct": 0.0000307,     # NSE transaction charge, on turnover
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.00003,      # buy side only
    "gst_pct": 0.18,               # on brokerage + exchange + SEBI
}

EQUITY_DELIVERY = {                 # CNC, held overnight; zerodha.com/charges read 2026-11-11
    "brokerage_pct": 0.0,          # zero brokerage on delivery
    "brokerage_cap": 0.0,
    "stt_buy_pct": 0.001,          # 0.1 % on the buy side
    "stt_sell_pct": 0.001,         # and 0.1 % on the sell side
    "exchange_pct": 0.0000307,     # NSE transaction charge, on turnover
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.00015,      # 0.015 % on the buy side
    "gst_pct": 0.18,               # on brokerage + exchange + SEBI
    "dp_per_sell": 15.34,          # DP charge per scrip per day sold, GST included (₹3.5 CDSL + ₹9.5 Zerodha + GST)
}


FUTURES = {
    "brokerage_pct": 0.0003,       # 0.03 % or ₹20 per executed order, whichever is lower
    "brokerage_cap": 20.0,
    "stt_sell_pct": 0.0005,        # 0.05 % on the sell side
    "exchange_pct": 0.0000183,     # NSE transaction charge
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.00002,      # 0.002 % on the buy side
    "gst_pct": 0.18,
}

OPTIONS = {                         # all on the PREMIUM value
    "brokerage_flat": 20.0,        # ₹20 per executed order
    "stt_sell_pct": 0.0015,        # 0.15 % on the sell side
    "exchange_pct": 0.0003553,
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.00003,
    "gst_pct": 0.18,
}


@dataclass(frozen=True)
class Charges:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    dp: float = 0.0                # depository charge on a delivery sell (GST already inside)

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp, 2)


def equity_intraday_charges(buy_value: float, sell_value: float, buy_orders: int = 1,
                            sell_orders: int = 1, rates: dict = EQUITY_INTRADAY) -> Charges:
    def brokerage(value: float, orders: int) -> float:
        return min(rates["brokerage_cap"] * orders, rates["brokerage_pct"] * value) if value > 0 else 0.0

    brok = brokerage(buy_value, buy_orders) + brokerage(sell_value, sell_orders)
    turnover = buy_value + sell_value
    exchange = rates["exchange_pct"] * turnover
    sebi = turnover * rates["sebi_per_crore"] / 1e7
    return Charges(
        brokerage=round(brok, 2),
        stt=round(rates["stt_sell_pct"] * sell_value, 2),
        exchange=round(exchange, 2),
        sebi=round(sebi, 2),
        stamp=round(rates["stamp_buy_pct"] * buy_value, 2),
        gst=round(rates["gst_pct"] * (brok + exchange + sebi), 2),
    )


def equity_delivery_charges(buy_value: float, sell_value: float, sells: int = 1,
                            rates: dict = EQUITY_DELIVERY) -> Charges:
    """Zerodha charges on a delivery (CNC) round trip: STT both sides, stamp on the buy, and one DP
    charge for each day the shares are sold (`sells`)."""
    turnover = buy_value + sell_value
    exchange = rates["exchange_pct"] * turnover
    sebi = turnover * rates["sebi_per_crore"] / 1e7
    brok = rates["brokerage_pct"] * turnover
    return Charges(
        brokerage=round(brok, 2),
        stt=round(rates["stt_buy_pct"] * buy_value + rates["stt_sell_pct"] * sell_value, 2),
        exchange=round(exchange, 2),
        sebi=round(sebi, 2),
        stamp=round(rates["stamp_buy_pct"] * buy_value, 2),
        gst=round(rates["gst_pct"] * (brok + exchange + sebi), 2),
        dp=round(rates["dp_per_sell"] * sells, 2) if sell_value > 0 else 0.0,
    )


def futures_charges(buy_value: float, sell_value: float, buy_orders: int = 1, sell_orders: int = 1) -> Charges:
    """Zerodha charges on index or stock futures; values are contract values (price × quantity)."""
    return equity_intraday_charges(buy_value, sell_value, buy_orders, sell_orders, rates=FUTURES)


def options_charges(buy_premium: float, sell_premium: float, buy_orders: int = 1, sell_orders: int = 1,
                    rates: dict = OPTIONS) -> Charges:
    """Zerodha charges on options; values are PREMIUM values (premium × quantity)."""
    brok = (rates["brokerage_flat"] * buy_orders if buy_premium > 0 else 0.0) + \
           (rates["brokerage_flat"] * sell_orders if sell_premium > 0 else 0.0)
    turnover = buy_premium + sell_premium
    exchange = rates["exchange_pct"] * turnover
    sebi = turnover * rates["sebi_per_crore"] / 1e7
    return Charges(brokerage=round(brok, 2), stt=round(rates["stt_sell_pct"] * sell_premium, 2),
                   exchange=round(exchange, 2), sebi=round(sebi, 2), stamp=round(rates["stamp_buy_pct"] * buy_premium, 2),
                   gst=round(rates["gst_pct"] * (brok + exchange + sebi), 2))


def index_derivative_tick(price: float) -> float:
    """NSE's index-derivative tick by index level (NSE equity-derivatives page, updated 2026-02-10):
    0.05 up to 15,000, 0.10 to 30,000, 0.20 above. Kite's instrument list overrides it."""
    return 0.05 if price <= 15000 else (0.10 if price <= 30000 else 0.20)


def tick_for(price: float) -> float:
    """NSE cash-market tick by price band (NSE/CMTR/67133, from 2025-04-15). In live and
    paper trading the instrument's own tick_size from Kite's instrument list wins."""
    for upto, tick in ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00)):
        if price < upto:
            return tick
    return 5.00


def round_to_tick(price: float, tick: float, up: bool) -> float:
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 2)
