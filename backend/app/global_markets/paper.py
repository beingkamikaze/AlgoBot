"""Global paper trading in dollars (XAUUSD first): account, tickets, your confirmation, fills, exits and the kill switch.

Flow (assisted, like NSE): ticket -> risk check -> you confirm -> working -> filled -> open trade watched for its stop,
target and flat-by time -> closed. Nothing here talks to a broker (plan phase X8).

A trade is priced from one source for its whole life: real spot (Twelve Data) when the key is set, otherwise COMEX
futures (`GC=F`), labelled as such. Both give mid prices, so the bid and ask use an assumed spread (`costs.DEFAULT_SPREAD`).
Kept apart from the NSE tables: `global_accounts`, `global_limits`, `global_orders`, `global_trades` (migration 0006).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from app.db import db, eq
from app.global_markets import journal, macro, news, signals
from app.global_markets import paper_sim as sim
from app.global_markets.clock import IST, day_window, market_status, session_bounds, trading_day
from app.global_markets.costs import DEFAULT_SPREAD, CostModel
from app.global_markets.data import feed
from app.global_markets.instruments import resolve
from app.global_markets.risk import (AccountState, Limits, OpenPosition, OrderRequest, PriceInfo, check, is_hard,
                                     max_lots)

log = logging.getLogger("global.paper")
COSTS = CostModel()
QUOTES = sim.Quotes(spread=DEFAULT_SPREAD, slippage=COSTS.slippage)
ENTRY_BUFFER = 0.30             # dollars beyond the ask (a long) so the limit fills on an ordinary next bar
EXPIRE_SECONDS = 10 * 60
STARTING_CAPITAL = 10_000.0      # dollars: about your NSE paper capital
TICKET_MINUTES = 15             # an unconfirmed ticket expires: its price has moved on
STALE_EXIT_SECONDS = 45 * 60    # no bars for this long after flat-by, with the market shut: close at the last price
_lock = threading.RLock()


class PaperError(Exception):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime | int | float) -> str:
    if isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).isoformat()


listeners: list = []           # called as fn(kind, user_id, data) for "filled", "closed" and "expired" (Telegram registers here)


def _tell(kind: str, user_id: str, data: dict) -> None:
    for fn in listeners:
        try:
            fn(kind, user_id, data)
        except Exception as e:  # noqa: BLE001 - trading never waits on a notification
            log.warning("paper listener failed: %s", e)


def audit(user_id: str | None, action: str, detail: dict, actor: str = "user") -> None:
    try:
        db().insert("audit_log", {"user_id": user_id, "actor": actor, "action": f"global_{action}", "detail": detail})
    except Exception as e:  # noqa: BLE001 - the audit log must never break trading
        log.warning("audit log failed: %s", e)


# ---------------------------------------------------------------- prices

def bars(symbol: str, source: str) -> list:
    """1-minute bars from one source (cached by the feed, so the loop and the chart share Twelve Data calls)."""
    return feed.series(symbol, "1m", source).bars


def price_info(symbol: str, source: str, now: dt.datetime | None = None) -> PriceInfo:
    try:
        bs = [b for b in bars(symbol, source) if b.complete]
    except Exception as e:  # noqa: BLE001 - no price is a reason on the ticket, not a crash
        log.warning("no %s price for %s: %s", source, symbol, e)
        bs = []
    if not bs:
        return PriceInfo(None, None, source)
    now_s = (now or _now()).timestamp()
    return PriceInfo(bs[-1].c, max(0.0, now_s - (bs[-1].t + 60)), source)


def _headlines(symbol: str) -> list[dict]:
    try:
        return news.headlines(symbol)["headlines"]
    except Exception:  # noqa: BLE001 - a card without headlines is still a card
        return []


def _from_signal(signal: dict | None) -> dict:
    """The strategy name and its fixed-rule confidence, when a ticket comes from a live signal."""
    if not signal:
        return {}
    card = (signal.get("explanation") or {}).get("card") or {}
    return {"strategy": card.get("source") or signal["strategy_id"], "confidence": card.get("confidence")}


def _from_idea(idea: dict) -> dict:
    """An agent idea: named as untested, graded low (see `agent/desk.py`)."""
    from app.global_markets.agent.desk import card_source
    return card_source(idea)


def on_trade_closed(trade_id: str) -> None:
    """Write the trade's post-mortem in the background (replaced in tests)."""
    journal.schedule(trade_id)


