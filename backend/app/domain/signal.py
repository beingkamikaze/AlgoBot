"""The signal and its "why this trade" explanation (guide section 3.12).

Every field that states a fact is filled by code. The language model only writes
`summary`, and only from the other fields, so it cannot add a reason that is not
in the evidence.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Condition(BaseModel):
    """One check a strategy made, e.g. "price above VWAP"."""
    name: str
    met: bool
    detail: str                       # "1,204.2 > VWAP 1,195.8"


class TradePlan(BaseModel):
    entry: float
    stop: float
    stop_reason: str                  # "opening-range midpoint"
    target: float | None = None
    target_reason: str | None = None  # "2R"
    exit_by: dt.time | None = None    # forced time exit, e.g. 15:15
    quantity: int | None = None
    max_loss_with_charges: float | None = None
    instrument: str = "equity"        # "equity" or "future"
    contract: str | None = None       # e.g. NIFTY26OCTFUT
    lot_size: int | None = None
    lots: int | None = None
    notional: float | None = None     # position value
    margin_estimate: float | None = None
    # trade-management rules fixed before entry (app/core/manage.py): the rule values, the ATR(14) at the
    # signal bar for trailing, and a plain description. None: the stop and target never move.
    management: dict | None = None


class Evidence(BaseModel):
    """Track record of this exact strategy version, from SQL."""
    source: str                       # "backtest", "paper", "live"
    trades: int
    expectancy_r: float | None = None # average R per trade after costs
    profit_factor: float | None = None
    win_rate: float | None = None
    regime: str | None = None         # set when the numbers are for the current regime only


class SimilarTrades(BaseModel):
    count: int
    wins: int
    losses: int
    average_r: float | None = None
    note: str | None = None           # a pattern found by SQL, e.g. "3 of 4 losses after 14:00"


class Confidence(StrEnum):
    NONE = "no track record"          # new version or too few trades: paper only
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Explanation(BaseModel):
    rule: str                                         # the condition that fired, with numbers
    conditions: list[Condition] = Field(default_factory=list)
    plan: TradePlan
    evidence: list[Evidence] = Field(default_factory=list)
    similar: SimilarTrades | None = None
    risks: list[str] = Field(default_factory=list)    # results tomorrow, VIX rising, ...
    invalidation: str                                 # what cancels the idea
    confidence: Confidence = Confidence.NONE
    summary: str | None = None                        # plain words, written last, from the above

    @model_validator(mode="after")
    def stop_on_the_right_side(self) -> "Explanation":
        p = self.plan
        if p.stop == p.entry:
            raise ValueError("the stop cannot equal the entry")
        return self


def grade_confidence(evidence: list[Evidence], min_trades: int = 100) -> Confidence:
    """Fixed rule, never a model's opinion: enough trades and a positive expectancy after costs."""
    best = max((e for e in evidence if e.expectancy_r is not None), key=lambda e: e.trades, default=None)
    if best is None or best.trades < 30:
        return Confidence.NONE
    if best.expectancy_r <= 0:
        return Confidence.LOW
    if best.trades >= min_trades and best.expectancy_r >= 0.10 and (best.profit_factor or 0) >= 1.3:
        return Confidence.HIGH
    return Confidence.MEDIUM


class Signal(BaseModel):
    strategy_id: str
    strategy_version: str
    symbol: str
    exchange: str = "NSE"
    side: Side
    created_at: dt.datetime
    explanation: Explanation
