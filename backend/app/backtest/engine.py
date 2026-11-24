"""Event-driven intraday backtester (guide section 3.5).

Fill rules are pessimistic on purpose:
  - a signal on bar i's close enters at bar i+1's OPEN, plus slippage;
  - if the next open is already beyond the stop, the trade is skipped;
  - stop and target in the same bar: the stop wins;
  - an open beyond the stop fills at the open (a gap), not at the stop;
  - a target fills only if price trades THROUGH it (high > target for a long);
  - stops and time exits pay slippage; a target (a resting limit) does not;
  - everything still open is closed at the close of the bar ending at flat_by (15:15),
    or at the session's last bar;
  - Zerodha's equity-intraday charges come off every trade (and off each partial exit separately);
  - trade management (app/core/manage.py, off by default): the stop in force when a bar opens is checked
    first, then a partial, then the target; a bar's range moves the stop only from the next bar on.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from app.core import manage
from app.core.costs import round_to_tick
from app.core.instruments import Instrument, resolve
from app.strategies.base import Candidate, Frame, Strategy


@dataclass
class BacktestConfig:
    risk_per_trade: float = 2000.0       # ₹ lost at the stop (before charges): sets the quantity
    capital_per_trade: float = 200000.0  # no single position larger than this (no leverage)
    slippage_bps: float = 2.0            # 0.02 % against you on market-type fills
    flat_by_minute: int = 360            # 9:15 + 360 min = 15:15
    max_trades_per_session: int = 2      # per symbol
    min_risk_pct: float = 0.05           # skip stops closer than this % of price (inside the noise)
    fixed_lots: int | None = None        # futures: trade this many lots whatever the risk (R stays per-lot)


@dataclass
class Trade:
    symbol: str
    side: str
    session: str
    signal_t: int
    entry_t: int
    exit_t: int
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str                   # "stop", "target", "time", "session-end", "time-stop"
    qty: int
    risk_per_share: float
    gross: float
    costs: float
    net: float
    r: float                           # net P&L / money at risk
    rule: str
    features: dict = field(default_factory=dict)
    regime: dict = field(default_factory=dict)
    management: dict | None = None     # rules, the stop's moves and the partial (None: unmanaged)

    def to_dict(self) -> dict:
        return asdict(self)


def _slip(price: float, bps: float, worse_up: bool) -> float:
    return price * (1 + bps / 1e4) if worse_up else price * (1 - bps / 1e4)


def size(entry: float, risk_per_share: float, cfg: BacktestConfig, inst: Instrument | None = None) -> int:
    """Quantity in units (shares, or lots × lot size)."""
    if risk_per_share <= 0:
        return 0
    lot = inst.lot_size if inst else 1
    if inst is not None and inst.kind == "future" and cfg.fixed_lots:
        return cfg.fixed_lots * lot
    margin_pct = inst.margin_pct if inst else 100.0
    lots = math.floor(min(cfg.risk_per_trade / (risk_per_share * lot),
                          cfg.capital_per_trade / (entry * lot * margin_pct / 100)))
    return int(lots) * lot


def run_symbol(strategy: Strategy, f: Frame, cfg: BacktestConfig, regime_for=None,
               inst: Instrument | None = None) -> list[Trade]:
    inst = inst or resolve(f.symbol)
    rules = manage.rules_from(strategy.params)
    atr = f.ind("atr", manage.ATR_PERIOD) if rules and rules["trail_atr"] > 0 else None
    bar_s = f.bar_minutes * 60
    trades: list[Trade] = []
    n = len(f)
    pending: Candidate | None = None
    pos: dict | None = None
    taken: dict[str, int] = {}
    for i in range(n):
        last_of_session = i == n - 1 or f.session[i + 1] != f.session[i]

        # 1. a pending signal enters at this bar's open
        if pending is not None:
            cand, pending = pending, None
            if f.session[i] == f.session[cand.signal_index]:
                long = cand.side == "buy"
                tick = inst.tick(f.o[i])
                entry = round_to_tick(_slip(f.o[i], cfg.slippage_bps, long), tick, up=long)
                stop = cand.stop
                risk = entry - stop if long else stop - entry
                if risk > entry * cfg.min_risk_pct / 100:
                    qty = size(entry, risk, cfg, inst)
                    if qty > 0:
                        target = entry + cand.target_r * risk if long else entry - cand.target_r * risk
                        pos = {"cand": cand, "long": long, "entry": entry, "stop": stop, "target": target,
                               "risk": risk, "qty": qty, "entry_i": i, "m": None}
                        if rules:
                            a = float(atr[cand.signal_index]) if atr is not None else None
                            pos["m"] = manage.start(rules, long=long, entry=entry, stop=stop, qty=qty, lot=inst.lot_size,
                                                    atr=a if a and not math.isnan(a) else None, entry_t=int(f.t[i]),
                                                    target_r=cand.target_r, tick=tick)
                        taken[f.session[i]] = taken.get(f.session[i], 0) + 1

        # 2. manage an open position on this bar
        if pos is not None:
            m = pos["m"]
            long, target = pos["long"], pos["target"]
            stop = m["stop"] if m else pos["stop"]
            exit_price = reason = None
            gap_open = i > pos["entry_i"]
            if long and f.l[i] <= stop:
                base = f.o[i] if gap_open and f.o[i] < stop else stop
                exit_price, reason = _slip(base, cfg.slippage_bps, worse_up=False), "stop"
            elif not long and f.h[i] >= stop:
                base = f.o[i] if gap_open and f.o[i] > stop else stop
                exit_price, reason = _slip(base, cfg.slippage_bps, worse_up=True), "stop"
            else:
                if m and manage.partial_hit(m, f.h[i], f.l[i]):
                    manage.book_partial(m, int(f.t[i]))
                if long and f.h[i] > target:
                    exit_price, reason = target, "target"
                elif not long and f.l[i] < target:
                    exit_price, reason = target, "target"
                elif f.minute[i] + f.bar_minutes >= cfg.flat_by_minute or last_of_session:
                    exit_price = _slip(f.c[i], cfg.slippage_bps, worse_up=not long)
                    reason = "time" if f.minute[i] + f.bar_minutes >= cfg.flat_by_minute else "session-end"
                elif m and manage.time_stop_due(m, f.c[i], int(f.t[i]) + bar_s):
                    exit_price, reason = _slip(f.c[i], cfg.slippage_bps, worse_up=not long), "time-stop"
            if exit_price is not None:
                trades.append(_close(f, pos, i, exit_price, reason, regime_for, inst))
                pos = None
            elif m:
                manage.after_bar(m, f.h[i], f.l[i], int(f.t[i]) + bar_s)

        # 3. look for a new signal (acted on at the next bar's open)
        if (pos is None and not last_of_session and taken.get(f.session[i], 0) < cfg.max_trades_per_session
                and f.minute[i] + f.bar_minutes < cfg.flat_by_minute):
            cand = strategy.check(f, i)
            if cand is not None:
                pending = cand
    return trades


def _close(f: Frame, pos: dict, i: int, exit_price: float, reason: str, regime_for, inst: Instrument) -> Trade:
    cand: Candidate = pos["cand"]
    qty, entry, long, m = pos["qty"], pos["entry"], pos["long"], pos["m"]
    part = m["partial"] if m else None
    # each exit order (the partial, then the rest) pays its own charges
    legs = [(part["qty"], part["price"]), (qty - part["qty"], exit_price)] if part else [(qty, exit_price)]
    gross = costs = 0.0
    for q, px in legs:
        buy_v, sell_v = (entry * q, px * q) if long else (px * q, entry * q)
        gross += (px - entry) * q if long else (entry - px) * q
        costs += inst.charges(buy_v, sell_v).total
    net = gross - costs
    at_risk = pos["risk"] * qty
    session = str(f.session[pos["entry_i"]])
    return Trade(
        symbol=f.symbol, side=cand.side, session=session, signal_t=int(f.t[cand.signal_index]),
        entry_t=int(f.t[pos["entry_i"]]), exit_t=int(f.t[i]), entry=round(entry, 2), stop=round(pos["stop"], 2),
        target=round(pos["target"], 2), exit=round(float(exit_price), 2), exit_reason=reason, qty=qty,
        risk_per_share=round(pos["risk"], 4), gross=round(gross, 2), costs=costs, net=round(net, 2),
        r=round(net / at_risk, 4) if at_risk else 0.0, rule=cand.rule, features=dict(cand.features),
        regime=regime_for(session, cand) if regime_for else {}, management=manage.summary(m) if m else None,
    )


def trades_array(trades: list[Trade], key: str) -> np.ndarray:
    return np.array([getattr(t, key) for t in trades], dtype=float)
