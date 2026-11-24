"""Live orders through Kite: the same ticket as paper, a second confirmation, then real orders.

Flow for one trade (stocks MIS on NSE, index futures MIS on NFO; options stay paper for now):
  preview   your ticket re-sized with your LIVE limits and real Kite funds; risk check; F&O margin from Kite
  approve   every gate again (server switch, Kite login today, two-factor, armed today, market open), the
            quantity must equal the preview's, then a LIMIT entry tagged with our order id
  sync      every few seconds: entry filled -> a stop-loss (SL) order goes in at once. If the stop is refused,
            the position is closed: a position is never left unprotected.
            Target reached, 15:15, "close" or the kill switch -> that same stop order is modified into a
            marketable LIMIT, so there is only ever ONE exit order per position (never two sells that could
            leave you short).
  manage    when the ticket carries trade-management rules (app/core/manage.py, the same functions the
            backtest and paper run), they were confirmed with the entry and run without asking again. Kite's
            last price is folded into 1-minute bars; at each finished minute the stop may move (breakeven,
            ATR trail) and the time stop is checked. A stop move MODIFIES the resting SL order (a refused
            modify leaves the old stop in place, so the position is never unprotected). A partial exit first
            shrinks the SL order's quantity, then sends a marketable LIMIT for the partial: the exit orders
            together never exceed the position. A time stop exits like a target. Every move is recorded in the
            trade's entry_context["manage"]["moves"], which the monitor shows and alerts on.
  reconcile Kite's MIS positions vs ours; any difference is raised as an alert. It never trades by itself
            on a mismatch, except to close a position of ours that Zerodha already squared off.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from app.core import manage
from app.core.calendar import IST
from app.core.costs import round_to_tick
from app.core.instruments import resolve
from app.db import db, eq
from app.live import gates as G
from app.live import session
from app.live.broker import Broker, BrokerError, BrokerOrder, tag_for
from app.paper import service as paper
from app.risk.engine import Limits, OrderRequest, check

log = logging.getLogger("live")
ENTRY_EXPIRE_SECONDS = 600            # an unfilled entry is cancelled after 10 minutes, like paper
SENDING_TIMEOUT = 60                  # an order we could not confirm is looked up by its tag for this long
STOP_LIMIT_PCT = 0.5                  # an SL order's limit sits this far beyond its trigger, so it fills
EXIT_LIMIT_PCT = 0.5                  # a marketable exit LIMIT sits this far through the last price
REPRICE_SECONDS = 20                  # an exit that has not filled is moved to the new price this often
_lock = threading.RLock()
_instruments: dict[str, tuple[str, dict]] = {}       # exchange -> (date, {tradingsymbol: row})
_last_diffs: dict[str, list] = {}


class LiveOrderError(Exception):
    pass


def _now() -> dt.datetime:
    return paper._now()


def _iso(t) -> str:
    return paper._iso(t)


def exchange_for(kind: str) -> str:
    if kind == "equity":
        return "NSE"
    if kind == "future":
        return "NFO"
    raise LiveOrderError("option structures are paper only for now: live options come after stocks and futures run clean")


def live_limits(row: dict) -> Limits:
    """Your paper limits with the live caps on top: live starts much smaller."""
    lim = Limits.from_row(row)
    live_loss = float(row.get("live_max_loss_per_trade") or 500)
    lim.max_loss_per_trade = min(lim.max_loss_per_trade, live_loss)
    lim.max_loss_per_trade_fno = live_loss
    lim.max_daily_loss = min(lim.max_daily_loss, float(row.get("live_max_daily_loss") or 1500))
    lim.max_trades_per_day = min(lim.max_trades_per_day, int(row.get("live_max_trades_per_day") or 3))
    lim.allow_naked_short_options = False
    return lim


def instrument(b: Broker, exchange: str, tradingsymbol: str, now: dt.datetime) -> dict | None:
    """Kite's instrument row (lot size, tick), downloaded once a day."""
    day = now.astimezone(IST).date().isoformat()
    hit = _instruments.get(exchange)
    if not hit or hit[0] != day:
        rows = b.instruments(exchange)
        _instruments[exchange] = (day, {r["tradingsymbol"]: r for r in rows})
        hit = _instruments[exchange]
    return hit[1].get(tradingsymbol)


# ---------------------------------------------------------------- preview and approve