def _flat_by_dt(now: dt.datetime, lim: Limits) -> dt.datetime | None:
    w = day_window(trading_day(now), lim.window())
    return w.flat_by if w else None


def _with_releases(v: dict, now: dt.datetime, lim: Limits) -> dict:
    """Add US release warnings (CPI, jobs, Fed) to a verdict. They warn; they never block."""
    extra = [w for w in macro.warnings_for(now, _flat_by_dt(now, lim)) if w not in v.get("warnings", [])]
    return {**v, "warnings": extra + list(v.get("warnings", []))} if extra else v


# ---------------------------------------------------------------- account

def ensure_account(user_id: str) -> tuple[dict, dict]:
    d = db()
    acct = d.select("global_accounts", {"user_id": eq(user_id)})
    if not acct:
        acct = d.insert("global_accounts", {"user_id": user_id, "currency": "USD", "starting_capital": STARTING_CAPITAL},
                        upsert_on="user_id")
    lim = d.select("global_limits", {"user_id": eq(user_id)})
    if not lim:
        lim = d.insert("global_limits", {"user_id": user_id}, upsert_on="user_id")
    return acct[0], lim[0]


def _day_start(day: dt.date) -> str:
    bounds = session_bounds(day)
    start = bounds[0] if bounds else dt.datetime.combine(day, dt.time(0, 0), dt.timezone.utc)
    return _iso(start)


def account_state(user_id: str, account: dict, now: dt.datetime | None = None) -> tuple[AccountState, dict]:
    now = now or _now()
    d = db()
    trades = d.select("global_trades", {"user_id": eq(user_id), "mode": eq("paper")}, order="entry_ts")
    working = d.select("global_orders", {"user_id": eq(user_id), "state": eq("working")})
    day = trading_day(now)
    month = day.strftime("%Y-%m")
    closed = [t for t in trades if t.get("exit_ts")]
    open_ = [t for t in trades if not t.get("exit_ts")]
    today = sorted((t for t in closed if t.get("trading_day") == day.isoformat()), key=lambda t: t.get("exit_ts"))
    streak = 0
    for t in reversed(today):
        if float(t["net_pnl"] or 0) < 0:
            streak += 1
        else:
            break
    entries = [o for o in working if o["leg"] == "entry"]
    kill = d.select("risk_events", {"user_id": eq(user_id), "kind": eq("global_kill_switch"), "ts": f"gte.{_day_start(day)}"},
                    limit=1)
    st = AccountState(
        capital=round(float(account.get("starting_capital") or 0) + sum(float(t["net_pnl"] or 0) for t in closed), 2),
        open_positions=[OpenPosition(t["symbol"], t["side"], float(t["units"]), float(t["entry"]),
                                     float((t["entry_context"] or {}).get("stop") or t["entry"])) for t in open_],
        working_value=round(sum(float(o["params"]["units"]) * float(o["params"]["limit"]) for o in entries), 2),
        entries_today=sum(1 for t in trades if t.get("trading_day") == day.isoformat()) + len(entries),
        realized_today=round(sum(float(t["net_pnl"] or 0) for t in today), 2),
        realized_month=round(sum(float(t["net_pnl"] or 0) for t in closed if (t.get("trading_day") or "").startswith(month)), 2),
        consecutive_losses=streak,
        kill_switch=bool(kill),
    )
    return st, {"trades": trades, "working": working}


# ---------------------------------------------------------------- tickets

