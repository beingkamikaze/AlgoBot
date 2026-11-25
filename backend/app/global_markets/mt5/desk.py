"""The gold desk on an MT5 DEMO account: your confirmed tickets go to the broker instead of the app's simulator.

You choose where confirmed tickets go (global_accounts.route): "paper" (the simulator, the default) or "mt5_demo". With
mt5_demo, everything before the broker is unchanged: the agent or a signal proposes, the risk check sizes the ticket from
your max loss, you confirm (web or Telegram), and the risk check runs again. Then:

  send()   the entry goes to MT5 with its stop and target held AT THE BROKER, so they work even if the app is down. The
           order expires at MT5 when the app's own entry would (10 minutes, or the end of the entry window);
  sync()   every 10 s: a fill at MT5 becomes a trade (mode "demo") at the broker's real price; a stop, a target or a close
           at MT5 closes it with the broker's real profit, commission and swap; at flat-by the app closes it; and the app's
           open trades are checked against MT5's positions, with every difference shown on the Paper page.

Rules that do not bend: demo accounts only (the bridge and Mt5Broker.connect both refuse a real account); no risk override
(that is for the simulator only); getting out is never blocked. Agents never place orders: only your confirmation does.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading

from app.config import get_settings
from app.db import db, eq
from app.global_markets.clock import trading_day
from app.global_markets.instruments import resolve
from app.global_markets.mt5.broker import BridgeClient, Mt5Broker, Mt5Error, tag_for

log = logging.getLogger("global.mt5")
SYNC_SECONDS = 10
UNKNOWN_GRACE_SECONDS = 120         # an order whose fate was unknown and is still not at MT5 after this never arrived
CLOSE_RETRY_SECONDS = 30            # a close that has not shown up at MT5 by then is sent again (closing twice is harmless)

state: dict = {"status": "not started", "error": None, "mismatches": [], "last_sync": None}
_broker: Mt5Broker | None = None


def configured() -> bool:
    s = get_settings()
    return bool(s.mt5_bridge_url and s.mt5_bridge_token)


def broker() -> Mt5Broker:
    global _broker
    if _broker is None:
        s = get_settings()
        _broker = Mt5Broker(BridgeClient(s.mt5_bridge_url, s.mt5_bridge_token))
    return _broker


def _iso(t: dt.datetime | int | float) -> str:
    if isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).isoformat()


def is_mt5(order: dict) -> bool:
    return (order.get("params") or {}).get("broker") == "mt5"


# ---------------------------------------------------------------- sending your confirmed entry

def send(order: dict, params: dict, expires: dt.datetime, now: dt.datetime) -> dict:
    """Your confirmed entry -> MT5. Returns the order's params with what MT5 said. Raises Mt5Error when MT5 refused it
    (nothing was placed). A lost answer is not an error: the order is marked unknown and sync() finds out."""
    if params.get("target") is None:
        raise Mt5Error("a ticket sent to MT5 needs a target")
    b = broker()
    b.connect()                                                   # refuses anything but a demo account
    try:
        r = b.place_entry(order["id"], params["side"], float(params["lots"]), float(params["limit"]),
                          float(params["stop"]), float(params["target"]), expires_at=expires)
        info = {k: r.get(k) for k in ("tag", "ticket", "state", "price", "volume", "found_after_timeout")}
    except Mt5Error as e:
        if not e.ambiguous:
            raise
        info = {"tag": tag_for(order["id"]), "state": "unknown", "note": str(e)}
    return {**params, "broker": "mt5", "mt5": {**info, "sent_at": _iso(now), "expires_at": _iso(expires),
                                               "account": (b.account or {}).get("login")}}


def cancel(order: dict) -> None:
    """Cancel a working entry at MT5. Refused if it has already filled there."""
    b = broker()
    tag = order["params"]["mt5"]["tag"]
    for o in b.client.orders():
        if o["comment"] == tag:
            b.cancel(o["ticket"])
            return
    if b.find(tag):
        raise Mt5Error("this order has already filled at MT5: close the position instead")


def close(trade: dict, reason: str) -> None:
    """Out at the market at MT5, at most the app's assumed slippage from the price. Never blocked by a limit."""
    from app.global_markets.paper import COSTS
    ticket = (trade.get("entry_context") or {}).get("mt5_position")
    if not ticket:
        raise Mt5Error("this trade has no MT5 position number")
    broker().close(int(ticket), max_slippage=max(COSTS.slippage * 4, 0.5), reason=reason)


# ---------------------------------------------------------------- keeping the app in step with MT5

