from __future__ import annotations

from dataclasses import dataclass, field

# timeframe -> (Yahoo interval, seconds per bar, default range, longest range Yahoo keeps)
TIMEFRAMES: dict[str, tuple[str, int, str, str]] = {
    "1m": ("1m", 60, "1d", "5d"),
    "5m": ("5m", 300, "5d", "60d"),
    "15m": ("15m", 900, "1mo", "60d"),
    "1h": ("60m", 3600, "3mo", "2y"),
    "1d": ("1d", 86400, "1y", "10y"),
}
RANGES = ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "60d")


@dataclass(frozen=True)
class Bar:
    t: int            # bar start, unix seconds (UTC)
    o: float
    h: float
    l: float          # noqa: E741
    c: float
    v: int
    complete: bool    # False for the bar still forming


@dataclass
class Candles:
    symbol: str               # our symbol: RELIANCE, NIFTY, BANKNIFTY
    provider_symbol: str      # RELIANCE.NS, ^NSEI
    name: str | None
    tf: str
    bars: list[Bar] = field(default_factory=list)
    prev_close: float | None = None
    last_price: float | None = None
    as_of: int | None = None  # provider's last trade time, unix seconds
    source: str = "yahoo"
    delayed: bool = True      # free data: treat as delayed until a broker feed replaces it


@dataclass(frozen=True)
class Quote:
    symbol: str
    last: float
    prev_close: float | None
    change_pct: float | None
    as_of: int | None