def _waiting_twin(d, user_id: str, params: dict) -> dict | None:
    since = _iso(_now() - dt.timedelta(minutes=TICKET_MINUTES))
    for o in d.select("global_orders", {"user_id": eq(user_id), "state": eq("proposed"), "created_at": f"gte.{since}"}):
        if all(o["params"].get(k) == params.get(k) for k in ("symbol", "side", "stop", "target", "lots", "source")):
            return o
    return None


def propose(user_id: str, *, symbol: str, side: str, stop: float, target: float | None = None, lots: float | None = None,
            why: str = "", now: dt.datetime | None = None, target_r: float | None = None, signal: dict | None = None,
            idea: dict | None = None) -> dict:
    """Make a ticket: price, size and the risk verdict. Nothing is placed until approve().

    `target_r` sets the target from the ticket's own entry (a strategy's "2R"); `signal` is the live signal it came from;
    `idea` is the gold agent's idea it came from (graded low: untested)."""
    if side not in ("buy", "sell"):
        raise PaperError("side must be buy or sell")
    try:
        inst = resolve(symbol)
    except ValueError as e:
        raise PaperError(str(e))
    now = now or _now()
    with _lock:
        acct, lim_row = ensure_account(user_id)
        lim = Limits.from_row(lim_row)
        st, _ = account_state(user_id, acct, now)
        source = feed.default_source()
        px = price_info(inst.symbol, source, now)
        if px.last is None:
            raise PaperError(f"no {source} price for {inst.symbol} right now")
        long = side == "buy"
        limit = inst.round_price(px.last + (QUOTES.half + ENTRY_BUFFER) * (1 if long else -1), up=long)
        stop = inst.round_price(float(stop), up=not long)
        if target_r is not None:
            target = limit + target_r * (limit - stop)
        target = inst.round_price(float(target), up=not long) if target is not None else None
        sized = lots is None
        if sized:
            lots = max_lots(inst, limit, stop, lim, COSTS) if (stop < limit if long else stop > limit) else 0.0
        lots = round(float(lots), 8)
        if lots <= 0:
            v = {"verdict": "BLOCK", "reasons": ["the stop is too far for your max loss per trade: even "
                                                 f"{inst.min_lot:g} lot would lose more"] if sized else ["lots must be above zero"],
                 "warnings": [], "numbers": {}, "hard": [], "overridable": False}
        else:
            v = check(OrderRequest(inst, side, lots, limit, stop, target), lim, st, now, px, COSTS).to_dict()
        v = _with_releases(v, now, lim)
        params = {"symbol": inst.symbol, "side": side, "lots": lots, "units": inst.units(lots), "type": "LIMIT", "limit": limit,
                  "stop": stop, "target": target, "leg": "entry", "source": source, "last_price": px.last,
                  "price_age_seconds": round(px.age_seconds or 0), "spread": QUOTES.spread, "slippage": QUOTES.slippage,
                  "sized_by_app": sized, "why": why.strip()[:300]}
        if signal:
            params["signal_id"] = signal["id"]
        if idea:
            params["idea_id"] = idea["id"]
        d = db()
        same = _waiting_twin(d, user_id, params)
        if same:
            return same
        try:
            params["card"] = signals.card(
                symbol=inst.symbol, side=side, lots=lots, units=inst.units(lots), limit=limit, stop=stop, target=target,
                why=why, max_loss=(v.get("numbers") or {}).get("max_loss"), source=source, bars=bars(inst.symbol, source),
                now=now, flat_by=_flat_by_dt(now, lim), headlines=_headlines(inst.symbol), warnings=v.get("warnings"),
                **(_from_idea(idea) if idea else _from_signal(signal)))
        except Exception as e:  # noqa: BLE001 - the ticket stands without its card; the card says why it is missing
            log.warning("why card failed: %s", e)
            params["card"] = {"title": f"{'Buy' if side == 'buy' else 'Sell'} {inst.symbol}", "why": why.strip(),
                              "error": "the card could not be built from the latest bars"}
        o = d.insert("global_orders", {"user_id": user_id, "symbol": inst.symbol, "side": side, "lots": lots, "leg": "entry",
                                       "state": "proposed", "params": params, "risk_verdict": v})[0]
        audit(user_id, "propose", {"order_id": o["id"], "verdict": v["verdict"], "lots": lots, "limit": limit, "stop": stop})
        return o


