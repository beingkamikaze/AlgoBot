"""Paper trading: accounts, orders, fills and trades in Supabase.

Flow (assisted): propose -> risk check -> you confirm -> working -> filled -> open
trade watched for stop / target / 15:15 -> closed with Zerodha charges. Nothing
here talks to a broker; Phase 8 adds a live Kite adapter behind the same flow.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading

from app.core import manage
from app.core.calendar import IST, market_status
from app.core.costs import (equity_intraday_charges, futures_charges, index_derivative_tick, round_to_tick,
                            tick_for)
from app.core.instruments import Instrument, resolve
from app.db import db, eq
from app.market import feed, yahoo
from app.options import chain as ch
from app.options import structures as S
from app.paper import options as paper_options
from app.paper import sim
from app.risk.engine import AccountState, Limits, OpenPosition, OrderRequest, check, is_hard

log = logging.getLogger("paper")
ENTRY_BUFFER_PCT = 0.10       # a buy limit sits this far above the last price so it fills, like nse-budget
EXPIRE_MINUTES = 10
_lock = threading.RLock()


class PaperError(Exception):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _iso(t: dt.datetime | int) -> str:
    if isinstance(t, int):
        t = dt.datetime.fromtimestamp(t, IST)
    return t.astimezone(dt.timezone.utc).isoformat()


def _day_start(now: dt.datetime) -> str:
    return _iso(dt.datetime.combine(now.astimezone(IST).date(), dt.time(0, 0), IST))


def _month_start(now: dt.datetime) -> str:
    d = now.astimezone(IST).date()
    return _iso(dt.datetime.combine(d.replace(day=1), dt.time(0, 0), IST))


def spec(d: dict | None, fallback_symbol: str | None = None) -> dict:
    """Instrument facts stored on an order's params or a trade's entry_context."""
    d = d or {}
    return {"data_symbol": d.get("data_symbol") or d.get("symbol") or fallback_symbol, "kind": d.get("kind", "equity"),
            "lot_size": int(d.get("lot_size") or 1), "margin_pct": float(d.get("margin_pct") or 100.0)}


def open_qty(t: dict) -> int:
    """What is still open: the quantity less a partial exit already booked."""
    part = ((t.get("entry_context") or {}).get("manage") or {}).get("partial") or {}
    return int(t["qty"]) - int(part.get("qty") or 0)


def charges_for(kind: str, buy_value: float, sell_value: float) -> float:
    return (futures_charges if kind == "future" else equity_intraday_charges)(buy_value, sell_value).total


def audit(user_id: str | None, actor: str, action: str, detail: dict) -> None:
    try:
        db().insert("audit_log", {"user_id": user_id, "actor": actor, "action": action, "detail": detail})
    except Exception as e:  # noqa: BLE001 - the audit log must never break trading
        log.warning("audit log failed: %s", e)


# ---------------------------------------------------------------- account

def ensure_account(user_id: str, email: str | None) -> tuple[dict, dict]:
    """Profile, risk limits and the paper account; created on first use (also for accounts
    made before the database schema existed)."""
    d = db()
    if not d.select("profiles", {"user_id": eq(user_id)}, columns="user_id"):
        d.insert("profiles", {"user_id": user_id, "display_name": (email or "trader").split("@")[0]}, upsert_on="user_id")
    limits = d.select("risk_limits", {"user_id": eq(user_id)})
    if not limits:
        limits = d.insert("risk_limits", {"user_id": user_id}, upsert_on="user_id")
    acct = d.select("broker_accounts", {"user_id": eq(user_id), "broker": eq("paper")}, order="created_at", limit=1)
    if not acct:
        acct = d.insert("broker_accounts", {"user_id": user_id, "broker": "paper", "label": "Paper",
                                            "starting_capital": 1000000})
    return acct[0], limits[0]


def account_state(user_id: str, account: dict, now: dt.datetime | None = None, mode: str = "paper") -> tuple[AccountState, dict]:
    now = now or _now()
    d = db()
    trades = d.select("trades", {"user_id": eq(user_id), "mode": eq(mode)},
                      columns="id,symbol,side,qty,entry,exit,net_pnl,entry_ts,exit_ts,entry_context", order="entry_ts")
    working = d.select("orders", {"user_id": eq(user_id), "mode": eq(mode), "state": "in.(working,sending)"}, columns="params")
    day, month = _day_start(now), _month_start(now)
    closed = [t for t in trades if t["exit_ts"]]
    open_ = [t for t in trades if not t["exit_ts"]]
    capital = float(account.get("starting_capital") or 0) + sum(float(t["net_pnl"] or 0) for t in closed)
    # cash blocked: the full value of stock positions, the margin of futures and option positions
    def blocked(t):
        ctx = t["entry_context"] or {}
        if ctx.get("kind") == "option":
            return float(ctx.get("margin") or 0)
        return float(t["entry"]) * open_qty(t) * spec(ctx)["margin_pct"] / 100
    tied = sum(blocked(t) for t in open_)
    for o in working:
        op = o["params"]
        if op.get("leg") != "entry":
            continue
        tied += float(op["structure"]["margin"]) if op.get("kind") == "option" else \
            float(op["limit"]) * op["qty"] * spec(op)["margin_pct"] / 100
    today_closed = sorted((t for t in closed if t["exit_ts"] >= day), key=lambda t: t["exit_ts"])
    streak = 0
    for t in reversed(today_closed):
        if float(t["net_pnl"] or 0) < 0:
            streak += 1
        else:
            break
    entries_today = sum(1 for t in trades if t["entry_ts"] and t["entry_ts"] >= day)
    entries_today += sum(1 for o in working if o["params"].get("leg") == "entry")
    kill = d.select("risk_events", {"user_id": eq(user_id), "kind": eq("kill_switch"), "ts": f"gte.{day}"}, limit=1)
    st = AccountState(
        capital=round(capital, 2), cash_free=round(capital - tied, 2),
        open_positions=[
            # an option position's open risk is its planned loss: expressed as qty 1, entry = loss, stop = 0
            OpenPosition(t["symbol"], t["side"], 1, float((t["entry_context"] or {}).get("risk_amount") or 0), 0.0)
            if (t["entry_context"] or {}).get("kind") == "option" else
            OpenPosition(t["symbol"], t["side"], open_qty(t), float(t["entry"]),
                         float((t["entry_context"] or {}).get("stop") or t["entry"])) for t in open_],
        entries_today=entries_today,
        realized_today=round(sum(float(t["net_pnl"] or 0) for t in today_closed), 2),
        realized_month=round(sum(float(t["net_pnl"] or 0) for t in closed if t["exit_ts"] >= month), 2),
        consecutive_losses=streak, kill_switch=bool(kill),
    )
    return st, {"trades": trades, "working": working}


_KITE_INDEX = {"^NSEI": "NSE:NIFTY 50", "NIFTY": "NSE:NIFTY 50", "^NSEBANK": "NSE:NIFTY BANK", "BANKNIFTY": "NSE:NIFTY BANK",
               "^INDIAVIX": "NSE:INDIA VIX"}
_no_kite_until = 0.0


def kite_price(symbol: str) -> float | None:
    """Kite's last price when you are logged in to Kite today (real-time); None otherwise.
    The live tick first, then one throttled Kite quote call (app.market.kite)."""
    global _no_kite_until
    import time as _t
    live = feed.live_price(symbol)
    if live is not None:
        return live
    from app.market import kite as K
    kd = K.current()
    if kd is not None:
        key = _KITE_INDEX.get(symbol, K.key_for(symbol))
        try:
            got = kd.ohlc([key]).get(key) or {}
            return float(got["last_price"]) if got.get("last_price") else None
        except K.KiteDataError as e:
            log.debug("kite price for %s failed: %s", symbol, e)
            return None
    if _t.monotonic() < _no_kite_until:
        return None
    try:
        from app.live import session
        b = session.any_broker()
        if b is None:
            _no_kite_until = _t.monotonic() + 60
            return None
        key = _KITE_INDEX.get(symbol, f"NSE:{symbol}")
        return b.ltp([key]).get(key)
    except Exception as e:  # noqa: BLE001 - Yahoo is the fallback
        log.debug("kite price for %s failed: %s", symbol, e)
        _no_kite_until = _t.monotonic() + 60
        return None


def last_price(symbol: str) -> float | None:
    k = kite_price(symbol)
    if k is not None:
        return k
    try:
        q = yahoo.quotes([symbol])
        return q[0].last if q else None
    except yahoo.MarketDataError:
        return None


def exact_costs(inst: Instrument, side: str, qty: int, limit: float, stop: float) -> dict:
    """OrderRequest fields from Kite's margin API when logged in (exact margin and round-trip charges), else {}."""
    from app.risk import margin
    m = margin.for_order("NFO" if inst.kind == "future" else "NSE", inst.tradingsymbol, side, qty, limit, stop)
    if not m:
        return {}
    return {"margin_exact": m["margin"], "charges_exact": m["charges"]}