def sync(now: dt.datetime | None = None, flat_by_for=None) -> dict:
    """One pass. Fills, exits, expiries and flat-by for every order and trade on the MT5 demo route."""
    from app.global_markets import paper
    now = now or dt.datetime.now(dt.timezone.utc)
    stats = {"filled": 0, "closed": 0, "expired": 0}
    with paper._lock:
        d = db()
        entries = [o for o in d.select("global_orders", {"state": eq("working"), "leg": eq("entry")}) if is_mt5(o)]
        exits = [o for o in d.select("global_orders", {"state": eq("working"), "leg": eq("exit")}) if is_mt5(o)]
        open_ = d.select("global_trades", {"mode": eq("demo"), "exit_ts": "is.null"})
        if not entries and not open_ and not exits:
            state.update(status="idle: nothing at MT5", mismatches=[], last_sync=_iso(now))
            return stats
        b = broker()
        positions = {p["ticket"]: p for p in b.client.positions()}
        pending = {o["comment"]: o for o in b.client.orders()}
        deals = b.client.deals(int((now - dt.timedelta(days=4)).timestamp()))
        inst = resolve("XAUUSD")

        for o in entries:
            p, info = o["params"], o["params"]["mt5"]
            if info["tag"] in pending:
                continue
            filled = next((x for x in positions.values() if x["comment"] == info["tag"]), None)
            deal_in = next((x for x in deals if x["comment"] == info["tag"] and x["entry"] == "in"), None)
            if filled or deal_in:
                _open_trade(d, o, filled, deal_in, b, inst, now)
                stats["filled"] += 1
                continue
            age = now.timestamp() - dt.datetime.fromisoformat(info["sent_at"]).timestamp()
            if info.get("state") == "unknown" and age < UNKNOWN_GRACE_SECONDS:
                continue
            why = ("the order never reached MT5" if info.get("state") == "unknown"
                   else "MT5 cancelled it unfilled at its expiry")
            d.update("global_orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": _iso(now),
                                                            "params": {**p, "expired_because": why}})
            paper.audit(o["user_id"], "expired", {"order_id": o["id"], "why": why, "broker": "mt5"}, actor="system")
            paper._tell("expired", o["user_id"], {"symbol": p["symbol"], "why": why})
            stats["expired"] += 1

        exiting = {o["trade_id"]: o for o in exits}
        for t in d.select("global_trades", {"mode": eq("demo"), "exit_ts": "is.null"}):
            ctx = t["entry_context"] or {}
            ticket = ctx.get("mt5_position")
            if ticket in positions:
                ex = exiting.get(t["id"])
                if ex and now.timestamp() - int(ex["params"]["placed_t"]) > CLOSE_RETRY_SECONDS:
                    _try_close(t, ex["params"].get("reason", "manual"))
                    d.update("global_orders", {"id": eq(ex["id"])}, {"params": {**ex["params"], "placed_t": int(now.timestamp())}})
                elif not ex:
                    flat = (flat_by_for or _flat_by)(t)
                    if flat and now.timestamp() >= flat:
                        paper.close_position(t["user_id"], t["id"], reason="flat-by", now=now)
                continue
            mine = [x for x in deals if x["position"] == ticket]
            out = [x for x in mine if x["entry"] == "out"]
            if not out:
                continue                                    # reconcile below names it
            _close_trade(d, t, mine, out, now)
            if t["id"] in exiting:
                d.update("global_orders", {"id": eq(exiting[t["id"]]["id"])}, {"state": "filled", "updated_at": _iso(now)})
            stats["closed"] += 1

        still = d.select("global_trades", {"mode": eq("demo"), "exit_ts": "is.null"})
        mism = b.reconcile([{"tag": (t["entry_context"] or {}).get("mt5_tag"), "side": t["side"], "lots": float(t["lots"])}
                            for t in still])
        if mism != state.get("mismatches") and mism:
            log.warning("MT5 and the app disagree: %s", "; ".join(mism))
        state.update(status="in step with MT5" if not mism else "MT5 and the app disagree", mismatches=mism,
                     last_sync=_iso(now), error=None)
    return stats


def _flat_by(t: dict) -> int:
    from app.global_markets import paper
    rows = db().select("global_limits", {"user_id": eq(t["user_id"])})
    lim = paper.Limits.from_row(rows[0] if rows else {})
    return paper._flat_by_t(dt.date.fromisoformat(t["trading_day"]), lim)


def _try_close(t: dict, reason: str) -> None:
    try:
        close(t, reason)
    except Mt5Error as e:
        log.warning("MT5 close of %s failed: %s", t["id"], e)
        state.update(error=f"closing {t['side']} {t['symbol']} at MT5 failed: {e}")