def approve(user_id: str, order_id: str, override: bool = False, reason: str = "", now: dt.datetime | None = None) -> dict:
    """Your confirmation. The risk check runs again on a fresh price. `override` (paper only) takes a ticket blocked by one
    of YOUR limits; hard blocks are never lifted. It is recorded as rules not followed and still counts toward your losses."""
    now = now or _now()
    with _lock:
        d = db()
        rows = d.select("global_orders", {"id": eq(order_id), "user_id": eq(user_id)})
        if not rows:
            raise PaperError("no such order")
        o = rows[0]
        if o["state"] != "proposed":
            raise PaperError(f"this order is {o['state']}, not waiting for your confirmation")
        p = o["params"]
        acct, lim_row = ensure_account(user_id)
        st, _ = account_state(user_id, acct, now)
        px = price_info(p["symbol"], p["source"], now)
        v = check(OrderRequest(resolve(p["symbol"]), p["side"], float(p["lots"]), float(p["limit"]), float(p["stop"]),
                               float(p["target"]) if p.get("target") is not None else None),
                  Limits.from_row(lim_row), st, now, px, COSTS).to_dict()
        v = _with_releases(v, now, Limits.from_row(lim_row))
        if v["verdict"] != "PASS":
            hard = [r for r in v["reasons"] if is_hard(r)]
            if override and hard:
                raise PaperError("cannot take this even on paper: " + "; ".join(hard))
            if not override:
                out = d.update("global_orders", {"id": eq(order_id)}, {"state": "rejected", "risk_verdict": v, "updated_at": _iso(now)})
                audit(user_id, "blocked_at_approval", {"order_id": order_id, "reasons": v["reasons"]}, actor="system")
                return out[0]
            p = {**p, "override": {"reasons": v["reasons"], "note": reason.strip()[:300], "at": _iso(now)}}
            v = {**v, "overridden": True}
            audit(user_id, "risk_override", {"order_id": order_id, "reasons": v["reasons"], "note": reason.strip()[:300]})
        p = {**p, "placed_t": int(now.timestamp())}
        out = d.update("global_orders", {"id": eq(order_id)}, {"state": "working", "params": p, "risk_verdict": v,
                                                              "approved_at": _iso(now), "updated_at": _iso(now)})
        audit(user_id, "approve", {"order_id": order_id})
        return out[0]


def cancel(user_id: str, order_id: str) -> dict:
    with _lock:
        d = db()
        rows = d.select("global_orders", {"id": eq(order_id), "user_id": eq(user_id)})
        if not rows or rows[0]["state"] not in ("proposed", "working") or rows[0]["leg"] != "entry":
            raise PaperError("nothing to cancel")
        state = "rejected" if rows[0]["state"] == "proposed" else "cancelled"
        out = d.update("global_orders", {"id": eq(order_id)}, {"state": state, "updated_at": _iso(_now())})
        audit(user_id, state, {"order_id": order_id})
        return out[0]


