"""The risk check for global paper orders, in dollars. Pure: an order, your limits, the account's state and the clock in;
PASS or BLOCK with plain reasons out. Exits are never blocked: getting out must always be possible.

Prices: a long buys at the ask and its stop and target watch the bid (a short the other way round), as in the backtester.
The max loss counts the distance to the stop plus slippage and commission; a jump past the stop can still lose more.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field

from app.global_markets.clock import IST, TradingWindow, day_window, market_status
from app.global_markets.costs import CostModel
from app.global_markets.instruments import GlobalInstrument

STALE_PRICE = 20 * 60          # seconds: an older last price cannot price a ticket
DELAYED_PRICE = 3 * 60         # seconds: older than this, the ticket says so
FAIR_FILL_PCT = 0.25           # a limit further than this from the last price is not a fair paper fill


@dataclass
class Limits:
    max_loss_per_trade: float = 20.0
    max_daily_loss: float = 60.0
    max_monthly_loss: float = 300.0
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    max_open_positions: int = 1
    max_position_value: float = 10_000.0
    entry_start_london: dt.time = dt.time(8, 0)
    entry_end_new_york: dt.time = dt.time(11, 0)
    flat_by_new_york: dt.time = dt.time(13, 0)

    def window(self) -> TradingWindow:
        return TradingWindow(entry_start=self.entry_start_london, entry_end=self.entry_end_new_york, flat_by=self.flat_by_new_york)

    @classmethod
    def from_row(cls, row: dict) -> "Limits":
        d = cls()

        def t(v, default):
            return dt.time.fromisoformat(str(v)[:5]) if v else default
        return cls(
            max_loss_per_trade=float(row.get("max_loss_per_trade") or d.max_loss_per_trade),
            max_daily_loss=float(row.get("max_daily_loss") or d.max_daily_loss),
            max_monthly_loss=float(row.get("max_monthly_loss") or d.max_monthly_loss),
            max_trades_per_day=int(row.get("max_trades_per_day") or d.max_trades_per_day),
            max_consecutive_losses=int(row.get("max_consecutive_losses") or d.max_consecutive_losses),
            max_open_positions=int(row.get("max_open_positions") or d.max_open_positions),
            max_position_value=float(row.get("max_position_value") or d.max_position_value),
            entry_start_london=t(row.get("entry_start_london"), d.entry_start_london),
            entry_end_new_york=t(row.get("entry_end_new_york"), d.entry_end_new_york),
            flat_by_new_york=t(row.get("flat_by_new_york"), d.flat_by_new_york),
        )

    def to_dict(self) -> dict:
        return {k: (v.strftime("%H:%M") if isinstance(v, dt.time) else v) for k, v in asdict(self).items()}


@dataclass
class OpenPosition:
    symbol: str
    side: str
    units: float
    entry: float
    stop: float


@dataclass
class AccountState:
    capital: float                       # starting capital + all realized P&L
    open_positions: list[OpenPosition] = field(default_factory=list)
    working_value: float = 0.0           # dollars in entry orders still working
    entries_today: int = 0
    realized_today: float = 0.0          # net, this trading day (it rolls at 17:00 New York)
    realized_month: float = 0.0
    consecutive_losses: int = 0          # losing trades in a row, this trading day
    kill_switch: bool = False

    @property
    def open_risk(self) -> float:
        return sum(p.units * abs(p.entry - p.stop) for p in self.open_positions)

    @property
    def open_value(self) -> float:
        return sum(p.units * p.entry for p in self.open_positions)


@dataclass
class OrderRequest:
    inst: GlobalInstrument
    side: str
    lots: float
    limit: float
    stop: float | None
    target: float | None = None
    leg: str = "entry"


@dataclass
class PriceInfo:
    last: float | None
    age_seconds: float | None            # how old the last bar is (its close)
    source: str                          # "spot" or "futures"


# Blocks no override can lift, even on paper: the order is malformed, a fair fill cannot be simulated, the setup has already
# failed, or it is your own emergency brake. Everything else is one of YOUR limits.
HARD_BLOCKS = ("side must be", "lots must be", "a limit price is required", "is not on the", "market closed",
               "the kill switch is on", "a stop is required", "is on the wrong side", "no recent price", "too far to be a fair fill",
               "is already beyond the stop", "past today's flat-by")


def is_hard(reason: str) -> bool:
    return any(h in reason for h in HARD_BLOCKS)


@dataclass
class Verdict:
    verdict: str
    reasons: list[str]
    warnings: list[str]
    numbers: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hard"] = [r for r in self.reasons if is_hard(r)]
        d["overridable"] = self.verdict == "BLOCK" and not d["hard"]
        return d


def usd(x: float) -> str:
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def max_lots(inst: GlobalInstrument, entry: float, stop: float, lim: Limits, costs: CostModel = CostModel()) -> float:
    """The most lots inside both your max loss per trade and your position cap. 0 means even the smallest lot is too big."""
    by_risk = inst.lots_for_risk(lim.max_loss_per_trade, abs(entry - stop), costs.round_trip_per_unit(inst.contract_size))
    steps = math.floor(lim.max_position_value / (entry * inst.contract_size) / inst.lot_step + 1e-9)
    by_value = round(steps * inst.lot_step, 8)
    lots = min(by_risk, by_value)
    return lots if lots >= inst.min_lot - 1e-9 else 0.0


def check(o: OrderRequest, lim: Limits, st: AccountState, now: dt.datetime, price: PriceInfo,
          costs: CostModel = CostModel()) -> Verdict:
    block: list[str] = []
    warn: list[str] = []
    nums: dict = {}
    inst = o.inst
    long = o.side == "buy"

    if o.side not in ("buy", "sell"):
        block.append("side must be buy or sell")
    if not inst.is_valid_lots(o.lots):
        block.append(f"lots must be {inst.min_lot:g} or more, in steps of {inst.lot_step:g}")
    if o.limit is None or o.limit <= 0:
        block.append("a limit price is required (no market orders)")
    elif not inst.is_on_tick(o.limit):
        block.append(f"limit {o.limit} is not on the ${inst.tick:g} tick")
    if block:
        return Verdict("BLOCK", block, warn, nums)
    if o.leg == "exit":
        return Verdict("PASS", [], ["exits are never blocked"], nums)

    m = market_status(now)
    if not m.is_open:
        block.append(f"market closed ({m.reason})")
    else:
        w = day_window(m.trading_day, lim.window())
        if m.now >= w.flat_by:
            block.append(f"past today's flat-by time {w.flat_by.astimezone(IST):%H:%M} IST: a new trade would close at once")
        elif not (w.entry_start <= m.now <= w.entry_end):
            block.append(f"outside your entry window {w.entry_start.astimezone(IST):%H:%M}–{w.entry_end.astimezone(IST):%H:%M} IST "
                         f"({lim.entry_start_london:%H:%M} London to {lim.entry_end_new_york:%H:%M} New York)")
    if st.kill_switch:
        block.append("the kill switch is on for this trading day")

    if o.stop is None:
        block.append("a stop is required on every entry")
        return Verdict("BLOCK", block, warn, nums)
    if (long and o.stop >= o.limit) or (not long and o.stop <= o.limit):
        block.append(f"the stop {o.stop} is on the wrong side of the entry {o.limit}")
        return Verdict("BLOCK", block, warn, nums)
    if o.target is not None and ((long and o.target <= o.limit) or (not long and o.target >= o.limit)):
        block.append(f"the target {o.target} is on the wrong side of the entry {o.limit}")

    units = inst.units(o.lots)
    risk_unit = abs(o.limit - o.stop)
    extra = units * costs.round_trip_per_unit(inst.contract_size)
    loss = units * risk_unit + extra
    value = units * o.limit
    nums.update(units=units, value=round(value, 2), risk_per_unit=round(risk_unit, 2), max_loss=round(loss, 2),
                costs_at_stop=round(extra, 2), open_risk_before=round(st.open_risk, 2))
    if o.target is not None:
        rr = abs(o.target - o.limit) / risk_unit
        nums["reward_risk"] = round(rr, 2)
        if rr < 1:
            warn.append(f"reward:risk is only {rr:.2f}")

    if loss > lim.max_loss_per_trade + 0.005:
        block.append(f"max loss {usd(loss)} (incl. {usd(extra)} slippage) is over your {usd(lim.max_loss_per_trade)} per trade")
    lost_today = max(0.0, -st.realized_today)
    if lost_today >= lim.max_daily_loss:
        block.append(f"today's loss {usd(lost_today)} has reached your daily limit {usd(lim.max_daily_loss)}: no new entries today")
    elif lost_today + st.open_risk + loss > lim.max_daily_loss + 0.005:
        block.append(f"today's loss {usd(lost_today)} + open risk {usd(st.open_risk)} + this trade {usd(loss)} "
                     f"would be over your daily limit {usd(lim.max_daily_loss)}")
    if -st.realized_month >= lim.max_monthly_loss:
        block.append(f"this month's loss {usd(-st.realized_month)} has reached your monthly limit {usd(lim.max_monthly_loss)}")
    if st.entries_today >= lim.max_trades_per_day:
        block.append(f"{st.entries_today} entries today: your limit is {lim.max_trades_per_day}")
    if st.consecutive_losses >= lim.max_consecutive_losses:
        block.append(f"{st.consecutive_losses} losses in a row today: your limit is {lim.max_consecutive_losses}. Stop for the day.")
    if len(st.open_positions) >= lim.max_open_positions:
        block.append(f"{len(st.open_positions)} position(s) open: your limit is {lim.max_open_positions}")
    if any(p.symbol == inst.symbol for p in st.open_positions):
        block.append(f"you already have an open {inst.symbol} position")
    in_use = st.open_value + st.working_value
    if in_use + value > lim.max_position_value + 0.005:
        block.append(f"position value {usd(value)} (plus {usd(in_use)} already in use) is over your cap {usd(lim.max_position_value)}")
    if value > st.capital + 0.005:
        block.append(f"position value {usd(value)} is more than your capital {usd(st.capital)} (no leverage on paper)")

    if price.last is None or price.age_seconds is None or price.age_seconds > STALE_PRICE:
        block.append("no recent price: the ticket cannot be priced fairly"
                     + (f" (the last one is {price.age_seconds / 60:.0f} minutes old)" if price.age_seconds is not None else ""))
    else:
        gap = (o.limit / price.last - 1) * 100
        nums["limit_vs_last_pct"] = round(gap, 3)
        if abs(gap) > FAIR_FILL_PCT:
            block.append(f"limit {o.limit} is {abs(gap):.2f}% from the last price {price.last}: too far to be a fair fill")
        if (long and price.last <= o.stop) or (not long and price.last >= o.stop):
            block.append(f"the last price {price.last} is already beyond the stop {o.stop}")
        if price.age_seconds > DELAYED_PRICE:
            warn.append(f"the price is about {price.age_seconds / 60:.0f} minutes old: fills happen on bars as they arrive")
    if price.source == "futures":
        warn.append("priced from COMEX futures (GC=F), not spot: the level differs from XAUUSD; the spread is assumed")
    warn.append("a jump past the stop (news, the daily open) can lose more than the max loss")
    warn.append("paper trading: fills are simulated from 1-minute bars with an assumed spread and slippage")
    return Verdict("BLOCK" if block else "PASS", block, warn, nums)

