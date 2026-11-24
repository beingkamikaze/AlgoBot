"""What can be traded in the global setup. Add a new instrument to `INSTRUMENTS`; everything that sizes or prices a
trade asks this module.

XAUUSD is quoted in dollars an ounce and traded in lots. These are the common CFD terms (100 oz a lot, 0.01 lot
minimum, $0.01 tick); a broker's own specification replaces them when one is chosen (plan phase X8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class GlobalInstrument:
    symbol: str
    name: str
    quote_currency: str
    contract_size: float                # units in one lot: ounces for gold
    min_lot: float
    lot_step: float
    tick: float

    @property
    def _tick_decimals(self) -> int:
        return max(0, -math.floor(math.log10(self.tick)))

    def units(self, lots: float) -> float:
        return round(lots * self.contract_size, 6)

    def is_valid_lots(self, lots: float) -> bool:
        steps = lots / self.lot_step
        return lots >= self.min_lot - 1e-9 and abs(steps - round(steps)) < 1e-6

    def round_price(self, price: float, up: bool) -> float:
        n = price / self.tick
        n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
        return round(n * self.tick, self._tick_decimals)

    def is_on_tick(self, price: float) -> bool:
        return abs(price / self.tick - round(price / self.tick)) < 1e-6

    def gross_pnl(self, side: str, entry: float, exit_price: float, lots: float) -> float:
        """Profit or loss in the quote currency, before costs."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        move = exit_price - entry if side == "buy" else entry - exit_price
        return round(move * self.units(lots), 2)

    def lots_for_risk(self, max_loss: float, stop_distance: float, cost_per_unit: float = 0.0) -> float:
        """The most lots whose loss at the stop, costs included, stays inside `max_loss`. 0 means even the smallest
        lot risks too much: the trade is refused, never rounded up."""
        if stop_distance <= 0:
            raise ValueError("stop distance must be above zero")
        if max_loss <= 0 or cost_per_unit < 0:
            raise ValueError("max loss must be above zero and costs cannot be negative")
        units = max_loss / (stop_distance + cost_per_unit)
        steps = math.floor(units / self.contract_size / self.lot_step + 1e-9)
        lots = round(steps * self.lot_step, 8)
        return lots if lots >= self.min_lot - 1e-9 else 0.0


INSTRUMENTS: dict[str, GlobalInstrument] = {
    "XAUUSD": GlobalInstrument(symbol="XAUUSD", name="Gold spot", quote_currency="USD", contract_size=100.0,
                               min_lot=0.01, lot_step=0.01, tick=0.01),
}


def normalize(symbol: str) -> str:
    return symbol.strip().upper().replace("/", "").replace("_", "").replace(" ", "")


def resolve(symbol: str) -> GlobalInstrument:
    s = normalize(symbol)
    if s not in INSTRUMENTS:
        raise ValueError(f"{symbol!r} is not in the global setup (available: {', '.join(INSTRUMENTS)})")
    return INSTRUMENTS[s]