def close_position(user_id: str, trade_id: str, reason: str = "manual", now: dt.datetime | None = None) -> dict:
    """An exit for an open trade: never blocked; it fills at the next bar's open."""
    now = now or _now()
    with _lock:
        d = db()
        rows = d.select("global_trades", {"id": eq(trade_id), "user_id": eq(user_id)})
        if not rows or rows[0].get("exit_ts"):
            raise PaperError("no such open position")
        t = rows[0]
        if any(o["trade_id"] == trade_id for o in d.select("global_orders", {"user_id": eq(user_id), "state": eq("working"),
                                                                              "leg": eq("exit")})):
            raise PaperError("an exit for this position is already working")
        params = {"symbol": t["symbol"], "side": "sell" if t["side"] == "buy" else "buy", "lots": float(t["lots"]),
                  "units": float(t["units"]), "type": "MARKET-SIM", "leg": "exit", "source": t["price_source"], "reason": reason,
                  "placed_t": int(now.timestamp())}
        o = d.insert("global_orders", {"user_id": user_id, "symbol": t["symbol"], "side": params["side"], "lots": t["lots"],
                                       "leg": "exit", "state": "working", "params": params, "trade_id": trade_id,
                                       "approved_at": _iso(now),
                                       "risk_verdict": {"verdict": "PASS", "reasons": [], "warnings": ["exits are never blocked"],
                                                        "numbers": {}}})[0]
        audit(user_id, "close_position", {"trade_id": trade_id, "reason": reason})
        return o


def kill_switch(user_id: str, now: dt.datetime | None = None) -> dict:
    """Cancel every ticket and working entry, close every open position, and no new entries until the next trading day
    (it rolls at 17:00 New York). This switch covers the global setup only; the NSE pages have their own."""
    now = now or _now()
    with _lock:
        d = db()
        d.insert("risk_events", {"user_id": user_id, "kind": "global_kill_switch", "ts": _iso(now), "detail": {"by": "user"}})
        cancelled = 0
        for o in d.select("global_orders", {"user_id": eq(user_id), "state": "in.(proposed,working)", "leg": eq("entry")}):
            d.update("global_orders", {"id": eq(o["id"])}, {"state": "cancelled", "updated_at": _iso(now)})
            cancelled += 1
        closing = 0
        for t in d.select("global_trades", {"user_id": eq(user_id), "exit_ts": "is.null"}):
            try:
                close_position(user_id, t["id"], reason="kill switch", now=now)
                closing += 1
            except PaperError:
                pass
        audit(user_id, "kill_switch", {"cancelled": cancelled, "closing": closing})
        return {"cancelled_orders": cancelled, "closing_positions": closing}


# ---------------------------------------------------------------- the loop's work

def _flat_by_t(day: dt.date, lim: Limits) -> int:
    w = day_window(day, lim.window())
    return int(w.flat_by.timestamp()) if w else 0


def _close(d, t: dict, fill: sim.Fill, ctx: dict) -> None:
    res = sim.pnl(t["side"], float(t["entry"]), fill.price, float(t["units"]), float(ctx.get("entry_slippage") or 0),
                  fill.slippage, sim.Quotes(float(ctx.get("spread") or QUOTES.spread), QUOTES.slippage))
    risk = float(ctx.get("risk_amount") or 0)
    d.update("global_trades", {"id": eq(t["id"])}, {
        **res, "exit_ts": _iso(fill.t), "exit": fill.price, "exit_reason": fill.reason,
        "r_multiple": round(res["net_pnl"] / risk, 3) if risk else None, "mae": ctx.get("mae"), "mfe": ctx.get("mfe"),
        "entry_context": ctx})
    audit(t["user_id"], "trade_closed", {"trade_id": t["id"], "reason": fill.reason, "net": res["net_pnl"]}, actor="system")
    _tell("closed", t["user_id"], {"symbol": t["symbol"], "side": t["side"], "price": fill.price, "reason": fill.reason,
                                   "net": res["net_pnl"], "r": round(res["net_pnl"] / risk, 2) if risk else None})
    try:
        on_trade_closed(t["id"])
    except Exception as e:  # noqa: BLE001 - the trade is closed whatever happens to its post-mortem
        log.warning("post-mortem not scheduled for %s: %s", t["id"], e)