def preview(user_id: str, order_id: str, now: dt.datetime | None = None) -> dict:
    """What the ticket becomes with real money: live size, risk verdict, Kite margin. Places nothing."""
    now = now or _now()
    d = db()
    rows = d.select("orders", {"id": eq(order_id), "user_id": eq(user_id)})
    if not rows or rows[0]["state"] != "proposed":
        raise LiveOrderError("this ticket is not waiting for confirmation")
    p = rows[0]["params"]
    exch = exchange_for(p.get("kind", "equity"))
    b = session.broker_for(user_id, now)
    row = G.live_limits_row(user_id)
    lim = live_limits(row)
    acct = d.select("broker_accounts", {"user_id": eq(user_id), "broker": eq("zerodha")}, limit=1)[0]
    st, _ = paper.account_state(user_id, acct, now, mode="live")
    funds = b.funds()
    st.capital, st.cash_free = funds["net"], funds["available"]
    inst = resolve(p.get("app_symbol") or p["symbol"], now.date())
    k_inst = instrument(b, exch, p["symbol"], now)
    problems = []
    if k_inst is None:
        problems.append(f"Kite has no {exch}:{p['symbol']} today")
    elif int(k_inst.get("lot_size") or 1) != inst.lot_size:
        problems.append(f"lot size changed: Kite says {k_inst['lot_size']}, the app assumed {inst.lot_size}")
    last = b.ltp([f"{exch}:{p['symbol']}"]).get(f"{exch}:{p['symbol']}")
    long = p["side"] == "buy"
    limit = float(p["limit"])
    if last:
        limit = round_to_tick(last * (1 + (paper.ENTRY_BUFFER_PCT if long else -paper.ENTRY_BUFFER_PCT) / 100), inst.tick(last), up=long)
    qty = paper.size_for(limit, float(p["stop"]), lim, st, inst)
    cap = float(row.get("live_max_order_value") or 50000)
    while qty > 0 and qty * limit * inst.margin_pct / 100 > cap:
        qty -= inst.lot_size
    margin = None
    exact: dict = {}
    if qty > 0:
        from app.risk import margin as M
        m = M.for_order(exch, p["symbol"], p["side"], qty, limit, float(p["stop"]))     # exact margin + round-trip charges
        if m:
            exact = {"charges_exact": m["charges"], **({"margin_exact": m["margin"]} if exch == "NFO" else {})}
            margin = m["margin"] if exch == "NFO" else None
        elif exch == "NFO":
            margin = b.order_margin(exch, p["symbol"], p["side"], qty, limit)
            exact = {"margin_exact": margin}
        if margin is not None and margin > funds["available"]:
            problems.append(f"Kite asks for ₹{margin:,.0f} margin; ₹{funds['available']:,.0f} is free")
    if qty <= 0:
        unit = f"one lot of {inst.lot_size}" if inst.kind == "future" else "one share"
        v = {"verdict": "BLOCK", "reasons": [f"with your live max loss (₹{lim.max_loss_for(inst.kind):,.0f}) and order cap "
                                             f"(₹{cap:,.0f}), {unit} does not fit"], "warnings": [], "numbers": {}}
    else:
        v = check(OrderRequest(p["symbol"], p["side"], qty, limit, float(p["stop"]), p.get("target"), kind=inst.kind,
                               lot_size=inst.lot_size, margin_pct=inst.margin_pct, index=inst.is_index, live_prices=True,
                               **exact), lim, st, now, last).to_dict()
    if problems:
        v = {**v, "verdict": "BLOCK", "reasons": v["reasons"] + problems}
    return {"order_id": order_id, "exchange": exch, "symbol": p["symbol"], "side": p["side"], "qty": qty, "limit": limit,
            "stop": p["stop"], "target": p.get("target"), "last_price": last, "funds": funds, "margin": margin,
            "margin_source": "Kite" if margin is not None else "estimate",
            "live_limits": {"max_loss_per_trade": lim.max_loss_for(inst.kind), "max_daily_loss": lim.max_daily_loss,
                            "max_trades_per_day": lim.max_trades_per_day, "max_order_value": cap},
            "management": manage.describe((p.get("manage") or {}).get("rules")), "verdict": v}


def approve(user_id: str, order_id: str, expected_qty: int, aal: str | None, now: dt.datetime | None = None) -> dict:
    """Your second, real-money confirmation. Every gate and the risk check run again here."""
    now = now or _now()
    with _lock:
        gs = G.check(user_id, aal, now)
        if not G.all_ok(gs):
            raise LiveOrderError("live trading is not allowed right now: " + "; ".join(G.blocking(gs)))
        pv = preview(user_id, order_id, now)
        if pv["verdict"]["verdict"] != "PASS":
            raise LiveOrderError("the risk check blocks it: " + "; ".join(pv["verdict"]["reasons"]))
        if pv["qty"] != expected_qty:
            raise LiveOrderError(f"the live size changed from {expected_qty} to {pv['qty']}: check the new preview and confirm again")
        d = db()
        o = d.select("orders", {"id": eq(order_id)})[0]
        acct = d.select("broker_accounts", {"user_id": eq(user_id), "broker": eq("zerodha")}, limit=1)[0]
        tag = tag_for(order_id)
        params = {**o["params"], "qty": pv["qty"], "limit": pv["limit"], "exchange": pv["exchange"], "tag": tag,
                  "placed_t": int(now.timestamp()), "live_preview": {k: pv[k] for k in ("funds", "margin", "live_limits", "last_price")}}
        d.update("orders", {"id": eq(order_id)}, {"mode": "live", "account_id": acct["id"], "state": "sending", "params": params,
                                                  "risk_verdict": pv["verdict"], "approved_at": _iso(now), "updated_at": _iso(now)})
        paper.audit(user_id, "user", "live_approve", {"order_id": order_id, "qty": pv["qty"], "limit": pv["limit"],
                                                      "symbol": pv["symbol"], "aal": aal})
        b = session.broker_for(user_id, now)
        try:
            kid = b.place(exchange=pv["exchange"], tradingsymbol=pv["symbol"], side=pv["side"], qty=pv["qty"],
                          order_type="LIMIT", price=pv["limit"], trigger=None, tag=tag)
        except BrokerError as e:
            if e.ambiguous:      # it may have reached Zerodha: sync looks it up by tag; never sent twice
                paper.audit(user_id, "system", "live_send_unconfirmed", {"order_id": order_id, "error": str(e)})
                return d.select("orders", {"id": eq(order_id)})[0]
            out = d.update("orders", {"id": eq(order_id)}, {"state": "rejected", "updated_at": _iso(now),
                                                            "params": {**params, "broker_error": str(e)}})
            paper.audit(user_id, "system", "live_rejected", {"order_id": order_id, "error": str(e)})
            raise LiveOrderError(f"Zerodha refused the order: {e}") from e
        out = d.update("orders", {"id": eq(order_id)}, {"state": "working", "broker_order_id": kid, "updated_at": _iso(now)})
        if o.get("signal_id"):
            d.update("signals", {"id": eq(o["signal_id"])}, {"status": "ordered"})
        paper.audit(user_id, "system", "live_sent", {"order_id": order_id, "kite_order_id": kid})
        return out[0]


