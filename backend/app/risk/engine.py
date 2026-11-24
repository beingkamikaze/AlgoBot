"""Risk engine (guide section 3.6): every order passes here, from any source.

Pure functions: an order, your limits, the account's state and the clock in,
PASS or BLOCK with reasons out. Ported from the nse-risk skill and extended.
Exits are never blocked by a limit: getting out must always be possible.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field

from app.core.calendar import IST, market_status
from app.core.costs import equity_intraday_charges, futures_charges, index_derivative_tick, tick_for


@dataclass
class Limits:
    max_loss_per_trade: float = 2000
    max_daily_loss: float = 6000
    max_monthly_loss: float = 30000
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    max_open_positions: int = 3
    max_margin_used_pct: float = 50
    entry_start: dt.time = dt.time(9, 30)
    entry_end: dt.time = dt.time(14, 45)
    flat_by: dt.time = dt.time(15, 15)
    max_loss_per_trade_fno: float | None = None     # futures and options; None = use max_loss_per_trade
    allow_naked_short_options: bool = False

    def max_loss_for(self, kind: str) -> float:
        if kind in ("future", "option") and self.max_loss_per_trade_fno:
            return self.max_loss_per_trade_fno
        return self.max_loss_per_trade

    @classmethod
    def from_row(cls, row: dict) -> "Limits":
        def t(v, default):
            return dt.time.fromisoformat(str(v)[:5]) if v else default
        d = cls()
        return cls(
            max_loss_per_trade=float(row.get("max_loss_per_trade") or d.max_loss_per_trade),
            max_daily_loss=float(row.get("max_daily_loss") or d.max_daily_loss),
            max_monthly_loss=float(row.get("max_monthly_loss") or d.max_monthly_loss),
            max_trades_per_day=int(row.get("max_trades_per_day") or d.max_trades_per_day),
            max_consecutive_losses=int(row.get("max_consecutive_losses") or d.max_consecutive_losses),
            max_open_positions=int(row.get("max_open_positions") or d.max_open_positions),
            max_margin_used_pct=float(row.get("max_margin_used_pct") or d.max_margin_used_pct),
            entry_start=t(row.get("entry_start"), d.entry_start),
            entry_end=t(row.get("entry_end"), d.entry_end),
            flat_by=t(row.get("flat_by"), d.flat_by),
            max_loss_per_trade_fno=float(row["max_loss_per_trade_fno"]) if row.get("max_loss_per_trade_fno") else None,
            allow_naked_short_options=bool(row.get("allow_naked_short_options")),
        )


@dataclass
class OpenPosition:
    symbol: str
    side: str
    qty: int
    entry: float
    stop: float


@dataclass
class AccountState:
    capital: float                       # starting capital + all realized P&L
    cash_free: float                     # capital minus money tied up in open positions and working orders
    open_positions: list[OpenPosition] = field(default_factory=list)
    entries_today: int = 0
    realized_today: float = 0.0          # net, after charges
    realized_month: float = 0.0
    consecutive_losses: int = 0          # losing trades in a row, today
    kill_switch: bool = False

    @property
    def open_risk(self) -> float:
        return sum(p.qty * abs(p.entry - p.stop) for p in self.open_positions)


@dataclass
class OrderRequest:
    symbol: str
    side: str                            # "buy" or "sell"
    qty: int
    limit: float
    stop: float | None = None
    target: float | None = None
    leg: str = "entry"                   # "entry" or "exit"
    kind: str = "equity"                 # "equity" or "future"
    lot_size: int = 1
    margin_pct: float = 100.0            # cash blocked as % of the position value
    index: bool = True                   # futures: index (index-derivative ticks) or stock (cash-market ticks)
    margin_exact: float | None = None    # Kite's margin API (app.risk.margin), when logged in: replaces the estimate
    charges_exact: float | None = None   # Kite's round-trip charges at the stop, when logged in
    live_prices: bool = False            # the last price came from Kite (real time), not Yahoo (delayed)

    def tick(self, price: float) -> float:
        return index_derivative_tick(price) if self.kind == "future" and self.index else tick_for(price)

    def charges(self, buy_value: float, sell_value: float) -> float:
        fn = futures_charges if self.kind == "future" else equity_intraday_charges
        return fn(buy_value, sell_value).total


# Blocks no override can lift, even on paper: the order is malformed, a fair fill cannot be simulated, the setup has
# already failed, or it is your own emergency brake. Everything else is one of YOUR limits, which a paper
# override may set aside (recorded as "rules not followed").
HARD_BLOCKS = ("side must be", "quantity must be", "a limit price is required", "is not on the NSE tick", "no open ",
               "exit of ", "market closed", "the kill switch is on", "a stop is required", "is on the wrong side",
               "no legs", "lots must be a whole number", "too far to be a fair fill", "is already beyond the stop")


def is_hard(reason: str) -> bool:
    return any(h in reason for h in HARD_BLOCKS)


@dataclass
class Verdict:
    verdict: str                         # "PASS" or "BLOCK"
    reasons: list[str]
    warnings: list[str]
    numbers: dict

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hard"] = [r for r in self.reasons if is_hard(r)]
        d["overridable"] = self.verdict == "BLOCK" and not d["hard"]     # paper only: your limits, not hard blocks
        return d


def _money(x: float) -> str:
    return f"₹{x:,.0f}"


def check(o: OrderRequest, lim: Limits, st: AccountState, now: dt.datetime, last_price: float | None) -> Verdict:
    block: list[str] = []
    warn: list[str] = []
    nums: dict = {}
    now = now.astimezone(IST)
    long = o.side == "buy"

    # ---- format, for every order
    if o.side not in ("buy", "sell"):
        block.append("side must be buy or sell")
    if not isinstance(o.qty, int) or o.qty < 1:
        block.append("quantity must be a whole number of shares, 1 or more")
    if o.limit is None or o.limit <= 0:
        block.append("a limit price is required (no market orders)")
    elif abs(o.limit / o.tick(o.limit) - round(o.limit / o.tick(o.limit))) > 1e-6:
        block.append(f"limit {o.limit} is not on the NSE tick ({o.tick(o.limit)})")
    if isinstance(o.qty, int) and o.qty >= 1 and o.qty % o.lot_size:
        block.append(f"quantity must be whole lots of {o.lot_size}")
    if block:
        return Verdict("BLOCK", block, warn, nums)

    if o.leg == "exit":
        held = next((p for p in st.open_positions if p.symbol == o.symbol), None)
        if held is None:
            block.append(f"no open {o.symbol} position to exit")
        elif o.qty > held.qty:
            block.append(f"exit of {o.qty} is more than the {held.qty} held")
        m = market_status(now)
        if not m.is_open:
            warn.append(f"the market is {m.phase}: the exit waits for the next session")
        return Verdict("BLOCK" if block else "PASS", block, warn, nums)

    # ---- entries
    m = market_status(now)
    if not m.is_open:
        block.append(f"market closed ({m.reason or m.phase})")
    elif not (lim.entry_start <= now.time() <= lim.entry_end):
        block.append(f"outside your entry window {lim.entry_start:%H:%M}–{lim.entry_end:%H:%M} IST")
    if st.kill_switch:
        block.append("the kill switch is on for today")

    if o.stop is None:
        block.append("a stop is required on every entry")
        return Verdict("BLOCK", block, warn, nums)
    if (long and o.stop >= o.limit) or (not long and o.stop <= o.limit):
        block.append(f"the stop {o.stop} is on the wrong side of the entry {o.limit}")
        return Verdict("BLOCK", block, warn, nums)
    if o.target is not None and ((long and o.target <= o.limit) or (not long and o.target >= o.limit)):
        block.append(f"the target {o.target} is on the wrong side of the entry {o.limit}")

    risk_share = abs(o.limit - o.stop)
    value = o.qty * o.limit
    # stocks always block their full value (no leverage on stocks, whatever the broker allows); F&O use Kite's margin
    margin = o.margin_exact if o.margin_exact is not None and o.kind != "equity" else value * o.margin_pct / 100
    exit_value = o.qty * o.stop
    charges = o.charges_exact if o.charges_exact is not None else \
        (o.charges(value, exit_value) if long else o.charges(exit_value, value))
    loss = o.qty * risk_share + charges
    nums.update(value=round(value, 2), margin=round(margin, 2), risk_per_share=round(risk_share, 2), max_loss=round(loss, 2),
                charges_at_stop=charges, open_risk_before=round(st.open_risk, 2),
                margin_source="Kite" if o.margin_exact is not None and o.kind != "equity" else
                ("full value" if o.kind == "equity" else "estimate"),
                charges_source="Kite" if o.charges_exact is not None else "estimate")
    if o.kind == "future":
        nums["lots"] = o.qty // o.lot_size
        warn.append(f"futures are leveraged: a {value / 1e5:,.1f} lakh position on about {margin / 1e5:,.1f} lakh margin")
    if o.target is not None:
        rr = abs(o.target - o.limit) / risk_share
        nums["reward_risk"] = round(rr, 2)
        if rr < 1:
            warn.append(f"reward:risk is only {rr:.2f}")

    cap_loss = lim.max_loss_for(o.kind)
    if loss > cap_loss + 0.5:
        which = "F&O " if o.kind != "equity" and lim.max_loss_per_trade_fno else ""
        block.append(f"max loss {_money(loss)} (incl. {_money(charges)} charges) is over your {which}{_money(cap_loss)} per trade")
    lost_today = max(0.0, -st.realized_today)
    if lost_today >= lim.max_daily_loss:
        block.append(f"today's loss {_money(lost_today)} has reached your daily limit {_money(lim.max_daily_loss)}: no new entries today")
    elif lost_today + st.open_risk + loss > lim.max_daily_loss + 0.5:
        block.append(f"today's loss {_money(lost_today)} + open risk {_money(st.open_risk)} + this trade {_money(loss)} "
                     f"would be over your daily limit {_money(lim.max_daily_loss)}")
    if -st.realized_month >= lim.max_monthly_loss:
        block.append(f"this month's loss {_money(-st.realized_month)} has reached your monthly limit {_money(lim.max_monthly_loss)}")
    if st.entries_today >= lim.max_trades_per_day:
        block.append(f"{st.entries_today} entries today: your limit is {lim.max_trades_per_day}")
    if st.consecutive_losses >= lim.max_consecutive_losses:
        block.append(f"{st.consecutive_losses} losses in a row today: your limit is {lim.max_consecutive_losses}. Stop for the day.")
    if len(st.open_positions) >= lim.max_open_positions:
        block.append(f"{len(st.open_positions)} positions open: your limit is {lim.max_open_positions}")
    if any(p.symbol == o.symbol for p in st.open_positions):
        block.append(f"you already have an open {o.symbol} position")
    need = margin                                   # stocks: the full value (no leverage); futures: the margin
    if need > st.cash_free + 0.5:
        what = "margin" if o.kind == "future" else "position"
        block.append(f"{what} {_money(need)} is more than your free cash {_money(st.cash_free)}"
                     + (" (no leverage on stocks)" if o.kind == "equity" else ""))
    cap = st.capital * lim.max_margin_used_pct / 100
    used = st.capital - st.cash_free
    if used + need > cap + 0.5:
        block.append(f"capital in use would be {_money(used + need)}, over your {lim.max_margin_used_pct:g}% cap ({_money(cap)})")

    if last_price:
        gap = (o.limit / last_price - 1) * 100
        nums["limit_vs_last_pct"] = round(gap, 2)
        if (long and gap > 1) or (not long and gap < -1):
            block.append(f"limit {o.limit} is {abs(gap):.1f}% {'above' if long else 'below'} the last price {last_price}: too far to be a fair fill")
        elif (long and gap < -0.5) or (not long and gap > 0.5):
            warn.append(f"limit is {abs(gap):.1f}% away from the last price {last_price}: it may not fill")
        if (long and last_price <= o.stop) or (not long and last_price >= o.stop):
            block.append(f"the last price {last_price} is already beyond the stop {o.stop}")
    else:
        warn.append("no recent price to compare the limit with")
    warn.append("paper trading: fills are simulated on Kite's real-time prices" if o.live_prices else
                "paper trading: fills are simulated on free, delayed data (log in to Kite for real-time prices)")
    return Verdict("BLOCK" if block else "PASS", block, warn, nums)


# ---------------------------------------------------------------- option structures

EXPIRY_CUTOFF = dt.time(14, 30)      # no new option positions on expiry day after this: gamma is too fast
MAX_SPREAD_PCT = 10.0                 # a leg whose bid-ask spread is wider than this % of its price is illiquid


@dataclass
class StructureRequest:
    label: str                        # "NIFTY 27OCT IRON CONDOR"
    underlying: str
    expiry: dt.date
    legs: list[dict]                  # {kind, strike, side, lots, price, spread_pct, oi}
    lot_size: int
    max_loss: float | None            # rupees for the whole position at the planned stop or at expiry; None = unlimited
    planned_loss: float               # what the exit rules risk (stop), rupees, before charges
    charges: float                    # round trip
    margin: float
    naked: bool
    source: str                       # chain source: "kite", "nse" or "modeled"
    margin_source: str = "estimate"   # "Kite" when the margin came from Kite's basket-margin API


def check_structure(o: StructureRequest, lim: Limits, st: AccountState, now: dt.datetime) -> Verdict:
    """PASS or BLOCK for a new option position (one or more legs entered together)."""
    block: list[str] = []
    warn: list[str] = []
    now = now.astimezone(IST)
    m = market_status(now)
    if not m.is_open:
        block.append(f"market closed ({m.reason or m.phase})")
    elif not (lim.entry_start <= now.time() <= lim.entry_end):
        block.append(f"outside your entry window {lim.entry_start:%H:%M}–{lim.entry_end:%H:%M} IST")
    if st.kill_switch:
        block.append("the kill switch is on for today")
    if not o.legs:
        block.append("no legs")
    for lg in o.legs:
        if not isinstance(lg.get("lots"), int) or lg["lots"] < 1:
            block.append(f"{lg.get('strike')} {lg.get('kind')}: lots must be a whole number, 1 or more")
        sp = lg.get("spread_pct")
        if sp is not None and sp > MAX_SPREAD_PCT:
            block.append(f"{lg['strike']:g} {lg['kind']} is illiquid: bid-ask spread {sp:.1f}% of its price (limit {MAX_SPREAD_PCT:g}%)")
        if o.source in ("nse", "kite") and not lg.get("oi"):
            warn.append(f"{lg['strike']:g} {lg['kind']} shows no open interest")
    if o.naked:
        if not lim.allow_naked_short_options:
            block.append("naked short options (unlimited loss) are switched off in your limits; use a spread or "
                         "an iron condor / fly, or enable them yourself")
        else:
            warn.append("naked short options: the loss is not capped; your stop is the only limit")
    if o.expiry == now.date() and now.time() > EXPIRY_CUTOFF:
        block.append(f"expiry day after {EXPIRY_CUTOFF:%H:%M}: no new option positions")
    elif o.expiry == now.date():
        warn.append("expiry day: option prices can move very fast near the close")
    loss = o.planned_loss + o.charges
    cap = lim.max_loss_for("option")
    which = "F&O " if lim.max_loss_per_trade_fno else ""
    if loss > cap + 0.5:
        block.append(f"max loss {_money(loss)} (incl. {_money(o.charges)} charges) is over your {which}{_money(cap)} per trade")
    if o.max_loss is not None and o.max_loss + o.charges > loss + 0.5:
        warn.append(f"the stop limits the loss to {_money(loss)}, but a gap past the stop could lose up to {_money(o.max_loss + o.charges)}")
    lost_today = max(0.0, -st.realized_today)
    if lost_today >= lim.max_daily_loss:
        block.append(f"today's loss {_money(lost_today)} has reached your daily limit {_money(lim.max_daily_loss)}")
    elif lost_today + st.open_risk + loss > lim.max_daily_loss + 0.5:
        block.append(f"today's loss {_money(lost_today)} + open risk {_money(st.open_risk)} + this position {_money(loss)} "
                     f"would be over your daily limit {_money(lim.max_daily_loss)}")
    if -st.realized_month >= lim.max_monthly_loss:
        block.append(f"this month's loss has reached your monthly limit {_money(lim.max_monthly_loss)}")
    if st.entries_today >= lim.max_trades_per_day:
        block.append(f"{st.entries_today} entries today: your limit is {lim.max_trades_per_day}")
    if st.consecutive_losses >= lim.max_consecutive_losses:
        block.append(f"{st.consecutive_losses} losses in a row today: your limit is {lim.max_consecutive_losses}")
    if len(st.open_positions) >= lim.max_open_positions:
        block.append(f"{len(st.open_positions)} positions open: your limit is {lim.max_open_positions}")
    if o.margin > st.cash_free + 0.5:
        block.append(f"margin {_money(o.margin)} is more than your free cash {_money(st.cash_free)}")
    cap_used = st.capital * lim.max_margin_used_pct / 100
    if st.capital - st.cash_free + o.margin > cap_used + 0.5:
        block.append(f"capital in use would be over your {lim.max_margin_used_pct:g}% cap ({_money(cap_used)})")
    if o.source == "modeled":
        warn.append("prices are MODELED from India VIX, not NSE quotes: fills will differ")
    warn.append("paper trading: fills at Kite's real-time bid and ask" if o.source == "kite" else
                "paper trading: fills at NSE's delayed bid and ask")
    nums = {"planned_loss": round(o.planned_loss, 2), "max_loss_at_expiry": o.max_loss, "charges": round(o.charges, 2),
            "max_loss": round(loss, 2), "margin": round(o.margin, 2), "margin_source": o.margin_source, "lots": max((lg["lots"] for lg in o.legs), default=0)}
    return Verdict("BLOCK" if block else "PASS", block, warn, nums)