def tick(now: dt.datetime | None = None) -> dict:
    """One pass: expire old tickets, fill or expire working orders, then manage open trades. Fetches prices only when
    there is something to watch, so Twelve Data's daily allowance is not spent on an idle account."""
    now = now or _now()
    d = db()
    stats = {"filled": 0, "expired": 0, "closed": 0}
    with _lock:
        cutoff = _iso(now - dt.timedelta(minutes=TICKET_MINUTES))
        for o in d.select("global_orders", {"state": eq("proposed"), "created_at": f"lt.{cutoff}"}):
            d.update("global_orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now)})
            stats["expired"] += 1
        working = d.select("global_orders", {"state": eq("working")}, order="created_at")
        open_trades = d.select("global_trades", {"mode": eq("paper"), "exit_ts": "is.null"})
        if not working and not open_trades:
            return stats
        cache: dict[tuple[str, str], list] = {}
        limits: dict[str, Limits] = {}

        def bars_for(symbol: str, source: str) -> list:
            if (symbol, source) not in cache:
                try:
                    cache[(symbol, source)] = [b for b in bars(symbol, source) if b.complete]
                except Exception as e:  # noqa: BLE001 - try again on the next pass
                    log.warning("no %s bars for %s: %s", source, symbol, e)
                    cache[(symbol, source)] = []
            return cache[(symbol, source)]

        def limits_of(uid: str) -> Limits:
            if uid not in limits:
                rows = d.select("global_limits", {"user_id": eq(uid)})
                limits[uid] = Limits.from_row(rows[0] if rows else {})
            return limits[uid]

        for o in working:
            p = o["params"]
            bs = bars_for(p["symbol"], p["source"])
            q = sim.Quotes(float(p.get("spread") or QUOTES.spread), float(p.get("slippage") or QUOTES.slippage))
            if o["leg"] == "exit":
                trades = d.select("global_trades", {"id": eq(o["trade_id"])})
                if not trades or trades[0].get("exit_ts"):
                    d.update("global_orders", {"id": eq(o["id"])}, {"state": "cancelled", "updated_at": _iso(now)})
                    continue
                t = trades[0]
                fill = sim.exit_now(t["side"], int(p["placed_t"]), bs, q, p.get("reason", "manual"))
                if fill is None:
                    continue
                d.update("global_orders", {"id": eq(o["id"])}, {"state": "filled", "updated_at": _iso(now)})
                _close(d, t, fill, dict(t["entry_context"] or {}))
                stats["closed"] += 1
                continue
            placed = int(p["placed_t"])
            day = trading_day(dt.datetime.fromtimestamp(placed, dt.timezone.utc))
            w = day_window(day, limits_of(o["user_id"]).window())
            end_t = int(w.entry_end.timestamp()) if w else placed
            if p.get("override") and placed >= end_t and w:
                end_t = int(w.flat_by.timestamp())      # you overrode the entry window: it may fill until flat-by
            # only bars inside the order's life can fill it: a delayed feed can deliver later bars in the same batch
            live = [b for b in bs if b.t < min(end_t, placed + EXPIRE_SECONDS)]
            fill = sim.entry_fill(p["side"], float(p["limit"]), placed, live, q)
            if fill is None:
                why = sim.entry_expired(placed, bs, EXPIRE_SECONDS, end_t)
                if why is None and now.timestamp() - placed > EXPIRE_SECONDS + STALE_EXIT_SECONDS:
                    why = "no new prices arrived"
                if why:
                    d.update("global_orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now),
                                                                    "params": {**p, "expired_because": why}})
                    audit(o["user_id"], "expired", {"order_id": o["id"], "why": why}, actor="system")
                    _tell("expired", o["user_id"], {"symbol": p["symbol"], "why": why})
                    stats["expired"] += 1
                continue
            stop = float(p["stop"])
            if (p["side"] == "buy" and fill.price <= stop) or (p["side"] == "sell" and fill.price >= stop):
                d.update("global_orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now),
                                                                "params": {**p, "expired_because": "price was already past the stop"}})
                stats["expired"] += 1
                continue
            units = float(p["units"])
            risk = units * (abs(fill.price - stop) + COSTS.round_trip_per_unit(resolve(p["symbol"]).contract_size))
            t = d.insert("global_trades", {
                "user_id": o["user_id"], "mode": "paper", "symbol": p["symbol"], "side": p["side"], "lots": p["lots"],
                "units": units, "price_source": p["source"], "entry_ts": _iso(fill.t), "entry": fill.price,
                "rules_followed": not p.get("override"), "trading_day": trading_day(dt.datetime.fromtimestamp(fill.t, dt.timezone.utc)).isoformat(),
                "entry_context": {"order_id": o["id"], "stop": stop, "target": p.get("target"), "limit": p["limit"],
                                  "spread": q.spread, "entry_slippage": fill.slippage, "risk_amount": round(risk, 2),
                                  "why": p.get("why"), "override": p.get("override"), "card": p.get("card"),
                                  "last_checked": fill.t - 60,
                                  "mae": 0, "mfe": 0}})[0]
            d.update("global_orders", {"id": eq(o["id"])}, {"state": "filled", "trade_id": t["id"], "updated_at": _iso(now)})
            audit(o["user_id"], "entry_filled", {"order_id": o["id"], "trade_id": t["id"], "price": fill.price}, actor="system")
            _tell("filled", o["user_id"], {"symbol": p["symbol"], "side": p["side"], "lots": float(p["lots"]),
                                           "price": fill.price, "stop": stop, "target": p.get("target")})
            stats["filled"] += 1

        exiting = {o["trade_id"] for o in d.select("global_orders", {"state": eq("working"), "leg": eq("exit")})}
        for t in d.select("global_trades", {"mode": eq("paper"), "exit_ts": "is.null"}):
            if t["id"] in exiting:
                continue
            ctx = dict(t["entry_context"] or {})
            bs = bars_for(t["symbol"], t["price_source"])
            q = sim.Quotes(float(ctx.get("spread") or QUOTES.spread), QUOTES.slippage)
            flat_t = _flat_by_t(dt.date.fromisoformat(t["trading_day"]), limits_of(t["user_id"]))
            ec = sim.exit_check(t["side"], float(t["entry"]), float(ctx["stop"]),
                                float(ctx["target"]) if ctx.get("target") is not None else None,
                                int(ctx.get("last_checked") or 0), bs, flat_t, q, float(ctx.get("mae") or 0),
                                float(ctx.get("mfe") or 0))
            ctx.update(last_checked=ec.last_t, mae=round(ec.mae, 2), mfe=round(ec.mfe, 2))
            fill = ec.fill
            if fill is None and bs and now.timestamp() > flat_t + STALE_EXIT_SECONDS and not market_status(now).is_open:
                last = bs[-1]          # the day ended without a bar at flat-by in the data: close at the last price
                long = t["side"] == "buy"
                fill = sim.Fill(last.t, round(last.c - q.half - q.slippage if long else last.c + q.half + q.slippage, 2),
                                "session-end", q.slippage)
            if fill is not None:
                _close(d, t, fill, ctx)
                stats["closed"] += 1
            else:
                d.update("global_trades", {"id": eq(t["id"])}, {"entry_context": ctx, "mae": ctx["mae"], "mfe": ctx["mfe"]})
    return stats


