"""Paper fill simulation on 1-minute bars (pure functions; the service persists results).

Rules, pessimistic like the backtester:
  - an entry LIMIT fills on the first bar AFTER it was placed whose range reaches
    the limit; if that bar opens past the limit in your favour, you get the open
    (plus slippage, capped at the limit);
  - an unfilled entry expires after `expire_minutes` or at the end of the entry window;
  - open positions: the stop is checked before the target in each bar; an open
    beyond the stop fills at the open; the target fills only if price trades through;
  - at flat_by (15:15) anything still open is closed at that bar's open;
  - trade management (app/core/manage.py), when the trade carries rules: the stop in force at the bar's
    open is checked first, then the partial, then the target; a bar moves the stop only for later bars
    (the fill bar never does: part of it came before the fill).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.core import manage
from app.core.calendar import IST
from app.market.models import Bar

SLIP_BPS = 2.0


def _slip(p: float, worse_up: bool) -> float:
    return p * (1 + SLIP_BPS / 1e4) if worse_up else p * (1 - SLIP_BPS / 1e4)


@dataclass
class Fill:
    t: int
    price: float
    reason: str


def entry_fill(side: str, limit: float, placed_t: int, bars: list[Bar]) -> Fill | None:
    long = side == "buy"
    for b in bars:
        if b.t < placed_t:          # the bar the order was placed in started earlier: we can't know its order of events
            continue
        if long and b.l <= limit:
            price = min(_slip(b.o, True), limit) if b.o <= limit else limit
            return Fill(b.t, round(price, 2), "entry")
        if not long and b.h >= limit:
            price = max(_slip(b.o, False), limit) if b.o >= limit else limit
            return Fill(b.t, round(price, 2), "entry")
    return None


def entry_expired(placed_t: int, now: dt.datetime, expire_minutes: int, entry_end: dt.time) -> bool:
    now = now.astimezone(IST)
    return now.timestamp() - placed_t > expire_minutes * 60 or now.time() > entry_end


@dataclass
class ExitCheck:
    fill: Fill | None
    mae: float              # worst excursion against the position, per share (positive number)
    mfe: float              # best excursion in favour, per share
    last_t: int             # last bar examined
    partial: dict | None = None     # a partial exit that filled in these bars
    moves: list = field(default_factory=list)   # stop moves made in these bars


def exit_check(side: str, entry: float, stop: float, target: float | None, after_t: int, bars: list[Bar],
               flat_by: dt.time, mae: float = 0.0, mfe: float = 0.0, m: dict | None = None) -> ExitCheck:
    """`m` is the trade's management state (manage.start); it is updated in place."""
    long = side == "buy"
    last_t = after_t
    partial, moves = None, []

    def done(fill):
        return ExitCheck(fill, mae, mfe, last_t, partial, moves)

    for b in bars:
        if b.t <= after_t:
            continue
        last_t = b.t
        local = dt.datetime.fromtimestamp(b.t, IST)
        if local.time() >= flat_by:
            return done(Fill(b.t, round(_slip(b.o, not long), 2), "time"))
        mae = max(mae, (entry - b.l) if long else (b.h - entry))
        mfe = max(mfe, (b.h - entry) if long else (entry - b.l))
        if m:
            stop = m["stop"]
        if long and b.l <= stop:
            base = b.o if b.o < stop else stop
            return done(Fill(b.t, round(_slip(base, False), 2), "stop"))
        if not long and b.h >= stop:
            base = b.o if b.o > stop else stop
            return done(Fill(b.t, round(_slip(base, True), 2), "stop"))
        if m and manage.partial_hit(m, b.h, b.l):
            partial = manage.book_partial(m, b.t)
        if target is not None and long and b.h > target:
            return done(Fill(b.t, round(target, 2), "target"))
        if target is not None and not long and b.l < target:
            return done(Fill(b.t, round(target, 2), "target"))
        if m and manage.time_stop_due(m, b.c, b.t + 60):
            return done(Fill(b.t, round(_slip(b.c, not long), 2), "time-stop"))
        if m and b.t > m["entry_t"]:
            moves += manage.after_bar(m, b.h, b.l, b.t + 60)
    return done(None)