# ---------------------------------------------------------------- the sync loop

def _marketable(side: str, last: float, tick: float) -> float:
    """A LIMIT that crosses the spread: a sell below the last price, a buy above it."""
    return round_to_tick(last * (1 - EXIT_LIMIT_PCT / 100), tick, up=False) if side == "sell" else \
        round_to_tick(last * (1 + EXIT_LIMIT_PCT / 100), tick, up=True)


def _stop_order(long: bool, stop: float, tick: float) -> tuple[str, float, float]:
    """(side, trigger, limit) of the SL order protecting a position."""
    if long:
        return "sell", stop, round_to_tick(stop * (1 - STOP_LIMIT_PCT / 100), tick, up=False)
    return "buy", stop, round_to_tick(stop * (1 + STOP_LIMIT_PCT / 100), tick, up=True)


def _risk_event(user_id: str, kind: str, detail: dict) -> None:
    try:
        db().insert("risk_events", {"user_id": user_id, "kind": kind, "detail": detail})
    except Exception as e:  # noqa: BLE001
        log.warning("risk event failed: %s", e)
    log.warning("live %s: %s", kind, detail)


def _booked(ctx: dict) -> int:
    """The quantity a partial exit has already closed."""
    return int(((ctx.get("manage") or {}).get("partial") or {}).get("qty") or 0)


def _stop_qty(t: dict, ctx: dict) -> int:
    """What the stop order must cover: the position less the partial booked and the partial still working."""
    return int(t["qty"]) - _booked(ctx) - int((ctx.get("partial_order") or {}).get("qty") or 0)


def _protect(b: Broker, t: dict, ctx: dict, now: dt.datetime) -> None:
    """Place the stop for an open position. If that fails, close the position at once."""
    long = t["side"] == "buy"
    inst = resolve(ctx.get("app_symbol") or t["symbol"], now.date())
    side, trig, lim = _stop_order(long, float(ctx["stop"]), inst.tick(float(ctx["stop"])))
    qty = _stop_qty(t, ctx)
    try:
        ctx["kite_stop_id"] = b.place(exchange=ctx["exchange"], tradingsymbol=t["symbol"], side=side, qty=qty,
                                      order_type="SL", price=lim, trigger=trig, tag=ctx["tag"])
        ctx["protected"] = True
        paper.audit(t["user_id"], "system", "live_stop_placed", {"trade_id": t.get("id"), "kite_order_id": ctx["kite_stop_id"]})
    except BrokerError as e:
        ctx["protected"] = False
        _risk_event(t["user_id"], "live_unprotected", {"symbol": t["symbol"], "error": str(e)})
        last = b.ltp([f"{ctx['exchange']}:{t['symbol']}"]).get(f"{ctx['exchange']}:{t['symbol']}") or float(t["entry"])
        try:          # no stop possible: get out now
            ctx["kite_stop_id"] = b.place(exchange=ctx["exchange"], tradingsymbol=t["symbol"], side=side, qty=qty,
                                          order_type="LIMIT", price=_marketable(side, last, inst.tick(last)), trigger=None,
                                          tag=ctx["tag"])
            ctx.update(exiting="stop could not be placed", exiting_t=int(now.timestamp()))
        except BrokerError as e2:
            _risk_event(t["user_id"], "live_exit_failed", {"symbol": t["symbol"], "error": str(e2),
                                                           "action": "CLOSE THIS POSITION IN KITE NOW"})


def _open_trade(o: dict, bo: BrokerOrder, now: dt.datetime, b: Broker) -> None:
    d = db()
    p = o["params"]
    qty, price = bo.filled, bo.average_price
    d.insert("fills", {"order_id": o["id"], "user_id": o["user_id"], "ts": _iso(now), "qty": qty, "price": price,
                       "costs": {"entry_side": paper.charges_for(p.get("kind", "equity"), price * qty, 0)}})
    d.update("orders", {"id": eq(o["id"])}, {"state": "filled", "updated_at": _iso(now)})
    sig = d.select("signals", {"id": eq(o["signal_id"])}, columns="explanation") if o.get("signal_id") else []
    exp = sig[0]["explanation"] if sig else None
    mg = None
    if (p.get("manage") or {}).get("rules"):        # confirmed with the entry: runs without asking again
        risk = abs(price - float(p["stop"]))
        mg = manage.start(p["manage"]["rules"], long=p["side"] == "buy", entry=price, stop=float(p["stop"]), qty=qty,
                          lot=int(p.get("lot_size") or 1), atr=p["manage"].get("atr"), entry_t=int(now.timestamp()),
                          target_r=abs(float(p["target"]) - price) / risk if p.get("target") is not None and risk else None,
                          tick=resolve(p.get("app_symbol") or p["symbol"], now.date()).tick(price))
    ctx = {**paper.spec(p), "app_symbol": p.get("app_symbol"), "exchange": p["exchange"], "tag": p["tag"],
           "expiry": p.get("expiry"), "order_id": o["id"], "kite_entry_id": bo.order_id, "stop": p["stop"],
           "initial_stop": p["stop"], "manage": mg,
           "target": p.get("target"), "risk_amount": round(abs(price - float(p["stop"])) * qty, 2),
           "strategy": p.get("strategy"), "strategy_version": p.get("strategy_version"), "source": p.get("source"),
           "summary": (exp or {}).get("summary"), "mae": 0, "mfe": 0}
    t = d.insert("trades", {"user_id": o["user_id"], "mode": "live", "signal_id": o.get("signal_id"), "symbol": p["symbol"],
                            "side": p["side"], "qty": qty, "entry_ts": _iso(now), "entry": price, "rules_followed": True,
                            "entry_context": ctx})[0]
    paper.audit(o["user_id"], "system", "live_entry_filled", {"order_id": o["id"], "qty": qty, "price": price})
    _protect(b, t, ctx, now)
    d.update("trades", {"id": eq(t["id"])}, {"entry_context": ctx})