# ---------------------------------------------------------------- the page

def overview(user_id: str) -> dict:
    acct, lim_row = ensure_account(user_id)
    now = _now()
    st, raw = account_state(user_id, acct, now)
    d = db()
    orders = d.select("global_orders", {"user_id": eq(user_id)}, order="created_at.desc", limit=50)
    lim = Limits.from_row(lim_row)
    positions = []
    for t in raw["trades"]:
        if t.get("exit_ts"):
            continue
        ctx = t["entry_context"] or {}
        px = price_info(t["symbol"], t["price_source"], now)
        long = t["side"] == "buy"
        close_at = (px.last - QUOTES.half if long else px.last + QUOTES.half) if px.last is not None else None
        unreal = None if close_at is None else round(((close_at - float(t["entry"])) if long else (float(t["entry"]) - close_at))
                                                     * float(t["units"]), 2)
        positions.append({**t, "last": px.last, "price_age_seconds": round(px.age_seconds) if px.age_seconds is not None else None,
                          "unrealized": unreal, "stop": ctx.get("stop"), "target": ctx.get("target")})
    closed = sorted((t for t in raw["trades"] if t.get("exit_ts")), key=lambda t: t.get("exit_ts"), reverse=True)[:100]
    m = market_status(now)
    w = day_window(m.trading_day, lim.window())
    source = feed.default_source()
    now_px = price_info("XAUUSD", source, now)
    return {
        "account": {"currency": acct.get("currency", "USD"), "starting_capital": float(acct["starting_capital"]),
                    "capital": st.capital, "realized_today": st.realized_today, "realized_month": st.realized_month,
                    "open_risk": round(st.open_risk, 2), "entries_today": st.entries_today,
                    "consecutive_losses": st.consecutive_losses, "kill_switch": st.kill_switch},
        "limits": lim.to_dict(),
        "window": {"entry_start": w.entry_start.astimezone(IST).isoformat(), "entry_end": w.entry_end.astimezone(IST).isoformat(),
                   "flat_by": w.flat_by.astimezone(IST).isoformat()} if w else None,
        "market": {"is_open": m.is_open, "phase": m.phase, "reason": m.reason, "session": m.session,
                   "trading_day": m.trading_day.isoformat()},
        "price_source": source,
        "price": {"symbol": "XAUUSD", "last": now_px.last,
                  "age_seconds": round(now_px.age_seconds) if now_px.age_seconds is not None else None},
        "assumed": {"spread": QUOTES.spread, "slippage": QUOTES.slippage},
        "positions": positions,
        "orders": orders,
        "trades": closed,
        "loop": loop_state,
    }


