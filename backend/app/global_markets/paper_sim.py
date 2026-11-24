"""Paper fills for the global setup, on 1-minute mid-price bars (pure functions; `paper.py` stores the results).

The live sources give mid prices only, so the bid and ask are mid ∓ half an assumed spread. The rules follow the backtester:
  - an entry LIMIT fills on the first bar that STARTS at or after it was confirmed and whose ask (a long) or bid (a short)
    reaches the limit; a bar opening inside the limit fills at that open plus slippage, never worse than the limit;
  - it expires when the bars cover `expire_seconds` after it was confirmed without a fill, or the entry window has ended.
    Time is the bars' time, not the clock's, so a delayed feed does not expire orders early;
  - an open trade's stop watches the bid (a long) or the ask (a short); stop before target in the same bar; an open beyond
    the stop fills at the open (a gap); the target fills only if price trades through it, with no slippage;
  - the first bar starting at or after flat-by closes the trade at its open, with slippage;
  - a manual exit or the kill switch fills at the open of the first bar starting at or after it was asked for.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.market.models import Bar


@dataclass
class Fill:
    t: int
    price: float
    reason: str
    slippage: float = 0.0          # dollars an ounce paid beyond the spread


@dataclass(frozen=True)
class Quotes:
    spread: float
    slippage: float

    @property
    def half(self) -> float:
        return self.spread / 2


def _r(p: float) -> float:
    return round(p, 2)


def entry_fill(side: str, limit: float, placed_t: int, bars: list[Bar], q: Quotes) -> Fill | None:
    long = side == "buy"
    for b in bars:
        if b.t < placed_t or not b.complete:
            continue
        if long:
            ask_o, ask_l = b.o + q.half, b.l + q.half
            if ask_l <= limit:
                if ask_o <= limit:
                    price = min(ask_o + q.slippage, limit)
                    return Fill(b.t, _r(price), "entry", _r(price - ask_o))
                return Fill(b.t, _r(limit), "entry")
        else:
            bid_o, bid_h = b.o - q.half, b.h - q.half
            if bid_h >= limit:
                if bid_o >= limit:
                    price = max(bid_o - q.slippage, limit)
                    return Fill(b.t, _r(price), "entry", _r(bid_o - price))
                return Fill(b.t, _r(limit), "entry")
    return None


def entry_expired(placed_t: int, bars: list[Bar], expire_seconds: int, entry_end_t: int) -> str | None:
    """Why a working entry should expire, judged on the bars' own time; None while it may still fill."""
    done = [b for b in bars if b.complete]
    if not done:
        return None
    last_end = done[-1].t + 60
    if last_end >= placed_t + expire_seconds:
        return f"not filled within {expire_seconds // 60} minutes"
    if last_end > entry_end_t:
        return "the entry window ended"
    return None


@dataclass
class ExitCheck:
    fill: Fill | None
    mae: float                      # worst move against the trade, dollars an ounce (positive)
    mfe: float                      # best move in its favour
    last_t: int                     # last bar examined


def exit_check(side: str, entry: float, stop: float, target: float | None, after_t: int, bars: list[Bar], flat_by_t: int,
               q: Quotes, mae: float = 0.0, mfe: float = 0.0) -> ExitCheck:
    long = side == "buy"
    last_t = after_t
    for b in bars:
        if b.t <= after_t or not b.complete:
            continue
        last_t = b.t
        # the side you would close at: a long sells at the bid, a short buys at the ask
        o, h, lo = (b.o - q.half, b.h - q.half, b.l - q.half) if long else (b.o + q.half, b.h + q.half, b.l + q.half)
        if b.t >= flat_by_t:
            price = o - q.slippage if long else o + q.slippage
            return ExitCheck(Fill(b.t, _r(price), "time", q.slippage), mae, mfe, last_t)
        mae = max(mae, (entry - lo) if long else (h - entry))
        mfe = max(mfe, (h - entry) if long else (entry - lo))
        if long and lo <= stop:
            base = o if o < stop else stop
            return ExitCheck(Fill(b.t, _r(base - q.slippage), "stop", q.slippage), mae, mfe, last_t)
        if not long and h >= stop:
            base = o if o > stop else stop
            return ExitCheck(Fill(b.t, _r(base + q.slippage), "stop", q.slippage), mae, mfe, last_t)
        if target is not None and long and h > target:
            return ExitCheck(Fill(b.t, _r(target), "target"), mae, mfe, last_t)
        if target is not None and not long and lo < target:
            return ExitCheck(Fill(b.t, _r(target), "target"), mae, mfe, last_t)
    return ExitCheck(None, mae, mfe, last_t)


def exit_now(side: str, placed_t: int, bars: list[Bar], q: Quotes, reason: str) -> Fill | None:
    """A manual close: the open of the first bar starting at or after it was asked for, at the closing side, with slippage."""
    long = side == "buy"
    for b in bars:
        if b.t >= placed_t and b.complete:
            price = b.o - q.half - q.slippage if long else b.o + q.half + q.slippage
            return Fill(b.t, _r(price), reason, q.slippage)
    return None


def pnl(side: str, entry: float, exit_price: float, units: float, entry_slip: float, exit_slip: float, q: Quotes,
        commission: float = 0.0) -> dict:
    """Net is fill to fill; costs are what the spread, slippage and commission took; gross is net plus costs."""
    move = (exit_price - entry) if side == "buy" else (entry - exit_price)
    net = move * units - commission
    costs = units * (q.spread + entry_slip + exit_slip) + commission
    return {"net_pnl": round(net, 2), "costs": round(costs, 2), "gross_pnl": round(net + costs, 2)}
