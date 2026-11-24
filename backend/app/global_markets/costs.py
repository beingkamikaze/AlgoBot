"""What a XAUUSD trade costs. The spread is real where we have bid and ask (Dukascopy) and assumed where we do not. Slippage
and commission are assumptions until a broker is chosen: a broker's own numbers replace them (plan phase X8)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from app.global_markets.clock import NEW_YORK

DEFAULT_SPREAD = 0.60       # dollars an ounce: Dukascopy, London and New York hours, 1 and 2 Oct 2026


@dataclass(frozen=True)
class CostModel:
    slippage: float = 0.10              # dollars an ounce against you, on each market-type fill (a limit target pays none)
    commission_per_lot: float = 0.0     # dollars a lot, round trip

    def __post_init__(self) -> None:
        if self.slippage < 0 or self.commission_per_lot < 0:
            raise ValueError("costs cannot be negative")

    def round_trip_per_unit(self, contract_size: float) -> float:
        """Costs beyond the spread, an ounce, when sizing a trade: the spread is already in the entry price."""
        return 2 * self.slippage + self.commission_per_lot / contract_size


def spread_by_hour(columns: dict[str, np.ndarray]) -> list[dict]:
    """Median and 90th-percentile spread for each New York hour, from 1-minute closes. One row per hour with data."""
    spread = columns["ac"] - columns["bc"]
    hours, inverse = np.unique(columns["t"] // 3600, return_inverse=True)
    ny_hours = np.array([dt.datetime.fromtimestamp(int(h) * 3600, NEW_YORK).hour for h in hours])[inverse]
    rows = []
    for h in np.unique(ny_hours):
        s = spread[ny_hours == h]
        rows.append({"new_york_hour": int(h), "median": round(float(np.median(s)), 3), "p90": round(float(np.percentile(s, 90)), 3), "minutes": int(len(s))})
    return rows