def update_limits(user_id: str, values: dict) -> dict:
    allowed = set(Limits().to_dict())
    bad = set(values) - allowed
    if bad:
        raise PaperError(f"unknown limit(s): {', '.join(sorted(bad))}")
    ensure_account(user_id)
    for k, v in values.items():
        if k.endswith(("_london", "_new_york")):
            try:
                dt.time.fromisoformat(str(v)[:5])
            except ValueError:
                raise PaperError(f"{k} must be a time like 08:00")
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise PaperError(f"{k} must be a positive number")
    merged = Limits.from_row({**Limits().to_dict(), **db().select("global_limits", {"user_id": eq(user_id)})[0], **values})
    if not (dt.time(1, 0) <= merged.flat_by_new_york <= dt.time(16, 30)):
        raise PaperError("flat-by must be between 01:00 and 16:30 New York (the market closes at 17:00)")
    out = db().update("global_limits", {"user_id": eq(user_id)}, {**values, "updated_at": _iso(_now())})
    audit(user_id, "update_limits", values)
    return out[0]


# ---------------------------------------------------------------- background loop

loop_state: dict = {}


class Loop:
    """Runs tick() every `every` seconds. The global market is open nearly all week, so it runs whenever the app does;
    a pass with nothing to watch costs two small queries and no price calls."""

    def __init__(self, every: float = 20.0):
        self.every, self._stop = every, threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="global-paper-loop")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                result = tick()
                loop_state.clear()
                loop_state.update(at=_iso(_now()), **result)
            except Exception as e:  # noqa: BLE001 - keep the loop alive (e.g. before migration 0006 is run)
                if str(e) != loop_state.get("error"):     # say it once, not every 20 seconds
                    log.warning("global paper tick failed: %s", e)
                loop_state.clear()
                loop_state.update(at=_iso(_now()), error=str(e))
            self._stop.wait(max(1.0, self.every - (time.monotonic() - started)))