def size_for(entry: float, stop: float, lim: Limits, st: AccountState, inst: Instrument | None = None) -> int:
    """Quantity (shares, or lots x lot size) so that the loss at the stop, charges included, fits your
    max loss per trade, and the cash it blocks fits your free cash and your capital-in-use cap."""
    inst = inst or resolve("X")
    risk = abs(entry - stop)
    if risk <= 0:
        return 0
    lot = inst.lot_size
    lots = int(math.floor(lim.max_loss_per_trade / (risk * lot)))
    while lots > 0:
        q = lots * lot
        if q * risk + inst.charges(q * entry, q * stop).total <= lim.max_loss_per_trade:
            break
        lots -= 1
    per_lot_cash = entry * lot * inst.margin_pct / 100
    lots = min(lots, int(st.cash_free // per_lot_cash), int((st.capital * lim.max_margin_used_pct / 100) // per_lot_cash))
    return max(lots, 0) * lot


# ---------------------------------------------------------------- proposing and approving

TICKET_MINUTES = 15            # an unconfirmed ticket expires: its price and the market have moved on


def _waiting_twin(d, user_id: str, params: dict, keys: tuple[str, ...]) -> dict | None:
    """A ticket for the same order, still waiting for you: clicking again returns it instead of a duplicate."""
    since = _iso(_now() - dt.timedelta(minutes=TICKET_MINUTES))
    for o in d.select("orders", {"user_id": eq(user_id), "mode": eq("paper"), "state": eq("proposed"),
                                 "created_at": f"gte.{since}"}, columns="*"):
        if all(o["params"].get(k) == params.get(k) for k in keys):
            return o
    return None


def expire_stale_proposals(now: dt.datetime) -> int:
    """Tickets nobody confirmed within TICKET_MINUTES expire (TradingView tickets have their own rule)."""
    d = db()
    cutoff = _iso(now - dt.timedelta(minutes=TICKET_MINUTES))
    stale = d.select("orders", {"mode": eq("paper"), "state": eq("proposed"), "created_at": f"lt.{cutoff}"}, columns="id,params")
    n = 0
    for o in stale:
        if (o.get("params") or {}).get("source") == "tradingview":
            continue
        d.update("orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now)})
        n += 1
    return n


def propose(user_id: str, email: str | None, *, symbol: str, side: str, stop: float, target: float | None,
            qty: int | None = None, limit: float | None = None, source: str = "manual",
            card: dict | None = None) -> dict:
    """Make a ticket: price, size and the risk verdict. Nothing is placed until approve()."""
    with _lock:
        acct, lim_row = ensure_account(user_id, email)
        lim = Limits.from_row(lim_row)
        st, _ = account_state(user_id, acct)
        inst = resolve(symbol, _now().date())
        last = last_price(inst.data_symbol)
        long = side == "buy"
        if limit is None:
            if last is None:
                raise PaperError(f"no price for {symbol} right now")
            limit = round_to_tick(last * (1 + (ENTRY_BUFFER_PCT if long else -ENTRY_BUFFER_PCT) / 100),
                                  inst.tick(last), up=long)
        stop = round_to_tick(stop, inst.tick(stop), up=not long)
        if target is not None:
            target = round_to_tick(target, inst.tick(target), up=not long)
        if qty is None:
            qty = size_for(limit, stop, lim, st, inst)
        req = OrderRequest(symbol=inst.tradingsymbol, side=side, qty=int(qty), limit=limit, stop=stop, target=target,
                           kind=inst.kind, lot_size=inst.lot_size, margin_pct=inst.margin_pct, index=inst.is_index,
                           live_prices=kite_price(inst.data_symbol) is not None,
                           **(exact_costs(inst, side, int(qty), limit, stop) if qty > 0 else {}))
        verdict = check(req, lim, st, _now(), last) if qty > 0 else None
        if verdict is None:
            unit = f"a lot of {inst.lot_size}" if inst.kind == "future" else "one share"
            verdict_d = {"verdict": "BLOCK", "reasons": [f"the stop is too far for your max loss per trade: {unit} would lose more"],
                         "warnings": [], "numbers": {}}
        else:
            verdict_d = verdict.to_dict()

        d = db()
        signal_id = None
        if card:
            exp = card.get("explanation") or {}
            sig = d.insert("signals", {
                "user_id": user_id, "source": "tradingview" if source == "tradingview" else "strategy",
                "symbol": symbol, "side": side,
                "entry": exp.get("plan", {}).get("entry"), "stop": stop, "target": target,
                "context": {"strategy_id": card.get("strategy_id"), "strategy_version": card.get("strategy_version"),
                            "signal_time": card.get("created_at")},
                "explanation": exp, "status": "new"})
            signal_id = sig[0]["id"]
        params = {"symbol": inst.tradingsymbol, "app_symbol": inst.symbol, "data_symbol": inst.data_symbol, "kind": inst.kind,
                  "lot_size": inst.lot_size, "margin_pct": inst.margin_pct,
                  "expiry": inst.expiry.isoformat() if inst.expiry else None, "price_is_proxy": inst.price_is_proxy,
                  "side": side, "qty": int(qty), "type": "LIMIT", "limit": limit, "stop": stop,
                  "target": target, "leg": "entry", "source": source, "last_price": last,
                  "strategy": (card or {}).get("strategy_id"), "strategy_version": (card or {}).get("strategy_version"),
                  "manage": (((card or {}).get("explanation") or {}).get("plan") or {}).get("management")}
        same = _waiting_twin(d, user_id, params, ("symbol", "side", "stop", "target", "qty"))
        if same:
            return same
        order = d.insert("orders", {"user_id": user_id, "signal_id": signal_id, "account_id": acct["id"], "mode": "paper",
                                    "params": params, "state": "proposed", "risk_verdict": verdict_d})[0]
        audit(user_id, "user", "propose", {"order_id": order["id"], "params": params, "verdict": verdict_d["verdict"]})
        return order


def propose_structure(user_id: str, email: str | None, *, underlying: str, template: str, params: dict | None = None,
                      expiry: dt.date | None = None, exit_overrides: dict | None = None, source: str = "manual",
                      card: dict | None = None) -> dict:
    """A ticket for an option structure: legs priced from the chain, exits planned, risk verdict. Nothing is placed."""
    with _lock:
        acct, lim_row = ensure_account(user_id, email)
        lim = Limits.from_row(lim_row)
        st, _ = account_state(user_id, acct)
        try:
            c = ch.chain(underlying, expiry)
            s_ = S.build(c, template, params, _now())
        except (ch.ChainError, ValueError) as e:
            raise PaperError(str(e))
        mg = (((card or {}).get("explanation") or {}).get("plan") or {}).get("management")
        if mg and (exit_overrides or {}).get("underlying_stop") is not None:   # the signal's rules, on the index levels
            exit_overrides = {**exit_overrides, "manage": mg}
        exits = paper_options.plan_exits(s_, exit_overrides)
        from app.risk.engine import check_structure
        v = check_structure(paper_options.request_for(s_, exits), lim, st, _now()).to_dict()
        d = db()
        signal_id = None
        if card:
            exp = card.get("explanation") or {}
            signal_id = d.insert("signals", {"user_id": user_id, "source": "strategy", "symbol": s_.label,
                                             "side": "sell" if s_.net_premium > 0 else "buy",
                                             "context": {"strategy_id": card.get("strategy_id"), "signal_time": card.get("created_at")},
                                             "explanation": exp, "status": "new"})[0]["id"]
        params_out = {"kind": "option", "leg": "entry", "symbol": s_.label, "data_symbol": s_.underlying, "qty": s_.lots,
                      "side": "sell" if s_.net_premium > 0 else "buy", "source": source, "template": template,
                      "build_params": params or {}, "exit_overrides": exit_overrides or {}, "exits": exits,
                      "structure": {k: v_ for k, v_ in s_.to_dict().items() if k != "payoff"},
                      "strategy": (card or {}).get("strategy_id"), "strategy_version": (card or {}).get("strategy_version")}
        same = _waiting_twin(d, user_id, params_out, ("symbol", "template", "qty", "exit_overrides"))
        if same:
            return {**same, "payoff": s_.payoff}
        order = d.insert("orders", {"user_id": user_id, "signal_id": signal_id, "account_id": acct["id"], "mode": "paper",
                                    "params": params_out, "state": "proposed", "risk_verdict": v})[0]
        audit(user_id, "user", "propose_option", {"order_id": order["id"], "label": s_.label, "verdict": v["verdict"]})
        return {**order, "payoff": s_.payoff}


def approve(user_id: str, email: str | None, order_id: str, override: bool = False, reason: str = "") -> dict:
    """Your confirmation. The risk check runs again: things may have changed since the ticket.

    `override` (paper only) takes a ticket the risk check blocked for one of YOUR limits. Hard blocks (malformed
    order, market closed, kill switch, setup already failed) are never lifted. The trade is recorded as
    "rules not followed", with the reasons, and still counts toward your daily and monthly losses."""
    with _lock:
        d = db()
        rows = d.select("orders", {"id": eq(order_id), "user_id": eq(user_id)})
        if not rows:
            raise PaperError("no such order")
        o = rows[0]
        if o["state"] != "proposed":
            raise PaperError(f"this order is {o['state']}, not waiting for approval")
        p = o["params"]
        acct, lim_row = ensure_account(user_id, email)
        st, _ = account_state(user_id, acct)
        if p.get("kind") == "option":
            try:
                s_now, exits, v = paper_options.check(p, Limits.from_row(lim_row), st, _now())
            except (ch.ChainError, ValueError) as e:
                raise PaperError(f"cannot price the option legs right now: {e}")
            p = {**p, "structure": {k: v_ for k, v_ in s_now.to_dict().items() if k != "payoff"}, "exits": exits}
        else:
            sp = spec(p)
            try:
                inst = resolve(p.get("app_symbol") or p["symbol"], _now().date())
                exact = exact_costs(inst, p["side"], int(p["qty"]), float(p["limit"]), float(p["stop"])) \
                    if inst.tradingsymbol == p["symbol"] else {}
            except ValueError:
                exact = {}
            v = check(OrderRequest(p["symbol"], p["side"], p["qty"], p["limit"], p["stop"], p["target"], kind=sp["kind"],
                                   lot_size=sp["lot_size"], margin_pct=sp["margin_pct"],
                                   index=sp["data_symbol"] in ("NIFTY", "BANKNIFTY", "^NSEI", "^NSEBANK"),
                                   live_prices=kite_price(sp["data_symbol"]) is not None, **exact),
                      Limits.from_row(lim_row), st, _now(), last_price(sp["data_symbol"])).to_dict()
        now = _now()
        if v["verdict"] != "PASS":
            hard = [r for r in v["reasons"] if is_hard(r)]
            if override and hard:
                raise PaperError("cannot take this even on paper: " + "; ".join(hard))
            if not override:
                out = d.update("orders", {"id": eq(order_id)}, {"state": "rejected", "risk_verdict": v, "updated_at": _iso(now)})
                audit(user_id, "system", "blocked_at_approval", {"order_id": order_id, "reasons": v["reasons"]})
                return out[0]
            p = {**p, "override": {"reasons": v["reasons"], "note": reason.strip()[:300], "at": _iso(now)}}
            v = {**v, "overridden": True}
            audit(user_id, "user", "risk_override", {"order_id": order_id, "reasons": v["reasons"], "note": reason.strip()[:300]})
        p = {**p, "placed_t": int(now.timestamp())}
        out = d.update("orders", {"id": eq(order_id)}, {"state": "working", "params": p, "risk_verdict": v,
                                                       "approved_at": _iso(now), "updated_at": _iso(now)})
        if o.get("signal_id"):
            d.update("signals", {"id": eq(o["signal_id"])}, {"status": "ordered"})
        audit(user_id, "user", "approve", {"order_id": order_id})
        return out[0]


def reject(user_id: str, order_id: str) -> dict:
    with _lock:
        d = db()
        rows = d.select("orders", {"id": eq(order_id), "user_id": eq(user_id)})
        if not rows or rows[0]["state"] not in ("proposed", "working"):
            raise PaperError("nothing to cancel")
        state = "rejected" if rows[0]["state"] == "proposed" else "cancelled"
        out = d.update("orders", {"id": eq(order_id)}, {"state": state, "updated_at": _iso(_now())})
        if rows[0].get("signal_id") and state == "rejected":
            d.update("signals", {"id": eq(rows[0]["signal_id"])}, {"status": "rejected"})
        audit(user_id, "user", state, {"order_id": order_id})
        return out[0]


def close_position(user_id: str, trade_id: str, reason: str = "manual") -> dict:
    """An exit order for an open trade. Exits are never blocked; it fills at the next bar's open."""
    with _lock:
        d = db()
        rows = d.select("trades", {"id": eq(trade_id), "user_id": eq(user_id)})
        if not rows or rows[0]["exit_ts"]:
            raise PaperError("no such open position")
        t = rows[0]
        if t.get("mode") == "live":
            raise PaperError("this is a live position: close it from the Live page")
        existing = d.select("orders", {"user_id": eq(user_id), "state": eq("working")}, columns="id,params")
        if any(o["params"].get("trade_id") == trade_id for o in existing):
            raise PaperError("an exit for this position is already working")
        acct, _ = ensure_account(user_id, None)
        params = {**spec(t["entry_context"], t["symbol"]), "symbol": t["symbol"],
                  "side": "sell" if t["side"] == "buy" else "buy", "qty": open_qty(t), "type": "MARKET-SIM",
                  "leg": "exit", "trade_id": trade_id, "reason": reason, "placed_t": int(_now().timestamp())}
        o = d.insert("orders", {"user_id": user_id, "account_id": acct["id"], "mode": "paper", "params": params,
                                "state": "working", "approved_at": _iso(_now()),
                                "risk_verdict": {"verdict": "PASS", "reasons": [], "warnings": ["exits are never blocked"], "numbers": {}}})
        audit(user_id, "user", "close_position", {"trade_id": trade_id, "reason": reason})
        return o[0]


def kill_switch(user_id: str) -> dict:
    """Cancel every working entry, close every open position, no new entries until tomorrow."""
    with _lock:
        d = db()
        d.insert("risk_events", {"user_id": user_id, "kind": "kill_switch", "detail": {"by": "user"}})
        cancelled = 0
        for o in d.select("orders", {"user_id": eq(user_id), "mode": eq("paper"), "state": eq("working")}, columns="id,params"):
            if o["params"].get("leg") == "entry":
                d.update("orders", {"id": eq(o["id"])}, {"state": "cancelled", "updated_at": _iso(_now())})
                cancelled += 1
        closing = 0
        for t in d.select("trades", {"user_id": eq(user_id), "mode": eq("paper"), "exit_ts": "is.null"}, columns="id"):
            try:
                close_position(user_id, t["id"], reason="kill switch")
                closing += 1
            except PaperError:
                pass
        audit(user_id, "user", "kill_switch", {"cancelled": cancelled, "closing": closing})
    out = {"cancelled_orders": cancelled, "closing_positions": closing}
    try:                                         # one kill switch for everything: live positions too
        from app.live import service as live
        out["live"] = live.kill_switch(user_id)
    except Exception as e:  # noqa: BLE001 - before migration 0005 there is nothing live
        log.debug("live kill switch skipped: %s", e)
    return out


# ---------------------------------------------------------------- the simulator loop

def _bars_1m(symbol: str, cache: dict) -> list:
    if symbol not in cache:
        try:
            cache[symbol] = [b for b in feed.candles(symbol, "1m", "1d").bars if b.complete]
        except yahoo.MarketDataError as e:
            log.warning("no 1m bars for %s: %s", symbol, e)
            cache[symbol] = []
    return cache[symbol]


def _close_trade(t: dict, fill: sim.Fill, reason: str, ctx: dict) -> None:
    d = db()
    long = t["side"] == "buy"
    qty, entry = t["qty"], float(t["entry"])
    part = (ctx.get("manage") or {}).get("partial")
    legs = [(int(part["qty"]), float(part["price"])), (qty - int(part["qty"]), fill.price)] if part else [(qty, fill.price)]
    gross = costs = 0.0
    for q, px in legs:
        buy_v, sell_v = (entry * q, px * q) if long else (px * q, entry * q)
        gross += (px - entry) * q if long else (entry - px) * q
        costs += charges_for(spec(ctx)["kind"], buy_v, sell_v)
    costs = round(costs, 2)
    net = gross - costs
    risk = float(ctx.get("risk_amount") or 0)
    d.update("trades", {"id": eq(t["id"])}, {
        "exit_ts": _iso(fill.t), "exit": fill.price, "gross_pnl": round(gross, 2), "costs": costs,
        "net_pnl": round(net, 2), "r_multiple": round(net / risk, 3) if risk else None, "exit_reason": reason,
        "mae": ctx.get("mae"), "mfe": ctx.get("mfe"), "entry_context": ctx})
    audit(t["user_id"], "system", "trade_closed", {"trade_id": t["id"], "reason": reason, "net": round(net, 2)})
    from app.journal import postmortem          # imported here: the journal depends on the AI client, not the other way
    postmortem.schedule(t["id"])


def _after_close(t: dict, upd: dict) -> None:
    audit(t["user_id"], "system", "trade_closed", {"trade_id": t["id"], "reason": upd["exit_reason"], "net": upd["net_pnl"]})
    from app.journal import postmortem
    postmortem.schedule(t["id"])


def _management_events(t: dict, ctx: dict, ec: sim.ExitCheck) -> None:
    """A partial exit fills (recorded as a fill with its own charges) and the stop moves: both audited,
    so the monitor and the journal can show them."""
    if ec.partial:
        pt = ec.partial
        long = t["side"] == "buy"
        entry = float(t["entry"])
        buy_v, sell_v = (entry * pt["qty"], pt["price"] * pt["qty"]) if long else (pt["price"] * pt["qty"], entry * pt["qty"])
        try:
            db().insert("fills", {"order_id": ctx.get("order_id"), "user_id": t["user_id"], "ts": _iso(pt["t"]),
                                  "qty": pt["qty"], "price": pt["price"],
                                  "costs": {"partial_exit": charges_for(spec(ctx)["kind"], buy_v, sell_v)}})
        except Exception as e:  # noqa: BLE001 - the trade still records the partial in its entry_context
            log.warning("partial fill row failed for %s: %s", t["id"], e)
        audit(t["user_id"], "system", "partial_filled", {"trade_id": t["id"], **pt})
    for mv in ec.moves:
        audit(t["user_id"], "system", "stop_moved", {"trade_id": t["id"], **mv})


def tick(now: dt.datetime | None = None) -> dict:
    """One pass: fill or expire working orders, then manage open trades. Safe to call any time."""
    now = now or _now()
    d = db()
    stats = {"filled": 0, "expired": 0, "closed": 0}
    with _lock:
        cache: dict = {}
        try:
            from app.webhooks.tradingview import expire_stale_tickets
            stats["expired"] += expire_stale_tickets(now)        # unreviewed TradingView tickets go stale
            stats["expired"] += expire_stale_proposals(now)      # and so do every other unconfirmed ticket
        except Exception as e:  # noqa: BLE001 - before migration 0004 there is nothing to expire
            log.debug("ticket expiry skipped: %s", e)
        working = d.select("orders", {"mode": eq("paper"), "state": eq("working")})
        limits_cache: dict[str, Limits] = {}

        def limits_of(uid: str) -> Limits:
            if uid not in limits_cache:
                rows = d.select("risk_limits", {"user_id": eq(uid)})
                limits_cache[uid] = Limits.from_row(rows[0] if rows else {})
            return limits_cache[uid]

        for o in working:
            p = o["params"]
            if p.get("kind") == "option" and p.get("leg") == "entry":
                try:
                    trade = paper_options.fill_order(o, now)
                except (ch.ChainError, ValueError) as e:
                    log.warning("option fill failed for %s: %s", o["id"], e)
                    trade = None
                if trade:
                    audit(o["user_id"], "system", "entry_filled", {"order_id": o["id"], "label": p["symbol"]})
                    stats["filled"] += 1
                elif sim.entry_expired(int(p.get("placed_t") or now.timestamp()), now, EXPIRE_MINUTES,
                                       limits_of(o["user_id"]).entry_end):
                    d.update("orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now)})
                    stats["expired"] += 1
                continue
            if p.get("kind") == "option" and p.get("leg") == "exit":
                trades = d.select("trades", {"id": eq(p["trade_id"])})
                if trades and not trades[0]["exit_ts"] and market_status(now).is_open:
                    upd = paper_options.manage_trade(trades[0], now, limits_of(o["user_id"]).flat_by,
                                                     force_reason=p.get("reason", "manual"))
                    if upd:
                        d.update("orders", {"id": eq(o["id"])}, {"state": "filled", "updated_at": _iso(now)})
                        _after_close(trades[0], upd)
                        stats["closed"] += 1
                elif not trades or trades[0]["exit_ts"]:
                    d.update("orders", {"id": eq(o["id"])}, {"state": "cancelled", "updated_at": _iso(now)})
                continue
            bars = _bars_1m(spec(p)["data_symbol"], cache)
            if p.get("leg") == "exit":
                nxt = next((b for b in bars if b.t >= p["placed_t"]), None)
                if nxt is None:
                    continue
                trades = d.select("trades", {"id": eq(p["trade_id"])})
                if not trades or trades[0]["exit_ts"]:
                    d.update("orders", {"id": eq(o["id"])}, {"state": "cancelled", "updated_at": _iso(now)})
                    continue
                t = trades[0]
                price = round(nxt.o * (1 - 2 / 1e4) if t["side"] == "buy" else nxt.o * (1 + 2 / 1e4), 2)
                fill = sim.Fill(nxt.t, price, p.get("reason", "manual"))
                d.insert("fills", {"order_id": o["id"], "user_id": o["user_id"], "ts": _iso(fill.t), "qty": p["qty"],
                                   "price": fill.price})
                d.update("orders", {"id": eq(o["id"])}, {"state": "filled", "updated_at": _iso(now)})
                _close_trade(t, fill, p.get("reason", "manual"), {**(t["entry_context"] or {})})
                stats["closed"] += 1
                continue
            fill = sim.entry_fill(p["side"], float(p["limit"]), int(p["placed_t"]), bars)
            if fill is None:
                if sim.entry_expired(int(p["placed_t"]), now, EXPIRE_MINUTES, limits_of(o["user_id"]).entry_end):
                    d.update("orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now)})
                    audit(o["user_id"], "system", "expired", {"order_id": o["id"]})
                    stats["expired"] += 1
                continue
            risk_amount = abs(fill.price - float(p["stop"])) * p["qty"]
            charges = charges_for(spec(p)["kind"], fill.price * p["qty"], 0)
            d.insert("fills", {"order_id": o["id"], "user_id": o["user_id"], "ts": _iso(fill.t), "qty": p["qty"],
                               "price": fill.price, "costs": {"entry_side": charges}})
            d.update("orders", {"id": eq(o["id"])}, {"state": "filled", "updated_at": _iso(now)})
            sig = d.select("signals", {"id": eq(o["signal_id"])}, columns="explanation") if o.get("signal_id") else []
            exp = sig[0]["explanation"] if sig else None
            mg = None
            if (p.get("manage") or {}).get("rules"):
                inst_risk = abs(fill.price - float(p["stop"]))
                mg = manage.start(p["manage"]["rules"], long=p["side"] == "buy", entry=fill.price, stop=float(p["stop"]),
                                  qty=int(p["qty"]), lot=int(p.get("lot_size") or 1), atr=p["manage"].get("atr"),
                                  entry_t=fill.t, tick=(index_derivative_tick if spec(p)["kind"] == "future"
                                                         else tick_for)(fill.price),
                                  target_r=abs(float(p["target"]) - fill.price) / inst_risk
                                  if p.get("target") is not None and inst_risk else None)
            d.insert("trades", {
                "user_id": o["user_id"], "mode": "paper", "signal_id": o.get("signal_id"), "symbol": p["symbol"],
                "side": p["side"], "qty": p["qty"], "entry_ts": _iso(fill.t), "entry": fill.price,
                "rules_followed": not p.get("override"),
                "entry_context": {**spec(p), "expiry": p.get("expiry"), "price_is_proxy": p.get("price_is_proxy", False),
                                  "override": p.get("override"),
                                  "order_id": o["id"], "stop": p["stop"], "initial_stop": p["stop"], "target": p["target"],
                                  "manage": mg, "risk_amount": round(risk_amount, 2), "strategy": p.get("strategy"),
                                  "strategy_version": p.get("strategy_version"), "source": p.get("source"),
                                  "summary": (exp or {}).get("summary"), "last_checked": fill.t - 60, "mae": 0, "mfe": 0}})
            audit(o["user_id"], "system", "entry_filled", {"order_id": o["id"], "price": fill.price})
            stats["filled"] += 1

        for t in d.select("trades", {"mode": eq("paper"), "exit_ts": "is.null"}):
            ctx = dict(t["entry_context"] or {})
            if ctx.get("kind") == "option":
                if not market_status(now).is_open:
                    continue
                try:
                    upd = paper_options.manage_trade(t, now, limits_of(t["user_id"]).flat_by)
                except (ch.ChainError, ValueError) as e:
                    log.warning("option mark failed for %s: %s", t["id"], e)
                    continue
                if upd:
                    _after_close(t, upd)
                    stats["closed"] += 1
                continue
            bars = _bars_1m(spec(ctx, t["symbol"])["data_symbol"], cache)
            lim = limits_of(t["user_id"])
            mg = dict(ctx["manage"]) if ctx.get("manage") else None
            ec = sim.exit_check(t["side"], float(t["entry"]), float(ctx["stop"]),
                                float(ctx["target"]) if ctx.get("target") is not None else None,
                                int(ctx.get("last_checked", 0)), bars, lim.flat_by,
                                float(ctx.get("mae") or 0), float(ctx.get("mfe") or 0), m=mg)
            ctx.update(last_checked=ec.last_t, mae=round(ec.mae, 2), mfe=round(ec.mfe, 2))
            if mg is not None:
                ctx.update(manage=mg, stop=mg["stop"])
                _management_events(t, ctx, ec)
            if ec.fill is None and not market_status(now).is_open and now.time() >= dt.time(15, 30) and bars:
                last = bars[-1]       # the session ended without a 15:15 bar in the data: close at the last close
                ec = sim.ExitCheck(sim.Fill(last.t, last.c, "session-end"), ec.mae, ec.mfe, last.t)
            if ec.fill is not None:
                _close_trade(t, ec.fill, ec.fill.reason, ctx)
                stats["closed"] += 1
            else:
                d.update("trades", {"id": eq(t["id"])}, {"entry_context": ctx, "mae": ctx["mae"], "mfe": ctx["mfe"]})
    return stats


# ---------------------------------------------------------------- overview for the page

def overview(user_id: str, email: str | None) -> dict:
    acct, lim_row = ensure_account(user_id, email)
    st, raw = account_state(user_id, acct)
    d = db()
    orders = d.select("orders", {"user_id": eq(user_id), "mode": eq("paper")}, order="created_at.desc", limit=50)
    trades = sorted(raw["trades"], key=lambda t: t["entry_ts"] or "", reverse=True)
    full = {t["id"]: t for t in d.select("trades", {"user_id": eq(user_id), "mode": eq("paper")}, order="entry_ts.desc", limit=200)}
    open_syms = sorted({spec(t["entry_context"], t["symbol"])["data_symbol"] for t in trades if not t["exit_ts"]})
    quotes = {}
    if open_syms:
        try:
            quotes = {q.symbol: q.last for q in feed.quotes(open_syms)}
        except yahoo.MarketDataError:
            pass
    positions = []
    for t in (full[x["id"]] for x in trades if not x["exit_ts"] and x["id"] in full):
        ctx = t["entry_context"] or {}
        if ctx.get("kind") == "option":
            try:
                unreal = paper_options.unrealized(ctx, ch.chain(ctx["underlying"], dt.date.fromisoformat(ctx["expiry"])))
            except (ch.ChainError, ValueError):
                unreal = None
            last = None
            try:
                legs = ctx.get("legs") or []
                if len(legs) == 1:                       # one leg: its live bid is what you would sell at
                    q = ch.chain(ctx["underlying"], dt.date.fromisoformat(ctx["expiry"])).quote(float(legs[0]["strike"]), legs[0]["kind"])
                    last = (q.bid if legs[0]["side"] == "buy" else q.ask) if q else None
            except (ch.ChainError, ValueError, KeyError):
                pass
            ex = ctx["exits"]
            positions.append({**t, "last": last, "unrealized": unreal, "stop": ex["stop_pnl"], "target": ex["target_pnl"],
                              "underlying_stop": ex.get("underlying_stop"), "underlying_target": ex.get("underlying_target")})
            continue
        last = quotes.get(spec(ctx, t["symbol"])["data_symbol"])
        long = t["side"] == "buy"
        oq = open_qty(t)
        unreal = ((last - float(t["entry"])) if long else (float(t["entry"]) - last)) * oq if last else None
        part = (ctx.get("manage") or {}).get("partial")
        if unreal is not None and part:         # the booked share's gross P&L
            unreal += ((part["price"] - float(t["entry"])) if long else (float(t["entry"]) - part["price"])) * part["qty"]
        positions.append({**t, "last": last, "unrealized": round(unreal, 2) if unreal is not None else None,
                          "stop": ctx.get("stop"), "target": ctx.get("target"), "initial_stop": ctx.get("initial_stop"),
                          "open_qty": oq, "stop_moves": (ctx.get("manage") or {}).get("moves") or []})
    lim = Limits.from_row(lim_row)
    return {
        "account": {"id": acct["id"], "starting_capital": float(acct["starting_capital"] or 0), "capital": st.capital,
                    "cash_free": st.cash_free, "realized_today": st.realized_today, "realized_month": st.realized_month,
                    "open_risk": round(st.open_risk, 2), "entries_today": st.entries_today,
                    "consecutive_losses": st.consecutive_losses, "kill_switch": st.kill_switch},
        "limits": {k: (v.strftime("%H:%M") if isinstance(v, dt.time) else v) for k, v in lim.__dict__.items()},
        "fno_limit_available": "max_loss_per_trade_fno" in lim_row,     # migration 0003 adds the column
        "positions": positions,
        "orders": orders,
        "trades": [full[t["id"]] for t in trades if t["exit_ts"] and t["id"] in full][:100],
        "market": {"is_open": market_status().is_open},
    }


def update_limits(user_id: str, email: str | None, values: dict) -> dict:
    allowed = {"max_loss_per_trade", "max_daily_loss", "max_monthly_loss", "max_trades_per_day",
               "max_consecutive_losses", "max_open_positions", "max_margin_used_pct", "entry_start", "entry_end", "flat_by",
               "max_loss_per_trade_fno", "allow_naked_short_options"}
    bad = set(values) - allowed
    if bad:
        raise PaperError(f"unknown limit(s): {', '.join(sorted(bad))}")
    ensure_account(user_id, email)
    for k in ("entry_start", "entry_end", "flat_by"):
        if k in values:
            t = dt.time.fromisoformat(str(values[k])[:5])
            if not (dt.time(9, 15) <= t <= dt.time(15, 30)):
                raise PaperError(f"{k} must be inside market hours (9:15 to 15:30)")
    if "flat_by" in values and dt.time.fromisoformat(str(values["flat_by"])[:5]) > dt.time(15, 20):
        raise PaperError("flat_by must be 15:20 or earlier (Zerodha squares off MIS from 15:25)")
    for k, v in values.items():
        if k == "allow_naked_short_options":
            if not isinstance(v, bool):
                raise PaperError("allow_naked_short_options must be true or false")
        elif k == "max_loss_per_trade_fno" and v is None:
            continue                                     # back to "same as stocks"
        elif k not in ("entry_start", "entry_end", "flat_by") and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            raise PaperError(f"{k} must be a positive number")
    out = db().update("risk_limits", {"user_id": eq(user_id)}, {**values, "updated_at": _iso(_now())})
    audit(user_id, "user", "update_limits", values)
    return out[0]


class Loop:
    """Runs tick() every `every` seconds while the market is open (and once after the close)."""

    def __init__(self, every: float = 20.0):
        self.every, self._stop = every, threading.Event()
        self.last: dict = {}
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="paper-loop")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = _now()
                phase = market_status(now).phase
                if phase == "open" or (phase == "post-close" and now.time() < dt.time(16, 0)):
                    self.last = {"at": now.isoformat(timespec="seconds"), **tick(now)}
                else:
                    self.last = {"at": now.isoformat(timespec="seconds"), "idle": f"market {phase}"}
            except Exception as e:  # noqa: BLE001 - keep the loop alive, log the problem
                log.exception("paper tick failed: %s", e)
                self.last = {"at": _now().isoformat(timespec="seconds"), "error": str(e)}
            self._stop.wait(self.every)