def _close_trade(t: dict, ctx: dict, price: float, reason: str, now: dt.datetime) -> None:
    long = t["side"] == "buy"
    qty, entry = t["qty"], float(t["entry"])
    part = (ctx.get("manage") or {}).get("partial")
    legs = [(int(part["qty"]), float(part["price"])), (qty - int(part["qty"]), price)] if part else [(qty, price)]
    gross = costs = 0.0
    for q, px in legs:
        buy_v, sell_v = (entry * q, px * q) if long else (px * q, entry * q)
        gross += (px - entry) * q if long else (entry - px) * q
        costs += paper.charges_for(ctx.get("kind", "equity"), buy_v, sell_v)
    costs = round(costs, 2)
    net = gross - costs
    risk = float(ctx.get("risk_amount") or 0)
    db().update("trades", {"id": eq(t["id"])}, {
        "exit_ts": _iso(now), "exit": price, "gross_pnl": round(gross, 2), "costs": costs, "net_pnl": round(net, 2),
        "r_multiple": round(net / risk, 3) if risk else None, "exit_reason": reason, "entry_context": ctx,
        "mae": ctx.get("mae"), "mfe": ctx.get("mfe")})
    paper._after_close(t, {"exit_reason": reason, "net_pnl": round(net, 2)})


def _sync_entries(user_id: str, b: Broker, by_id: dict[str, BrokerOrder], by_tag: dict[str, BrokerOrder], now: dt.datetime) -> None:
    d = db()
    for o in d.select("orders", {"user_id": eq(user_id), "mode": eq("live"), "state": "in.(sending,working)"}):
        p = o["params"]
        age = now.timestamp() - int(p.get("placed_t") or now.timestamp())
        bo = by_id.get(o.get("broker_order_id") or "") or by_tag.get(p.get("tag", ""))
        if o["state"] == "sending":
            if bo:
                d.update("orders", {"id": eq(o["id"])}, {"state": "working", "broker_order_id": bo.order_id, "updated_at": _iso(now)})
                o["broker_order_id"] = bo.order_id
            elif age > SENDING_TIMEOUT:
                d.update("orders", {"id": eq(o["id"])}, {"state": "rejected", "updated_at": _iso(now)})
                _risk_event(user_id, "live_unknown_order", {"order_id": o["id"], "symbol": p["symbol"],
                                                            "action": "check Kite's order book: the order was not found"})
                continue
            else:
                continue
        if bo is None:
            continue
        if bo.status == "COMPLETE" or (bo.done and bo.filled > 0):
            _open_trade(o, bo, now, b)
        elif bo.status in ("CANCELLED", "REJECTED"):
            d.update("orders", {"id": eq(o["id"])}, {"state": "rejected" if bo.status == "REJECTED" else "expired",
                                                     "updated_at": _iso(now), "params": {**p, "broker_message": bo.message}})
            paper.audit(user_id, "system", f"live_entry_{bo.status.lower()}", {"order_id": o["id"], "message": bo.message})
        elif age > ENTRY_EXPIRE_SECONDS and not p.get("cancel_sent"):
            try:
                b.cancel(bo.order_id)
                d.update("orders", {"id": eq(o["id"])}, {"params": {**p, "cancel_sent": True}})
            except BrokerError as e:
                log.warning("cancel of a stale entry failed: %s", e)


def _book_partial(t: dict, ctx: dict, mg: dict, qty: int, price: float, now: dt.datetime) -> None:
    """A partial exit filled at Kite: record it (added to any part already booked) as a fill with its own charges."""
    prev = mg.get("partial")
    total, avg = qty, price
    if prev:            # a remainder sent after a refused partial: one weighted booking
        total = prev["qty"] + qty
        avg = (prev["qty"] * prev["price"] + qty * price) / total
    mg["partial_qty"] = total
    pt = manage.book_partial(mg, int(now.timestamp()), avg)
    long, entry = t["side"] == "buy", float(t["entry"])
    buy_v, sell_v = (entry * qty, price * qty) if long else (price * qty, entry * qty)
    try:
        db().insert("fills", {"order_id": ctx.get("order_id"), "user_id": t["user_id"], "ts": _iso(now), "qty": qty,
                              "price": price, "costs": {"partial_exit": paper.charges_for(ctx.get("kind", "equity"), buy_v, sell_v)}})
    except Exception as e:  # noqa: BLE001 - the trade still records the partial in its entry_context
        log.warning("partial fill row failed for %s: %s", t["id"], e)
    paper.audit(t["user_id"], "system", "live_partial_filled", {"trade_id": t["id"], **pt})


