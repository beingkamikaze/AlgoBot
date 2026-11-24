"""Paper trading for option structures (all legs enter and exit together).

Fills use NSE's option chain: legs you buy fill at the ask, legs you sell at the
bid. An open position is valued at what closing it would get you right now (long
legs at the bid, short legs at the ask), so the P&L shown is what you could
actually take.

Exit rules (set on the ticket, shown before you confirm):
  buyers (net debit)  stop when the position has lost `stop_pct` of the premium
                      (default 50%), target when it has gained `target_pct` (100%)
  sellers (net credit) stop when the loss reaches `stop_mult` x the credit (1x),
                      capped at the structure's max loss; target when `target_pct`
                      of the credit is captured (50%)
  every position      closed at your flat-by time (15:15); optional index levels
                      (from a signal) close it when the index crosses them
  managed (from a signal whose strategy version sets trade-management rules, app/core/manage.py):
                      the index stop moves to the index level at entry (breakeven) and trails, and a
                      time stop applies, all measured on the index; partial exits are not applied to
                      option structures (a structure enters and exits whole)
"""

from __future__ import annotations

import datetime as dt
import logging

from app.core import manage
from app.core.calendar import IST, market_status
from app.core.costs import options_charges
from app.db import db, eq
from app.options import chain as ch
from app.options import structures as S
from app.risk.engine import Limits, StructureRequest, check_structure

log = logging.getLogger("paper.options")
DEFAULT_EXITS = {"stop_pct": 0.5, "target_pct": 1.0, "stop_mult": 1.0, "credit_target_pct": 0.5}


def plan_exits(s: S.Structure, exits: dict | None = None) -> dict:
    e = {**DEFAULT_EXITS, **(exits or {})}
    qty = s.qty
    if s.net_premium < 0:                                   # you pay: risk a share of the premium
        debit = -s.net_premium
        planned = debit * qty * e["stop_pct"]
        out = {"type": "debit", "stop_pnl": -planned, "target_pnl": debit * qty * e["target_pct"]}
    else:                                                   # you receive: risk a multiple of the credit
        credit = s.net_premium
        planned = credit * qty * e["stop_mult"]
        if s.max_loss is not None:
            planned = min(planned, s.max_loss)
        out = {"type": "credit", "stop_pnl": -planned, "target_pnl": credit * qty * e["credit_target_pct"]}
    out.update(planned_loss=round(planned, 2), rules=e,
               underlying_stop=(exits or {}).get("underlying_stop"), underlying_target=(exits or {}).get("underlying_target"),
               side_hint=(exits or {}).get("side_hint"), manage=(exits or {}).get("manage"))
    out["stop_pnl"], out["target_pnl"] = round(out["stop_pnl"], 2), round(out["target_pnl"], 2)
    return out


def apply_exact(s: S.Structure) -> S.Structure:
    """Kite's basket margin (hedges count) and round-trip charges replace the estimates when you are logged in."""
    from app.risk import margin
    m = margin.for_basket([{"tradingsymbol": lg.tradingsymbol, "side": lg.side, "qty": lg.lots * s.lot_size,
                            "price": lg.price} for lg in s.legs])
    if m:
        s.margin = m["margin"]
        if m["charges"] is not None:
            s.charges_round_trip = m["charges"]
        s.margin_source = "Kite"
    return s


def request_for(s: S.Structure, exits: dict) -> StructureRequest:
    if s.margin_source != "Kite":
        apply_exact(s)
    return StructureRequest(
        label=s.label, underlying=s.underlying, expiry=s.expiry, lot_size=s.lot_size,
        legs=[{"kind": lg.kind, "strike": lg.strike, "side": lg.side, "lots": lg.lots, "price": lg.price,
               "spread_pct": lg.spread_pct, "oi": lg.oi} for lg in s.legs],
        max_loss=s.max_loss, planned_loss=exits["planned_loss"], charges=s.charges_round_trip, margin=s.margin,
        naked=s.naked, source=s.source, margin_source=s.margin_source)


def reprice(params: dict, now: dt.datetime | None = None) -> S.Structure:
    """The same strikes, priced from the chain right now."""
    st = params["structure"]
    c = ch.chain(st["underlying"], dt.date.fromisoformat(st["expiry"]))
    legs = [S._leg(c, lg["kind"], float(lg["strike"]), lg["side"], int(lg["lots"])) for lg in st["legs"]]
    return S.measure(c, st["template"], legs, now)