def _open_trade(d, o: dict, pos: dict | None, deal_in: dict | None, b: Mt5Broker, inst, now: dt.datetime) -> None:
    from app.global_markets import paper
    p = o["params"]
    price = float(deal_in["price"] if deal_in else pos["price_open"])
    vol = float(deal_in["volume"] if deal_in else pos["volume"])
    ticket = int(deal_in["position"] if deal_in else pos["ticket"])
    at = int(deal_in["time"] if deal_in else pos["time"])
    units = round(vol * float(b.spec["contract_size"]), 6)                 # what actually filled, in ounces
    lots = round(units / inst.contract_size, 8)
    stop = float(p["stop"])
    risk = round(units * abs(price - stop), 2)
    t = d.insert("global_trades", {
        "user_id": o["user_id"], "mode": "demo", "symbol": p["symbol"], "side": p["side"], "lots": lots, "units": units,
        "price_source": p["source"], "entry_ts": _iso(at), "entry": price, "rules_followed": True,
        "trading_day": trading_day(dt.datetime.fromtimestamp(at, dt.timezone.utc)).isoformat(),
        "entry_context": {"order_id": o["id"], "stop": stop, "target": p.get("target"), "limit": p["limit"],
                          "risk_amount": risk, "why": p.get("why"), "card": p.get("card"), "broker": "mt5",
                          "mt5_position": ticket, "mt5_tag": p["mt5"]["tag"], "mt5_account": p["mt5"].get("account"),
                          "mae": 0, "mfe": 0}})[0]
    d.update("global_orders", {"id": eq(o["id"])}, {"state": "filled", "trade_id": t["id"], "updated_at": _iso(now)})
    paper.audit(o["user_id"], "entry_filled", {"order_id": o["id"], "trade_id": t["id"], "price": price, "broker": "mt5"},
                actor="system")
    paper._tell("filled", o["user_id"], {"symbol": p["symbol"], "side": p["side"], "lots": lots, "price": price,
                                         "stop": stop, "target": p.get("target"), "where": "MT5 demo"})


def _close_trade(d, t: dict, mine: list[dict], out: list[dict], now: dt.datetime) -> None:
    from app.global_markets import paper
    last = max(out, key=lambda x: x["time"])
    profit = round(sum(float(x["profit"]) for x in mine), 2)
    charges = round(sum(float(x.get("commission") or 0) + float(x.get("swap") or 0) + float(x.get("fee") or 0)
                        for x in mine), 2)                               # negative = you paid
    net = round(profit + charges, 2)
    ctx = dict(t["entry_context"] or {})
    risk = float(ctx.get("risk_amount") or 0)
    reason = {"stop": "stop", "target": "target"}.get(last["reason"]) or ctx.get("closing_reason") or "closed at MT5"
    d.update("global_trades", {"id": eq(t["id"])}, {
        "exit_ts": _iso(int(last["time"])), "exit": float(last["price"]), "exit_reason": reason, "gross_pnl": profit,
        "costs": round(-charges, 2), "net_pnl": net, "r_multiple": round(net / risk, 3) if risk else None,
        "entry_context": {**ctx, "mt5_deals": [x["ticket"] for x in mine]}})
    paper.audit(t["user_id"], "trade_closed", {"trade_id": t["id"], "reason": reason, "net": net, "broker": "mt5"},
                actor="system")
    paper._tell("closed", t["user_id"], {"symbol": t["symbol"], "side": t["side"], "price": float(last["price"]),
                                         "reason": reason, "net": net, "r": round(net / risk, 2) if risk else None,
                                         "where": "MT5 demo"})
    try:
        paper.on_trade_closed(t["id"])
    except Exception as e:  # noqa: BLE001 - the trade is closed whatever happens to its post-mortem
        log.warning("post-mortem not scheduled for %s: %s", t["id"], e)


def window_expiry(now: dt.datetime, entry_end: dt.datetime | None, seconds: int) -> dt.datetime:
    """When the entry stops being wanted: the app's usual order life, or the end of the entry window if sooner."""
    end = now + dt.timedelta(seconds=seconds)
    return min(end, entry_end) if entry_end and entry_end > now else end


class SyncLoop:
    """Every 10 s while the MT5 bridge is set up: sync()."""

    def __init__(self, every: float = SYNC_SECONDS):
        self.every, self._stop = every, threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="mt5-sync").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sync()
            except Exception as e:  # noqa: BLE001 - keep trying; the Paper page says why
                msg = str(e)[:300]
                if msg != state.get("error"):
                    log.warning("MT5 sync failed: %s", msg)
                state.update(status="error", error=msg)
            self._stop.wait(self.every)

