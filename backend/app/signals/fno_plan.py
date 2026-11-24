"""A signal on a stock or an index, expressed in futures and options: the exact contract, what it costs,
and what it is estimated to make or lose at the signal's own stop and target.

  futures  one lot of the current month's contract (rolls 2 trading days before expiry)
  option   BUY the at-the-money CALL for a buy signal, the at-the-money PUT for a sell signal
           (a defined-risk way to take the direction: the most you can lose is the premium)
           expiry: the nearest one more than MIN_EXPIRY_DAYS days away (an option that close to expiry loses
           value too fast to buy), weekly for Nifty, monthly for Bank Nifty and stocks

The option estimates reprice the contract with Black-76 at the signal's stop and target levels, with today's
implied volatility and time left, then take off half the bid-ask spread on exit. They are estimates: a big
volatility change moves the premium on its own.

Nothing here places anything: each plan carries the exact request for a paper ticket, and the risk engine
checks that ticket like any other.
"""

from __future__ import annotations

import datetime as dt

from app.core.calendar import IST
from app.core.costs import options_charges
from app.core.instruments import INDEX_FUTURES, resolve
from app.market import fno
from app.options import chain as ch
from app.options import pricing
from app.options import structures as S
from app.paper.options import DEFAULT_EXITS
from app.risk.engine import Limits

ILLIQUID_SPREAD_PCT = 10.0       # the risk engine blocks option legs with a wider spread
THIN_OI = 500                    # open interest below this: you may not get out at a fair price
MIN_EXPIRY_DAYS = 3              # skip expiries 0-2 calendar days away when buying


def underlying_of(symbol: str) -> str:
    return symbol.upper().removesuffix("-FUT")


def available(symbol: str) -> bool:
    u = underlying_of(symbol)
    return u in INDEX_FUTURES or fno.is_fno(u)


def _future(u: str, side: str, entry: float, stop: float, target: float | None, lim: Limits | None, today: dt.date) -> dict:
    inst = resolve(f"{u}-FUT", today)
    qty, long = inst.lot_size, side == "buy"
    stop_v, entry_v = stop * qty, entry * qty
    loss = abs(entry - stop) * qty + inst.charges(*((entry_v, stop_v) if long else (stop_v, entry_v))).total
    gain = None
    if target is not None:
        tgt_v = target * qty
        gain = abs(target - entry) * qty - inst.charges(*((entry_v, tgt_v) if long else (tgt_v, entry_v))).total
    cap = lim.max_loss_for("future") if lim else None
    return {
        "contract": inst.tradingsymbol, "symbol": inst.symbol, "expiry": inst.expiry.isoformat() if inst.expiry else None,
        "lot_size": qty, "lots": 1, "side": side, "notional": round(entry * qty, 2), "margin": round(inst.margin(entry, qty), 2),
        "loss_at_stop": round(loss, 2), "gain_at_target": round(gain, 2) if gain is not None else None,
        "fits_limit": None if cap is None else loss <= cap, "limit": cap,
        "price_note": f"priced from {'the index' if inst.is_index else 'the stock'} until Kite data is connected: the futures "
                      "price differs by the basis",
        "ticket": {"symbol": inst.symbol, "side": side, "stop": stop, "target": target},
    }