def _sync_partial(b: Broker, t: dict, ctx: dict, mg: dict, so: BrokerOrder | None, by_id: dict[str, BrokerOrder],
                  by_tag: dict[str, BrokerOrder], last: float | None, now: dt.datetime) -> None:
    """Follow a working partial-exit order: book it when it fills, re-price it while it does not."""
    po = ctx["partial_order"]
    bo = by_id.get(po.get("id") or "") or (by_tag.get(po["tag"]) if not po.get("id") else None)
    if bo is None:
        if po.get("id") or now.timestamp() - po["t"] <= SENDING_TIMEOUT:
            return
        ctx.pop("partial_order")             # it never reached Kite: the stop's quantity is restored below
        mg["partial_qty"] = 0
        mg["notes"].append("partial order was not found at Kite: the stop covers the full quantity again")
        _risk_event(t["user_id"], "live_partial_failed", {"symbol": t["symbol"], "error": "the order was not found at Kite"})
        return
    po["id"] = bo.order_id
    if bo.status == "COMPLETE":
        ctx.pop("partial_order")
        _book_partial(t, ctx, mg, bo.filled, bo.average_price, now)
    elif bo.status in ("CANCELLED", "REJECTED"):
        ctx.pop("partial_order")
        if bo.filled:
            _book_partial(t, ctx, mg, bo.filled, bo.average_price, now)
        else:
            mg["partial_qty"] = 0            # not sent again: the rest of the trade runs on its stop
        rest = int(po["qty"]) - bo.filled
        _risk_event(t["user_id"], "live_partial_failed", {"symbol": t["symbol"], "status": bo.status, "message": bo.message,
                                                          "unfilled": rest})
        mg["notes"].append(f"partial order {bo.status.lower()} at Kite ({bo.message or 'no reason given'})")
        if rest > 0 and not (so and so.is_open) and last:
            # the stop already went: nothing else covers the unfilled part, so it goes out now
            side = "sell" if t["side"] == "buy" else "buy"
            tick = resolve(ctx.get("app_symbol") or t["symbol"], now.date()).tick(last)
            try:
                pid = b.place(exchange=ctx["exchange"], tradingsymbol=t["symbol"], side=side, qty=rest, order_type="LIMIT",
                              price=_marketable(side, last, tick), trigger=None, tag=po["tag"])
                ctx["partial_order"] = {"id": pid, "tag": po["tag"], "qty": rest, "t": int(now.timestamp())}
            except BrokerError as e:
                _risk_event(t["user_id"], "live_exit_failed", {"symbol": t["symbol"], "error": str(e), "qty": rest,
                                                               "action": "CLOSE THIS POSITION IN KITE NOW"})
    elif last and now.timestamp() - int(po["t"]) > REPRICE_SECONDS:
        side = "sell" if t["side"] == "buy" else "buy"
        try:
            b.modify(bo.order_id, price=_marketable(side, last, resolve(ctx.get("app_symbol") or t["symbol"], now.date()).tick(last)))
            po["t"] = int(now.timestamp())
        except BrokerError as e:
            log.warning("partial re-price failed: %s", e)


