"""Swing backtester on daily bars, held for days in delivery (CNC). Separate from the intraday engine.

Fill rules, pessimistic on purpose:
  - a signal on day i's close enters at day i+1's OPEN plus slippage (rounded up to the tick);
  - if that open is already at or below the stop, the trade is skipped;
  - each day the stop is checked first: a day whose low reaches the stop is a stop, even if its high also
    reached the target; an open below the stop (a gap) fills at the open, not at the stop; stops pay slippage;
  - a target is a resting limit sell: it fills only if the high trades THROUGH it, at the target (or at the
    open, if the day opens above it); no slippage;
  - an exit rule that fires on day j's close (e.g. "close above the 5-day average") sells at day j+1's open,
    with slippage;
  - the maximum holding period sells at the close of the last allowed day, with slippage;
  - a day marked `blocked` in the data (a demerger or a data disagreement, see app.swing.data) may not be
    entered on, and a position still open the evening before one is sold at that evening's close;
  - charges: Zerodha delivery (CNC) for overnight holds, including the DP charge on the sell; a position
    bought and sold on the same day is charged as intraday, as Zerodha does;
  - long only; when the frame carries an `in_universe` series (app.swing.universe), new signals only on days
    the stock is in the point-in-time universe (an open position is managed to its own exit either way).
Position size is a fixed amount of money per trade (no leverage), so "% per trade" means the same thing for
every trade; R is net P&L over the money at risk to the strategy's stop (or to its notional risk unit when
the strategy uses no stop order, which `risk_unit` says).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from app.core.costs import equity_delivery_charges, equity_intraday_charges, round_to_tick, tick_for
from app.swing.data import Daily


@dataclass
class SwingConfig:
    capital_per_trade: float = 100000.0   # ₹ per position
    slippage_bps: float = 5.0             # 0.05 % against you on market-type fills (daily open/close auctions)


@dataclass
class Entry:
    """What a strategy returns on day i's close."""
    stop: float | None          # stop order price, or None (no stop order)
    risk: float                 # the R unit per share: entry minus stop, or the notional risk when stop is None
    target: float | None = None
    max_hold: int = 10          # sessions held, counting the entry day
    rule: str = ""
    features: dict = field(default_factory=dict)


@dataclass
class SwingTrade:
    symbol: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    stop: float | None
    target: float | None
    exit_reason: str            # stop, target, rule, time, blocked, end
    qty: int
    days: int                   # sessions the money was in the position
    gross: float
    costs: float
    net: float
    r: float
    pct: float                  # net P&L as % of the money invested
    cost_r: float
    rule: str
    features: dict = field(default_factory=dict)

    @property
    def session(self) -> str:   # the Lab's split key
        return self.entry_date

    def to_dict(self) -> dict:
        return {**asdict(self), "session": self.session}


class SwingStrategy:
    id = "swing"
    name = "Swing"
    defaults: dict = {}

    def __init__(self, **params):
        self.params = {**self.defaults, **params}

    def prepare(self, f: Daily) -> dict:
        """Indicators for the whole frame (each value at i uses only days up to i)."""
        return {}

    def entry(self, f: Daily, ind: dict, i: int) -> Entry | None:
        raise NotImplementedError

    def exit_on_close(self, f: Daily, ind: dict, i: int, pos: dict) -> bool:
        return False


def _slip(price: float, bps: float, up: bool) -> float:
    return price * (1 + bps / 1e4) if up else price * (1 - bps / 1e4)


