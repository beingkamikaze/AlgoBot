"""Event-driven intraday backtester for XAUUSD. Signals come from closed mid-price bars; fills come from the real bid and
ask on 1-minute bars, so what is inside a 5-minute bar happens in a more honest order.

Fill rules are pessimistic on purpose:
  - a signal on a bar's close enters at the next bar's open: longs at the ask, shorts at the bid, plus slippage;
  - no entry if that bar is on another trading day, more than `max_entry_delay` late, or the open is already past the stop;
  - a long's stop watches the bid and a short's the ask: that is the price you would really get out at;
  - stop and target in the same bar: the stop wins;
  - an open beyond the stop fills at the open (a gap), not at the stop;
  - a target fills only if price trades THROUGH it;
  - stops and time exits pay slippage; a target (a resting limit) does not;
  - everything still open is closed at the close of the bar ending at flat-by, whatever the day: nothing is ever held
    overnight, over a weekend or into a holiday, and short days exit earlier (`clock.day_window`);
  - trade management (`app/core/manage.py`, off by default): the stop in force when a bar opens is checked first, then a
    partial, then the target; a bar's range moves the stop only from the next bar on.

Spread and slippage are in the fill prices. `Trade.costs` adds them up so a report can say what they took: `gross` is what the
trade would have made at mid prices with no slippage or commission.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

import numpy as np

from app.core import manage
from app.global_markets.clock import TradingWindow, day_window, session_name
from app.global_markets.costs import CostModel, DEFAULT_SPREAD
from app.global_markets.data.bars import TF_SECONDS, Ohlcv
from app.global_markets.data.store import mid
from app.global_markets.instruments import GlobalInstrument, resolve
from app.global_markets.strategies.base import Candidate, Frame, Strategy, day_labels, open_mask
from app.market.models import Bar


@dataclass(frozen=True)
class BacktestConfig:
    risk_per_trade: float = 20.0        # dollars lost at the stop, before slippage overruns: sets the size
    max_position_value: float = 10_000.0    # no position worth more than this (no leverage on $10,000)
    costs: CostModel = CostModel()
    window: TradingWindow = TradingWindow()
    max_trades_per_day: int = 2
    min_risk_pct: float = 0.05          # skip stops closer than this % of price (inside the noise)
    max_entry_delay: int = 300          # seconds after the signal bar's close; later than this and the moment has passed


@dataclass
class Execution:
    """The bars fills happen on: bid and ask, usually 1-minute."""
    t: np.ndarray
    bo: np.ndarray
    bh: np.ndarray
    bl: np.ndarray
    bc: np.ndarray
    ao: np.ndarray
    ah: np.ndarray
    al: np.ndarray
    ac: np.ndarray
    day: np.ndarray
    seconds: int
    data_kind: str

    def __len__(self) -> int:
        return len(self.t)


@dataclass
class Trade:
    symbol: str
    side: str
    session: str                        # the trading day
    market_session: str                 # "asia", "london", "new_york" at entry
    signal_t: int
    entry_t: int
    exit_t: int
    entry: float
    stop: float
    target: float
    exit: float
    exit_reason: str                    # "stop", "target", "time", "session-end", "time-stop"
    lots: float
    risk_per_unit: float
    gross: float
    costs: float
    net: float
    r: float                            # net P&L / money at risk
    rule: str
    features: dict = field(default_factory=dict)
    management: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def from_columns(symbol: str, tf: str, columns: dict[str, np.ndarray]) -> tuple[Frame, Execution]:
    """Stored 1-minute bid and ask -> signal bars and fill bars. Minutes the clock calls closed are dropped from both."""
    keep = open_mask(columns["t"])
    c = {k: v[keep] for k, v in columns.items()}
    ex = Execution(t=c["t"], bo=c["bo"], bh=c["bh"], bl=c["bl"], bc=c["bc"], ao=c["ao"], ah=c["ah"], al=c["al"], ac=c["ac"],
                   day=day_labels(c["t"]).astype(str), seconds=60, data_kind="spot, real bid and ask")
    return Frame.from_m1(symbol, tf, mid(c)), ex


def from_bars(symbol: str, tf: str, bars: list[Bar], spread: float = DEFAULT_SPREAD) -> tuple[Frame, Execution]:
    """Bars with no bid or ask (`GC=F`): an assumed, fixed spread around them, fills on the bars themselves. A rougher test:
    the report says so."""
    done = [b for b in bars if b.complete]
    t = np.array([b.t for b in done], dtype=np.int64)
    keep = open_mask(t) if len(t) else np.array([], dtype=bool)
    arr = lambda name: np.array([getattr(b, name) for b in done], dtype=float)[keep]  # noqa: E731
    t, o, h, lo, c, v = t[keep], arr("o"), arr("h"), arr("l"), arr("c"), arr("v")
    half = spread / 2
    ex = Execution(t=t, bo=o - half, bh=h - half, bl=lo - half, bc=c - half, ao=o + half, ah=h + half, al=lo + half, ac=c + half,
                   day=day_labels(t).astype(str), seconds=TF_SECONDS[tf], data_kind=f"futures with an assumed ${spread:.2f} spread")
    return Frame(symbol, tf, Ohlcv(t, o, h, lo, c, v)), ex


def _size(inst: GlobalInstrument, entry: float, risk: float, cfg: BacktestConfig) -> float:
    lots = inst.lots_for_risk(cfg.risk_per_trade, risk, cfg.costs.round_trip_per_unit(inst.contract_size))
    most = math.floor(cfg.max_position_value / (entry * inst.contract_size) / inst.lot_step + 1e-9) * inst.lot_step
    return round(min(lots, most), 8) if most >= inst.min_lot else 0.0


def _windows(days: np.ndarray, window: TradingWindow) -> dict[str, tuple[float, float, float] | None]:
    out = {}
    for d in np.unique(days):
        w = day_window(dt.date.fromisoformat(str(d)), window)
        out[str(d)] = (w.entry_start.timestamp(), w.entry_end.timestamp(), w.flat_by.timestamp()) if w else None
    return out


def run(strategy: Strategy, f: Frame, ex: Execution, cfg: BacktestConfig = BacktestConfig(), inst: GlobalInstrument | None = None) -> list[Trade]:
    inst = inst or resolve(f.symbol)
    rules = manage.rules_from(strategy.params)
    atr = f.ind("atr", manage.ATR_PERIOD) if rules and rules["trail_atr"] > 0 else None
    closes = f.t + f.bar_seconds
    windows = _windows(ex.day, cfg.window)
    slip = cfg.costs.slippage
    trades: list[Trade] = []
    pending: Candidate | None = None
    pos: dict | None = None
    taken: dict[str, int] = {}
    sig = 0
    n = len(ex)
    for j in range(n):
        t, day = int(ex.t[j]), str(ex.day[j])
        w = windows[day]

        # 1. signal bars that have closed by now: their answer acts on this bar's open at the earliest
        while sig < len(f) and closes[sig] <= t:
            sig_day = str(f.day[sig])
            sw = windows.get(sig_day)
            if (pos is None and sw and sw[0] <= closes[sig] <= sw[1] and taken.get(sig_day, 0) < cfg.max_trades_per_day):
                pending = strategy.check(f, sig) or pending
            sig += 1

        # 2. a pending signal enters at this bar's open
        if pending is not None:
            cand, pending = pending, None
            if (str(f.day[cand.signal_index]) == day and pos is None and t - closes[cand.signal_index] <= cfg.max_entry_delay and w and t < w[2]):
                long = cand.side == "buy"
                raw = ex.ao[j] + slip if long else ex.bo[j] - slip
                entry = inst.round_price(float(raw), up=long)
                risk = entry - cand.stop if long else cand.stop - entry
                if risk > entry * cfg.min_risk_pct / 100:
                    lots = _size(inst, entry, risk, cfg)
                    if lots > 0:
                        target = entry + cand.target_r * risk if long else entry - cand.target_r * risk
                        steps = int(round(lots / inst.lot_step))
                        pos = {"cand": cand, "long": long, "entry": entry, "stop": cand.stop, "target": target, "risk": risk, "lots": lots,
                               "steps": steps, "entry_j": j, "half_in": (ex.ao[j] - ex.bo[j]) / 2, "m": None}
                        if rules:
                            a = float(atr[cand.signal_index]) if atr is not None else None
                            pos["m"] = manage.start(rules, long=long, entry=entry, stop=cand.stop, qty=steps, lot=1,
                                                    atr=a if a and not math.isnan(a) else None, entry_t=t, target_r=cand.target_r, tick=inst.tick)
                        taken[day] = taken.get(day, 0) + 1

        # 3. manage an open position on this bar
        if pos is not None:
            m, long, target = pos["m"], pos["long"], pos["target"]
            stop = m["stop"] if m else pos["stop"]
            gap_open = j > pos["entry_j"]
            high, low = (ex.bh[j], ex.bl[j]) if long else (ex.ah[j], ex.al[j])
            first, last = (ex.bo[j], ex.bc[j]) if long else (ex.ao[j], ex.ac[j])
            last_of_day = j == n - 1 or str(ex.day[j + 1]) != day
            exit_price = reason = None
            slipped = 0.0
            if long and low <= stop:
                exit_price, reason, slipped = (first if gap_open and first < stop else stop) - slip, "stop", slip
            elif not long and high >= stop:
                exit_price, reason, slipped = (first if gap_open and first > stop else stop) + slip, "stop", slip
            else:
                if m and manage.partial_hit(m, high, low):
                    manage.book_partial(m, t)
                if long and high > target or not long and low < target:
                    exit_price, reason = target, "target"
                elif (w and t + ex.seconds >= w[2]) or last_of_day:
                    reason = "time" if w and t + ex.seconds >= w[2] else "session-end"
                    exit_price, slipped = last - slip if long else last + slip, slip
                elif m and manage.time_stop_due(m, last, t + ex.seconds):
                    exit_price, reason, slipped = last - slip if long else last + slip, "time-stop", slip
            if exit_price is not None:
                half_out = (ex.ac[j] - ex.bc[j]) / 2
                trades.append(_close(f, ex, inst, cfg, pos, j, float(exit_price), reason, slipped, half_out))
                pos = None
            elif m:
                manage.after_bar(m, high, low, t + ex.seconds)
    return trades


def _close(f: Frame, ex: Execution, inst: GlobalInstrument, cfg: BacktestConfig, pos: dict, j: int, exit_price: float, reason: str,
           slipped: float, half_out: float) -> Trade:
    cand: Candidate = pos["cand"]
    long, entry, m = pos["long"], pos["entry"], pos["m"]
    part = m["partial"] if m else None
    # (steps, price, slippage on that exit): a partial is a resting limit, so it pays none
    legs = [(part["qty"], part["price"], 0.0), (pos["steps"] - part["qty"], exit_price, slipped)] if part else [(pos["steps"], exit_price, slipped)]
    net = paid = 0.0
    for steps, px, leg_slip in legs:
        units = inst.units(steps * inst.lot_step)
        net += (px - entry) * units if long else (entry - px) * units
        paid += (pos["half_in"] + cfg.costs.slippage + half_out + leg_slip) * units
    commission = cfg.costs.commission_per_lot * pos["lots"]
    net -= commission
    costs = paid + commission
    at_risk = pos["risk"] * inst.units(pos["lots"])
    entry_t = int(ex.t[pos["entry_j"]])
    return Trade(symbol=f.symbol, side=cand.side, session=str(ex.day[pos["entry_j"]]),
                 market_session=session_name(dt.datetime.fromtimestamp(entry_t, dt.timezone.utc)) or "",
                 signal_t=int(f.t[cand.signal_index]), entry_t=entry_t, exit_t=int(ex.t[j]), entry=round(entry, 2), stop=round(pos["stop"], 2),
                 target=round(pos["target"], 2), exit=round(exit_price, 2), exit_reason=reason, lots=pos["lots"], risk_per_unit=round(pos["risk"], 4),
                 gross=round(net + costs, 2), costs=round(costs, 2), net=round(net, 2), r=round(net / at_risk, 4) if at_risk else 0.0, rule=cand.rule,
                 features=dict(cand.features), management=manage.summary(m) if m else None)