def _manage(b: Broker, t: dict, ctx: dict, mg: dict, so: BrokerOrder, last: float, now: dt.datetime) -> str | None:
    """Trade management on Kite's last price, folded into 1-minute bars like the paper trader's. Returns an exit
    reason (time stop, or the moved stop already crossed) or None."""
    long = t["side"] == "buy"
    minute = int(now.timestamp()) // 60 * 60
    bar, reason = ctx.get("bar"), None
    if bar and bar["t"] < minute:            # a minute finished: the time stop, then the stop moves for later bars
        end = int(bar["t"]) + 60
        if manage.time_stop_due(mg, float(bar["c"]), end):
            reason = "time-stop"
        else:
            for mv in manage.after_bar(mg, float(bar["h"]), float(bar["l"]), end):
                paper.audit(t["user_id"], "system", "live_stop_moved", {"trade_id": t["id"], **mv})
        bar = None
    ctx["bar"] = {"t": minute, "h": last, "l": last, "c": last} if bar is None else \
        {**bar, "h": max(float(bar["h"]), last), "l": min(float(bar["l"]), last), "c": last}
    ctx["stop"] = mg["stop"]
    if reason:
        return reason
    inst = resolve(ctx.get("app_symbol") or t["symbol"], now.date())
    if so.order_type == "SL" and round(float(so.trigger_price or 0), 2) != round(float(mg["stop"]), 2):
        if (long and last <= mg["stop"]) or (not long and last >= mg["stop"]):
            return "stop"                    # the moved stop is already crossed: Kite would refuse that trigger
        _, trig, lim = _stop_order(long, float(mg["stop"]), inst.tick(float(mg["stop"])))
        try:                                 # the resting stop moves: never cancelled and re-placed
            b.modify(so.order_id, trigger=trig, price=lim)
            so.trigger_price, so.price = trig, lim
            paper.audit(t["user_id"], "system", "live_stop_modified", {"trade_id": t["id"], "kite_order_id": so.order_id,
                                                                       "trigger": trig})
        except BrokerError as e:             # the old stop still rests: protected, and tried again next pass
            if ctx.get("stop_move_failed") != trig:
                ctx["stop_move_failed"] = trig
                _risk_event(t["user_id"], "live_stop_move_failed", {"symbol": t["symbol"], "to": trig, "error": str(e),
                                                                    "action": "the previous stop still protects the position"})
    if manage.partial_hit(mg, last, last) and not ctx.get("partial_order") and so.order_type == "SL":
        q = int(mg["partial_qty"])
        side = "sell" if long else "buy"
        try:                                 # shrink the stop FIRST: the exit orders never exceed the position
            b.modify(so.order_id, qty=_stop_qty(t, ctx) - q)
            so.qty = _stop_qty(t, ctx) - q
        except BrokerError as e:
            log.warning("partial: stop quantity change failed (%s); retrying next pass", e)
            return None
        tag = ctx["tag"] + "p"
        ctx["partial_order"] = {"id": None, "tag": tag, "qty": q, "t": int(now.timestamp())}
        try:
            ctx["partial_order"]["id"] = b.place(exchange=ctx["exchange"], tradingsymbol=t["symbol"], side=side, qty=q,
                                                 order_type="LIMIT", price=_marketable(side, last, inst.tick(last)),
                                                 trigger=None, tag=tag)
            paper.audit(t["user_id"], "system", "live_partial_sent", {"trade_id": t["id"], "qty": q, "last": last,
                                                                      "level": manage.partial_level(mg)})
        except BrokerError as e:
            if not e.ambiguous:              # refused: the stop goes back to the full quantity below
                ctx.pop("partial_order")
                mg["partial_qty"] = 0
                mg["notes"].append(f"partial order refused by Kite: {e}")
                _risk_event(t["user_id"], "live_partial_failed", {"symbol": t["symbol"], "error": str(e)})
    return None


def _fit_stop(b: Broker, t: dict, ctx: dict, so: BrokerOrder) -> None:
    """The stop (or the exit it became) covers exactly what is open: no more (a partial is working), no less."""
    want = _stop_qty(t, ctx)
    if want <= 0 or so.qty == want:
        return
    try:
        b.modify(so.order_id, qty=want)
        so.qty = want
    except BrokerError as e:
        _risk_event(t["user_id"], "live_unprotected", {"symbol": t["symbol"], "error": str(e), "stop_qty": so.qty,
                                                       "open_qty": want, "action": "set the stop order's quantity in Kite"})


def _sync_trades(user_id: str, b: Broker, by_id: dict[str, BrokerOrder], flat_by: dt.time, now: dt.datetime) -> None:
    d = db()
    trades = d.select("trades", {"user_id": eq(user_id), "mode": eq("live"), "exit_ts": "is.null"})
    if not trades:
        return
    by_tag: dict[str, BrokerOrder] = {}
    for o in by_id.values():
        if o.tag and o.tag not in by_tag:
            by_tag[o.tag] = o
    keys = sorted({f"{(t['entry_context'] or {})['exchange']}:{t['symbol']}" for t in trades})
    try:
        ltp = b.ltp(keys)
    except BrokerError:
        ltp = {}
    for t in trades:
        ctx = dict(t["entry_context"] or {})
        long = t["side"] == "buy"
        mg = ctx.get("manage")
        last = ltp.get(f"{ctx['exchange']}:{t['symbol']}")
        if last:
            move = (last - float(t["entry"])) if long else (float(t["entry"]) - last)
            ctx["mae"], ctx["mfe"] = round(min(float(ctx.get("mae") or 0), move * t["qty"]), 2), round(max(float(ctx.get("mfe") or 0), move * t["qty"]), 2)
        so = by_id.get(ctx.get("kite_stop_id") or "")
        if ctx.get("partial_order") and mg:
            _sync_partial(b, t, ctx, mg, so, by_id, by_tag, last, now)
        if so and so.status == "COMPLETE":
            if ctx.get("partial_order"):         # the partial is still working: close once it has filled
                d.update("trades", {"id": eq(t["id"])}, {"entry_context": ctx, "mae": ctx.get("mae"), "mfe": ctx.get("mfe")})
                continue
            _close_trade(t, ctx, so.average_price, ctx.get("exiting") or "stop", now)
            continue
        if so is None or so.status in ("CANCELLED", "REJECTED"):
            if so is not None:
                _risk_event(user_id, "live_stop_lost", {"symbol": t["symbol"], "status": so.status, "message": so.message})
            _protect(b, t, ctx, now)
            d.update("trades", {"id": eq(t["id"])}, {"entry_context": ctx})
            continue
        _fit_stop(b, t, ctx, so)
        reason = None
        if ctx.get("exit_requested"):
            reason = ctx["exit_requested"]
        elif now.astimezone(IST).time() >= flat_by:
            reason = "flat-by"
        elif last and ctx.get("target") is not None and ((long and last >= float(ctx["target"])) or (not long and last <= float(ctx["target"]))):
            reason = "target"
        elif mg and last and not ctx.get("exiting"):
            reason = _manage(b, t, ctx, mg, so, last, now)
            _fit_stop(b, t, ctx, so)         # a partial that did not go out: the stop covers everything again
        inst = resolve(ctx.get("app_symbol") or t["symbol"], now.date())
        exit_side = "sell" if long else "buy"
        if reason and not ctx.get("exiting") and last:
            try:      # the stop order itself becomes the exit: one exit order per position, always
                b.modify(so.order_id, order_type="LIMIT", price=_marketable(exit_side, last, inst.tick(last)))
                ctx.update(exiting=reason, exiting_t=int(now.timestamp()))
                paper.audit(user_id, "system", "live_exit", {"trade_id": t["id"], "reason": reason, "last": last})
            except BrokerError as e:
                log.warning("exit modify failed (%s); retrying next pass", e)
        elif ctx.get("exiting") and last and now.timestamp() - int(ctx.get("exiting_t") or 0) > REPRICE_SECONDS:
            try:
                b.modify(so.order_id, order_type="LIMIT", price=_marketable(exit_side, last, inst.tick(last)))
                ctx["exiting_t"] = int(now.timestamp())
            except BrokerError as e:
                log.warning("exit re-price failed: %s", e)
        d.update("trades", {"id": eq(t["id"])}, {"entry_context": ctx, "mae": ctx.get("mae"), "mfe": ctx.get("mfe")})