def _option(u: str, side: str, stop: float, target: float | None, lim: Limits | None, now: dt.datetime) -> dict:
    kind, template = ("CE", "long_call") if side == "buy" else ("PE", "long_put")
    exp = ch.nearest_expiry(u, now.date(), min_days=MIN_EXPIRY_DAYS)
    c = ch.chain(u, exp, allow_modeled=False)
    s = S.build(c, template, {"lots": 1, "offset": 0}, now)
    leg = s.legs[0]
    qty = s.qty
    t = c.years_left(now)
    fwd_ratio = c.forward(now) / c.spot if c.spot else 1.0
    iv = pricing.implied_vol(kind, leg.mid or leg.price, c.spot, leg.strike, t, fwd=c.forward(now)) or ((leg.iv or 0) / 100) or None
    half_spread = ((leg.price - leg.mid) if leg.mid else 0.0)

    def value_at(level: float) -> float | None:
        if not iv:
            return None
        p = pricing.black76(kind, level, leg.strike, t, iv, fwd=level * fwd_ratio).price
        return max(p - half_spread, 0.0)                    # you sell at about the bid

    cost = leg.price * qty
    at_stop, at_target = value_at(stop), (value_at(target) if target is not None else None)

    def pnl(exit_price: float | None) -> float | None:
        if exit_price is None:
            return None
        return round((exit_price - leg.price) * qty - options_charges(cost, exit_price * qty).total, 2)

    planned = leg.price * qty * DEFAULT_EXITS["stop_pct"]          # the paper ticket's premium stop (50% of what you pay)
    cap = lim.max_loss_for("option") if lim else None
    warnings = []
    if (leg.spread_pct or 0) > ILLIQUID_SPREAD_PCT:
        warnings.append(f"illiquid: the bid-ask spread is {leg.spread_pct}% of the price (the risk check blocks above {ILLIQUID_SPREAD_PCT:.0f}%)")
    if (leg.oi or 0) < THIN_OI:
        warnings.append(f"thin open interest ({leg.oi or 0}): getting out at a fair price may be hard")
    failed = (side == "buy" and c.spot <= stop) or (side == "sell" and c.spot >= stop)
    if failed:
        warnings.append(f"{u} is already beyond the signal's stop ({c.spot:,.2f} vs {stop:,.2f}): the setup has failed")
    days = (exp - now.date()).days
    if days <= 2:
        warnings.append(f"expiry is in {days} day{'s' if days != 1 else ''}: the premium loses value quickly")
    return {
        "contract": leg.tradingsymbol, "kind": kind, "strike": leg.strike, "expiry": exp.isoformat(), "days_to_expiry": days,
        "setup_failed": failed,
        "action": f"BUY {leg.tradingsymbol}", "lot_size": c.lot_size, "lots": 1, "qty": qty,
        "bid": c.quote(leg.strike, kind).bid if c.quote(leg.strike, kind) else None, "ask": leg.price, "mid": leg.mid,
        "spread_pct": leg.spread_pct, "oi": leg.oi, "iv": round(iv * 100, 2) if iv else None,
        "delta": round(s.greeks.get("delta", 0) / qty, 3) if qty else None, "theta_per_day": s.greeks.get("theta"),
        "spot": c.spot, "as_of": c.as_of, "source": c.source,
        "cost": round(cost, 2), "max_loss": round(cost + s.charges_round_trip, 2), "planned_loss": round(planned, 2),
        "est_at_stop": None if failed or at_stop is None else round(at_stop, 2), "pnl_at_stop": None if failed else pnl(at_stop),
        "est_at_target": round(at_target, 2) if at_target is not None else None, "pnl_at_target": pnl(at_target),
        "fits_limit": None if cap is None else planned <= cap, "limit": cap, "warnings": warnings,
        "ticket": {"underlying": u, "expiry": exp.isoformat(), "template": template, "params": {"lots": 1, "offset": 0},
                   "exits": {"underlying_stop": stop, "underlying_target": target, "side_hint": side}},
    }


def plan(symbol: str, side: str, entry: float, stop: float, target: float | None, lim: Limits | None = None,
         now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(IST)
    u = underlying_of(symbol)
    if not available(symbol):
        return {"underlying": u, "available": False,
                "reason": f"{u} has no futures or options on NSE: only the shares can be traded"}
    out: dict = {"underlying": u, "available": True, "is_index": u in INDEX_FUTURES, "side": side,
                 "levels": {"entry": entry, "stop": stop, "target": target}}
    try:
        out["future"] = _future(u, side, entry, stop, target, lim, now.date())
    except ValueError as e:
        out["future_error"] = str(e)
    try:
        out["option"] = _option(u, side, stop, target, lim, now)
        out["spot"] = out["option"]["spot"]
        out["setup_failed"] = out["option"]["setup_failed"]
    except (ch.ChainError, ValueError) as e:
        out["option_error"] = f"option chain unavailable: {e}"
        out["setup_failed"] = False
    out["note"] = ("The confidence and evidence on the card are for the " + ("index" if out["is_index"] else "stock") +
                   " signal. Futures follow the underlying closely but are leveraged; an option also loses to time decay and "
                   "the bid-ask spread, so its result differs from the signal's.")
    return out