def run(strategy: SwingStrategy, f: Daily, cfg: SwingConfig | None = None) -> list[SwingTrade]:
    cfg = cfg or SwingConfig()
    ind = strategy.prepare(f)
    n = len(f)
    trades: list[SwingTrade] = []
    pending: tuple[int, Entry] | None = None
    pos: dict | None = None
    for i in range(n):
        # 1. a pending signal enters at today's open
        if pending is not None:
            si, e = pending
            pending = None
            if not f.blocked[i]:
                entry = round_to_tick(_slip(f.o[i], cfg.slippage_bps, True), tick_for(f.o[i]), up=True)
                qty = math.floor(cfg.capital_per_trade / entry)
                if (e.stop is None or f.o[i] > e.stop) and qty > 0 and e.risk > 0:
                    risk = (entry - e.stop) if e.stop is not None else e.risk
                    pos = {"si": si, "ei": i, "entry": entry, "qty": qty, "stop": e.stop, "risk": risk,
                           "target": e.target, "max_hold": e.max_hold, "rule": e.rule, "features": e.features,
                           "sell_next_open": False}

        # 2. manage the open position today
        if pos is not None:
            px = reason = None
            at_close = False
            if pos["sell_next_open"]:
                px, reason = _slip(f.o[i], cfg.slippage_bps, False), "rule"
            elif pos["stop"] is not None and f.l[i] <= pos["stop"]:
                base = f.o[i] if f.o[i] < pos["stop"] else pos["stop"]
                px, reason = _slip(base, cfg.slippage_bps, False), "stop"
            elif pos["target"] is not None and f.h[i] > pos["target"]:
                px, reason = max(f.o[i], pos["target"]), "target"
            elif i - pos["ei"] + 1 >= pos["max_hold"]:
                px, reason, at_close = _slip(f.c[i], cfg.slippage_bps, False), "time", True
            elif i + 1 < n and f.blocked[i + 1]:
                px, reason, at_close = _slip(f.c[i], cfg.slippage_bps, False), "blocked", True
            elif i == n - 1:
                px, reason, at_close = f.c[i], "end", True
            elif strategy.exit_on_close(f, ind, i, pos):
                pos["sell_next_open"] = True
            if px is not None:
                trades.append(_close(f, pos, i, px, reason, at_close))
                pos = None
                continue            # no new signal on an exit day: tomorrow's open is not double-booked

        # 3. a new signal on today's close, acted on at tomorrow's open
        member = f.extra.get("in_universe")
        if pos is None and pending is None and i + 1 < n and (member is None or member[i]):
            e = strategy.entry(f, ind, i)
            if e is not None:
                pending = (i, e)
    return trades


def charges(entry: float, exit_price: float, qty: int, same_day: bool) -> float:
    buy, sell = entry * qty, exit_price * qty
    return (equity_intraday_charges(buy, sell) if same_day else equity_delivery_charges(buy, sell)).total


def _close(f: Daily, pos: dict, i: int, px: float, reason: str, at_close: bool) -> SwingTrade:
    qty, entry = pos["qty"], pos["entry"]
    same_day = i == pos["ei"]
    gross = (px - entry) * qty
    costs = charges(entry, px, qty, same_day)
    net = gross - costs
    at_risk = pos["risk"] * qty
    days = max(1, i - pos["ei"] + (1 if at_close or same_day else 0))
    return SwingTrade(
        symbol=f.symbol, signal_date=f.dates[pos["si"]], entry_date=f.dates[pos["ei"]], exit_date=f.dates[i],
        entry=round(entry, 2), exit=round(float(px), 2), stop=round(pos["stop"], 2) if pos["stop"] is not None else None,
        target=round(pos["target"], 2) if pos["target"] is not None else None, exit_reason=reason, qty=qty, days=days,
        gross=round(gross, 2), costs=round(costs, 2), net=round(net, 2), r=round(net / at_risk, 4) if at_risk else 0.0,
        pct=round(net / (entry * qty) * 100, 4), cost_r=round(costs / at_risk, 4) if at_risk else 0.0,
        rule=pos["rule"], features=dict(pos["features"]),
    )


# ---------------------------------------------------------------- statistics

def summary(trades: list[SwingTrade], years: float | None = None) -> dict:
    """Everything after costs. `years`: length of the test, for trades per year."""
    if not trades:
        return {"trades": 0}
    r = np.array([t.r for t in trades])
    pct = np.array([t.pct for t in trades])
    net = np.array([t.net for t in trades])
    days = np.array([t.days for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    # a ₹1 position kept busy all year: compound the average daily return while invested over 248 sessions
    per_day = pct.sum() / days.sum() / 100
    out = {
        "trades": len(trades),
        "avg_r": round(float(r.mean()), 4),
        "avg_pct": round(float(pct.mean()), 3),
        "median_pct": round(float(np.median(pct)), 3),
        "cost_r": round(float(np.mean([t.cost_r for t in trades])), 4),
        "cost_pct": round(float(np.mean([t.costs / (t.entry * t.qty) * 100 for t in trades])), 3),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
        "win_rate": round(float((net > 0).mean()), 4),
        "avg_days": round(float(days.mean()), 2),
        "pct_per_year_invested": round(((1 + per_day) ** 248 - 1) * 100, 2),
        "exit_reasons": {k: int(sum(1 for t in trades if t.exit_reason == k))
                         for k in ("stop", "target", "rule", "time", "blocked", "end")},
    }
    if years:
        out["trades_per_year"] = round(len(trades) / years, 1)
    return out