def reconcile(user_id: str, b: Broker, now: dt.datetime) -> list[dict]:
    """Kite's MIS positions vs our open live trades. Differences are alerts, not trades."""
    d = db()
    ours: dict[str, int] = {}
    open_trades = d.select("trades", {"user_id": eq(user_id), "mode": eq("live"), "exit_ts": "is.null"})
    for t in open_trades:
        q = int(t["qty"]) - _booked(t["entry_context"] or {})
        ours[t["symbol"]] = ours.get(t["symbol"], 0) + (q if t["side"] == "buy" else -q)
    theirs = {p["tradingsymbol"]: p["qty"] for p in b.positions()}
    pending = {o["params"]["symbol"] for o in d.select("orders", {"user_id": eq(user_id), "mode": eq("live"),
                                                                  "state": "in.(sending,working)"}, columns="params")}
    pending |= {t["symbol"] for t in open_trades if (t["entry_context"] or {}).get("partial_order")}
    diffs = []
    for sym in sorted(set(ours) | {s for s, q in theirs.items() if q}):
        if sym in pending:                   # an entry is filling right now: compare once it is done
            continue
        a, k = ours.get(sym, 0), theirs.get(sym, 0)
        if a == k:
            continue
        diffs.append({"symbol": sym, "app": a, "kite": k})
        if a and k == 0:    # Zerodha squared it off (e.g. 15:25 auto square-off, or you closed it in Kite)
            for t in (x for x in open_trades if x["symbol"] == sym):
                ctx = dict(t["entry_context"] or {})
                so = None
                try:
                    so = next((o for o in b.orders() if o.order_id == ctx.get("kite_stop_id")), None)
                except BrokerError:
                    pass
                price = so.average_price if so and so.filled else \
                    (b.ltp([f"{ctx['exchange']}:{sym}"]).get(f"{ctx['exchange']}:{sym}") or float(t["entry"]))
                _close_trade(t, ctx, price, "closed outside the app", now)
    if diffs and _last_diffs.get(user_id) != diffs:          # alert once per new difference, not every pass
        _risk_event(user_id, "reconcile_mismatch", {"at": now.astimezone(IST).strftime("%H:%M:%S"), "differences": diffs})
    _last_diffs[user_id] = diffs
    return diffs


def sync(user_id: str, now: dt.datetime | None = None) -> dict:
    """One pass for one user: entries, open trades, reconciliation."""
    now = now or _now()
    with _lock:
        try:
            b = session.broker_for(user_id, now)
        except session.LiveError:
            return {"skipped": "no Kite session"}
        try:
            orders = b.orders()
        except BrokerError as e:
            _risk_event(user_id, "live_broker_error", {"error": str(e)})
            return {"error": str(e)}
        by_id = {o.order_id: o for o in orders}
        by_tag: dict[str, BrokerOrder] = {}
        for o in orders:                         # the entry is the first order with a tag; stops reuse it later
            if o.tag and o.tag not in by_tag:
                by_tag[o.tag] = o
        _sync_entries(user_id, b, by_id, by_tag, now)
        flat_by = Limits.from_row(G.live_limits_row(user_id)).flat_by
        _sync_trades(user_id, b, {o.order_id: o for o in b.orders()}, flat_by, now)
        diffs = reconcile(user_id, b, now)
        return {"ok": True, "differences": diffs}


def users_with_live_work() -> list[str]:
    d = db()
    ids = {o["user_id"] for o in d.select("orders", {"mode": eq("live"), "state": "in.(sending,working)"}, columns="user_id")}
    ids |= {t["user_id"] for t in d.select("trades", {"mode": eq("live"), "exit_ts": "is.null"}, columns="user_id")}
    return sorted(ids)


# ---------------------------------------------------------------- your buttons

def close(user_id: str, trade_id: str, reason: str = "manual") -> dict:
    """Close a live position (never blocked). The next sync turns its stop order into a marketable exit."""
    with _lock:
        d = db()
        rows = d.select("trades", {"id": eq(trade_id), "user_id": eq(user_id), "mode": eq("live")})
        if not rows or rows[0]["exit_ts"]:
            raise LiveOrderError("no such open live position")
        ctx = {**(rows[0]["entry_context"] or {}), "exit_requested": reason}
        d.update("trades", {"id": eq(trade_id)}, {"entry_context": ctx})
        paper.audit(user_id, "user", "live_close", {"trade_id": trade_id, "reason": reason})
    sync(user_id)
    return {"closing": trade_id}