def check(params: dict, lim: Limits, state, now: dt.datetime) -> tuple[S.Structure, dict, dict]:
    s = reprice(params, now)
    exits = plan_exits(s, params.get("exit_overrides"))
    return s, exits, check_structure(request_for(s, exits), lim, state, now).to_dict()


def value_now(legs: list[dict], c: ch.Chain) -> float | None:
    """Per unit, what closing every leg gets you: long legs sold at the bid, short legs bought at the ask."""
    total = 0.0
    for lg in legs:
        q = c.quote(float(lg["strike"]), lg["kind"])
        if q is None:
            return None
        px = (q.bid if lg["side"] == "buy" else q.ask) or q.ltp
        if px is None:
            return None
        total += (1 if lg["side"] == "buy" else -1) * float(px) * int(lg["lots"])
    return total


def unrealized(ctx: dict, c: ch.Chain) -> float | None:
    v = value_now(ctx["legs"], c)
    if v is None:
        return None
    lots = max(int(lg["lots"]) for lg in ctx["legs"])
    return round((v + ctx["net_premium"] * lots) * ctx["lot_size"], 2)


def _charges(entry_legs: list[dict], exit_prices: dict[str, float], lot_size: int) -> float:
    buy = sell = 0.0
    for lg in entry_legs:
        q = int(lg["lots"]) * lot_size
        ex = exit_prices[lg["tradingsymbol"]]
        if lg["side"] == "buy":
            buy += float(lg["price"]) * q
            sell += ex * q
        else:
            sell += float(lg["price"]) * q
            buy += ex * q
    n = len(entry_legs)
    return options_charges(buy, sell, n, n).total


def fill_order(o: dict, now: dt.datetime) -> dict | None:
    """A confirmed option order: fill every leg at the chain's bid / ask now (market hours only)."""
    if not market_status(now).is_open:
        return None
    p = o["params"]
    s = apply_exact(reprice(p, now))
    exits = plan_exits(s, p.get("exit_overrides"))
    legs = [{"kind": lg.kind, "strike": lg.strike, "side": lg.side, "lots": lg.lots, "price": lg.price,
             "tradingsymbol": lg.tradingsymbol, "mid": lg.mid} for lg in s.legs]
    # what crossing the bid-ask spread cost at entry (fill vs mid); closing costs about the same again
    spread_cost = round(sum(abs(lg.price - (lg.mid or lg.price)) * lg.lots for lg in s.legs) * s.lot_size, 2)
    d = db()
    for lg in legs:
        d.insert("fills", {"order_id": o["id"], "user_id": o["user_id"], "ts": now.astimezone(dt.timezone.utc).isoformat(),
                           "qty": lg["lots"] * s.lot_size, "price": lg["price"], "costs": {"leg": lg["tradingsymbol"], "side": lg["side"]}})
    d.update("orders", {"id": eq(o["id"])}, {"state": "filled", "updated_at": now.astimezone(dt.timezone.utc).isoformat()})
    sig = d.select("signals", {"id": eq(o["signal_id"])}, columns="explanation") if o.get("signal_id") else []
    mg = start_management(exits, s.spot, int(now.timestamp()))
    trade = d.insert("trades", {
        "user_id": o["user_id"], "mode": "paper", "signal_id": o.get("signal_id"), "symbol": s.label,
        "side": "sell" if s.net_premium > 0 else "buy", "qty": s.lots, "entry_ts": now.astimezone(dt.timezone.utc).isoformat(),
        "entry": abs(s.net_premium), "rules_followed": not p.get("override"),
        "entry_context": {"kind": "option", "data_symbol": s.underlying, "underlying": s.underlying,
                          "expiry": s.expiry.isoformat(), "template": s.template, "lot_size": s.lot_size, "legs": legs,
                          "net_premium": s.net_premium, "max_loss": s.max_loss, "margin": s.margin, "naked": s.naked,
                          "exits": exits, "risk_amount": exits["planned_loss"], "source": s.source, "chain_as_of": s.as_of,
                          "strategy": p.get("strategy"), "strategy_version": p.get("strategy_version"), "override": p.get("override"), "summary": ((sig[0]["explanation"] if sig else {}) or {}).get("summary"),
                          "mae": 0, "mfe": 0, "spot_at_entry": s.spot, "spread_cost_entry": spread_cost,
                          "manage": mg, "initial_underlying_stop": exits.get("underlying_stop")}})[0]
    return trade