def kill_switch(user_id: str) -> dict:
    """Cancel live entries at Zerodha, close every live position, no new entries today."""
    cancelled, closing = 0, 0
    with _lock:
        d = db()
        try:
            b = session.broker_for(user_id, _now())
        except session.LiveError:
            b = None
        for o in d.select("orders", {"user_id": eq(user_id), "mode": eq("live"), "state": "in.(sending,working)"}):
            if b and o.get("broker_order_id"):
                try:
                    b.cancel(o["broker_order_id"])
                    cancelled += 1
                except BrokerError as e:
                    log.warning("kill switch cancel failed: %s", e)
        for t in d.select("trades", {"user_id": eq(user_id), "mode": eq("live"), "exit_ts": "is.null"}):
            d.update("trades", {"id": eq(t["id"])}, {"entry_context": {**(t["entry_context"] or {}), "exit_requested": "kill switch"}})
            closing += 1
        d.update("risk_limits", {"user_id": eq(user_id)}, {"live_armed_on": None})
        paper.audit(user_id, "user", "live_kill_switch", {"cancelled": cancelled, "closing": closing})
    if b:
        sync(user_id)
    return {"cancelled_live_orders": cancelled, "closing_live_positions": closing}


def arm(user_id: str, aal: str | None, confirm: str, now: dt.datetime | None = None) -> dict:
    now = now or _now()
    gs = G.check(user_id, aal, now, for_arming=True)
    if not G.all_ok(gs):
        raise LiveOrderError("cannot arm live trading: " + "; ".join(G.blocking(gs)))
    if confirm.strip().upper() != "LIVE":
        raise LiveOrderError('type LIVE to confirm that orders will use real money')
    kill = db().select("risk_events", {"user_id": eq(user_id), "kind": eq("kill_switch"), "ts": f"gte.{paper._day_start(now)}"}, limit=1)
    if kill:
        raise LiveOrderError("the kill switch was used today: live trading stays off until tomorrow")
    db().update("risk_limits", {"user_id": eq(user_id)}, {"live_armed_on": now.astimezone(IST).date().isoformat()})
    paper.audit(user_id, "user", "live_armed", {"date": now.astimezone(IST).date().isoformat(), "aal": aal})
    return {"armed_on": now.astimezone(IST).date().isoformat()}


def disarm(user_id: str) -> dict:
    db().update("risk_limits", {"user_id": eq(user_id)}, {"live_armed_on": None})
    paper.audit(user_id, "user", "live_disarmed", {})
    return {"armed_on": None}


def overview(user_id: str, aal: str | None) -> dict:
    d = db()
    row = G.live_limits_row(user_id)
    out = {"gates": G.check(user_id, aal, _now()), "session": session.status(user_id, _now()) if "live_max_loss_per_trade" in row else {"logged_in": False},
           "limits": {k: row.get(k) for k in ("live_max_loss_per_trade", "live_max_daily_loss", "live_max_trades_per_day",
                                               "live_max_order_value", "live_armed_on")},
           "orders": d.select("orders", {"user_id": eq(user_id), "mode": eq("live")}, order="created_at.desc", limit=30),
           "positions": d.select("trades", {"user_id": eq(user_id), "mode": eq("live"), "exit_ts": "is.null"}),
           "trades": d.select("trades", {"user_id": eq(user_id), "mode": eq("live"), "exit_ts": "not.is.null"},
                              order="exit_ts.desc", limit=50),
           "alerts": d.select("risk_events", {"user_id": eq(user_id), "kind": "in.(reconcile_mismatch,live_unprotected,live_exit_failed,live_stop_lost,live_unknown_order,live_broker_error,"
                                              "live_stop_move_failed,live_partial_failed)",
                                              "ts": f"gte.{paper._day_start(_now())}"}, order="ts.desc", limit=20),
           "funds": None}
    if out["session"].get("logged_in"):
        try:
            out["funds"] = session.broker_for(user_id, _now()).funds()
        except (session.LiveError, BrokerError) as e:
            out["funds_error"] = str(e)
    return out


def update_live_limits(user_id: str, values: dict) -> dict:
    allowed = {"live_max_loss_per_trade", "live_max_daily_loss", "live_max_trades_per_day", "live_max_order_value"}
    if set(values) - allowed:
        raise LiveOrderError(f"unknown live limit(s): {', '.join(sorted(set(values) - allowed))}")
    for k, v in values.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise LiveOrderError(f"{k} must be a positive number")
    out = db().update("risk_limits", {"user_id": eq(user_id)}, values)
    paper.audit(user_id, "user", "update_live_limits", values)
    return out[0]


class LiveLoop:
    """sync() for every user with live orders or positions, every few seconds while the market is open."""

    def __init__(self, every: float = 3.0):
        self.every, self._stop = every, threading.Event()
        self.last: dict = {}

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="live-loop").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from app.core.calendar import market_status
        while not self._stop.is_set():
            try:
                now = _now()
                st = market_status(now)
                if st.phase in ("pre-open", "open") or (st.phase == "post-close" and now.time() <= dt.time(15, 40)):
                    for uid in users_with_live_work():
                        self.last[uid] = sync(uid, now)
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                log.exception("live loop: %s", e)
            self._stop.wait(self.every)
            time.sleep(0)