def start_management(exits: dict, spot: float, t: int) -> dict | None:
    """Management on the index levels: needs the rules, an index stop and the direction."""
    plan = exits.get("manage") or {}
    stop, hint = exits.get("underlying_stop"), exits.get("side_hint")
    if not plan.get("rules") or stop is None or hint not in ("buy", "sell") or not spot:
        return None
    rules = {**plan["rules"], "partial_at_r": 0}
    m = manage.start(rules, long=hint == "buy", entry=float(spot), stop=float(stop), qty=1, atr=plan.get("atr"),
                     entry_t=t, tick=0.05)
    if plan["rules"].get("partial_at_r", 0) > 0:
        m["notes"].append("partial exits are not applied to option structures")
    return m


def manage_trade(t: dict, now: dt.datetime, flat_by: dt.time, force_reason: str | None = None) -> dict | None:
    """Check an open option position; close it if a rule says so. Returns the update applied, if any."""
    ctx = dict(t["entry_context"])
    c = ch.chain(ctx["underlying"], dt.date.fromisoformat(ctx["expiry"]))
    pnl = unrealized(ctx, c)
    if pnl is None:
        return None
    ctx["mae"] = round(max(float(ctx.get("mae") or 0), -pnl), 2)
    ctx["mfe"] = round(max(float(ctx.get("mfe") or 0), pnl), 2)
    ex = ctx["exits"] = dict(ctx["exits"])
    m = ctx["manage"] = dict(ctx["manage"]) if ctx.get("manage") else None
    if m:
        ex["underlying_stop"] = m["stop"]                # the index stop in force (it may have moved)
    reason = force_reason
    hint = ex.get("side_hint")
    now_t = int(now.timestamp())
    if reason is None:
        if pnl <= ex["stop_pnl"]:
            reason = "stop"
        elif pnl >= ex["target_pnl"]:
            reason = "target"
        elif ex.get("underlying_stop") and hint and ((hint == "buy" and c.spot <= ex["underlying_stop"]) or
                                                      (hint == "sell" and c.spot >= ex["underlying_stop"])):
            reason = "index stop"
        elif ex.get("underlying_target") and hint and ((hint == "buy" and c.spot >= ex["underlying_target"]) or
                                                        (hint == "sell" and c.spot <= ex["underlying_target"])):
            reason = "index target"
        elif now.astimezone(IST).time() >= flat_by:
            reason = "time"
        elif m and manage.time_stop_due(m, c.spot, now_t):
            reason = "time-stop"
    d = db()
    if reason is None:
        if m:                                             # the index level so far moves the stop for later checks
            moves = manage.after_bar(m, c.spot, c.spot, now_t)
            ex["underlying_stop"] = m["stop"]
            if moves:
                from app.paper.service import audit      # imported here: service imports this module
                for mv in moves:
                    audit(t["user_id"], "system", "stop_moved", {"trade_id": t["id"], **mv})
        d.update("trades", {"id": eq(t["id"])}, {"entry_context": ctx, "mae": ctx["mae"], "mfe": ctx["mfe"]})
        return None
    exit_prices = {}
    for lg in ctx["legs"]:
        q = c.quote(float(lg["strike"]), lg["kind"])
        exit_prices[lg["tradingsymbol"]] = float((q.bid if lg["side"] == "buy" else q.ask) or q.ltp)
    charges = _charges(ctx["legs"], exit_prices, ctx["lot_size"])
    net = pnl - charges
    ctx["exit_prices"] = exit_prices
    ctx["spot_at_exit"] = c.spot
    planned = float(ctx.get("risk_amount") or 0)
    upd = {"exit_ts": now.astimezone(dt.timezone.utc).isoformat(), "exit": round(abs((value_now(ctx["legs"], c) or 0) /
           max(int(lg["lots"]) for lg in ctx["legs"])), 2), "gross_pnl": round(pnl, 2), "costs": charges,
           "net_pnl": round(net, 2), "r_multiple": round(net / planned, 3) if planned else None, "exit_reason": reason,
           "mae": ctx["mae"], "mfe": ctx["mfe"], "entry_context": ctx}
    d.update("trades", {"id": eq(t["id"])}, upd)
    return upd
