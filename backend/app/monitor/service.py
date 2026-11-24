"""Open trades (paper and live) read against the live chart, with an alert history.

An alert is raised when a check that was passing starts failing, or when the trade's health gets worse
(on track -> watch -> at risk -> thesis broken). Alerts are kept in memory for the session and written to
risk_events (kind "monitor_alert") so the journal can show them later. Nothing here trades: it only tells you.
"""

from __future__ import annotations

import datetime as dt
import statistics
import threading
import time

from app.backtest import runner
from app.core import manage
from app.core.calendar import IST
from app.db import db, eq
from app.market import feed, yahoo
from app.monitor.analysis import Bars, Setup, analyze
from app.options import chain as ch
from app.paper import service as paper
from app.risk.engine import Limits
from app.strategies.base import Frame

HEALTH_ORDER = {"no data": 0, "on track": 0, "watch": 1, "at risk": 2, "thesis broken": 3}
_state: dict[str, dict] = {}            # trade id -> {"checks": {key: ok}, "health": str, "alerts": [...]}
_cache: dict[str, tuple[float, dict]] = {}
_sources: dict[str, str] = {}             # symbol -> where its last bars came from (kite_live / kite / yahoo)
_lock = threading.Lock()
CACHE_SECONDS = 8


def _bars(symbol: str, tf: str, rng: str) -> Bars:
    c = feed.candles(symbol, tf, rng)
    _sources[symbol] = c.source
    f = Frame(c)
    return Bars(t=f.t, o=f.o, h=f.h, l=f.l, c=f.c, v=f.v, session=f.session, minute=f.minute)


def setup_for(t: dict, flat_by: dt.time) -> Setup:
    ctx = t.get("entry_context") or {}
    entry_t = int(dt.datetime.fromisoformat(t["entry_ts"]).timestamp())
    if ctx.get("kind") == "option":
        ex = ctx.get("exits") or {}
        side = ex.get("side_hint") or ("sell" if ctx.get("template") in ("long_put", "bear_put_spread", "bear_call_spread") else "buy")
        first = ctx.get("initial_underlying_stop", ex.get("underlying_stop"))
        return Setup(symbol=ctx["underlying"], direction=1 if side == "buy" else -1, entry=float(ctx.get("spot_at_entry") or 0),
                     stop=first, target=ex.get("underlying_target"), entry_t=entry_t,
                     strategy=ctx.get("strategy"), flat_by=flat_by, current_stop=ex.get("underlying_stop"))
    sp = paper.spec(ctx, t["symbol"])
    first = ctx.get("initial_stop", ctx.get("stop"))
    return Setup(symbol=sp["data_symbol"], direction=1 if t["side"] == "buy" else -1, entry=float(t["entry"]),
                 stop=float(first) if first is not None else None,
                 target=float(ctx["target"]) if ctx.get("target") is not None else None, entry_t=entry_t,
                 strategy=ctx.get("strategy"), flat_by=flat_by,
                 current_stop=float(ctx["stop"]) if ctx.get("stop") is not None else None)


def expected_path(strategy: str | None, version: str | None, futures: bool) -> dict | None:
    """From the strategy's latest backtest: how long its winners and losers usually take."""
    if not strategy:
        return None
    try:
        bt = runner.latest_for(strategy, version or "1", futures=futures)
    except Exception:  # noqa: BLE001
        bt = None
    trades = (bt or {}).get("trades") or []
    if not trades:
        return None
    mins = lambda xs: [max(1, (x["exit_t"] - x["entry_t"]) // 60) for x in xs]  # noqa: E731
    win = mins([x for x in trades if x["exit_reason"] == "target"])
    loss = mins([x for x in trades if x["exit_reason"] == "stop"])
    mix = {k: round(sum(1 for x in trades if x["exit_reason"] == k) / len(trades), 3) for k in ("target", "stop", "time")}
    return {"trades": len(trades), "exit_mix": mix, "median_minutes_to_target": int(statistics.median(win)) if win else None,
            "median_minutes_to_stop": int(statistics.median(loss)) if loss else None,
            "target_share": round(len(win) / len(trades), 3)}


def _option_now(ctx: dict) -> dict | None:
    try:
        c = ch.chain(ctx["underlying"], dt.date.fromisoformat(ctx["expiry"]), allow_modeled=False)
    except (ch.ChainError, KeyError, ValueError):
        return None
    legs = []
    for lg in ctx.get("legs") or []:
        q = c.quote(float(lg["strike"]), lg["kind"])
        g = c.greeks(float(lg["strike"]), lg["kind"])
        legs.append({"contract": lg["tradingsymbol"], "side": lg["side"], "entry": lg["price"],
                     "bid": q.bid if q else None, "ask": q.ask if q else None, "mid": q.mid if q else None,
                     "delta": round(g.delta, 3) if g else None, "theta": round(g.theta, 2) if g else None, "iv": q.iv if q else None})
    try:
        pnl = paper.paper_options.unrealized(ctx, c)
    except Exception:  # noqa: BLE001
        pnl = None
    ex = ctx.get("exits") or {}
    return {"legs": legs, "pnl": pnl, "stop_pnl": ex.get("stop_pnl"), "target_pnl": ex.get("target_pnl"), "source": c.source,
            "spot": c.spot, "as_of": c.as_of, "lot_size": ctx.get("lot_size")}


def _alerts(trade_id: str, user_id: str, symbol: str, a: dict, now: dt.datetime) -> list[dict]:
    with _lock:
        st = _state.setdefault(trade_id, {"checks": {}, "health": None, "alerts": []})
        new = []
        for c in a.get("checks", []):
            was = st["checks"].get(c["key"])
            if was is True and not c["ok"] and c["severity"] in ("high", "medium"):
                new.append({"level": "high" if c["severity"] == "high" else "medium", "title": f"{symbol}: {c['name'].lower()} failed",
                            "detail": c["detail"]})
            st["checks"][c["key"]] = c["ok"]
        h, prev = a.get("health"), st["health"]
        if prev is not None and HEALTH_ORDER.get(h, 0) > HEALTH_ORDER.get(prev, 0):
            new.append({"level": "high" if h in ("at risk", "thesis broken") else "medium",
                        "title": f"{symbol}: {prev} → {h}", "detail": a.get("headline", "")})
        st["health"] = h
        stamp = now.astimezone(dt.timezone.utc).isoformat()
        for n in new:
            n.update(at=stamp, trade_id=trade_id, id=f"{trade_id[:8]}-{len(st['alerts']) + 1}")
            st["alerts"].append(n)
        history = list(st["alerts"])
    for n in new:
        try:
            db().insert("risk_events", {"user_id": user_id, "kind": "monitor_alert", "detail": n})
        except Exception:  # noqa: BLE001 - the alert is still shown
            pass
    return history


def _management_alerts(trade_id: str, user_id: str, symbol: str, mg: dict | None, now: dt.datetime) -> None:
    """An alert when trade management moves the stop or a partial exit fills. Events already there the first
    time this trade is seen (e.g. after a restart) are shown on the chart but not alerted again."""
    if not mg:
        return
    moves, part = mg.get("moves") or [], mg.get("partial")
    new = []
    with _lock:
        st = _state.setdefault(trade_id, {"checks": {}, "health": None, "alerts": []})
        seen = st.get("mg_seen")
        if seen is not None:
            for mv in moves[seen[0]:]:
                tm = dt.datetime.fromtimestamp(mv["t"], IST).strftime("%H:%M")
                new.append({"level": "medium", "title": f"{symbol}: stop moved {mv['from']:,.2f} → {mv['to']:,.2f}",
                            "detail": f"{tm}, {mv['why']}", "kind": "stop_moved"})
            if part and not seen[1]:
                new.append({"level": "medium", "title": f"{symbol}: partial exit filled",
                            "detail": f"{part['qty']} at {part['price']:,.2f} (+{part['at_r']:g}R); the rest runs", "kind": "partial"})
        st["mg_seen"] = (len(moves), bool(part))
        stamp = now.astimezone(dt.timezone.utc).isoformat()
        for n in new:
            n.update(at=stamp, trade_id=trade_id, id=f"{trade_id[:8]}-{len(st['alerts']) + 1}")
            st["alerts"].append(n)
    for n in new:
        try:
            db().insert("risk_events", {"user_id": user_id, "kind": "monitor_alert", "detail": n})
        except Exception:  # noqa: BLE001 - the alert is still shown
            pass


_match_tried: set[str] = set()


def match_strategy(t: dict, s: Setup) -> list[dict]:
    """Which of today's signals made this trade: same underlying, direction, stop and target (to the paisa).
    For trades whose ticket did not carry its signal."""
    from app.signals.scan import scan
    from app.strategies.library import describe
    if s.stop is None or s.target is None:
        return []
    sym = f"{s.symbol}-FUT" if s.symbol in ("NIFTY", "BANKNIFTY") else s.symbol
    side = "buy" if s.direction > 0 else "sell"
    found = []
    for st in describe():
        if st["market"] == "index_options":
            continue
        try:
            r = scan(st["id"], {}, [sym], None)
        except Exception:  # noqa: BLE001 - a failed scan only means no match from it
            continue
        for g in r["signals"]:
            p = g["explanation"]["plan"]
            if g["side"] == side and abs(p["stop"] - s.stop) < 0.01 and p.get("target") is not None and abs(p["target"] - s.target) < 0.01:
                found.append({"strategy": g["strategy_id"], "version": g["strategy_version"], "signal_time": g["created_at"],
                              "summary": g["explanation"].get("summary")})
    return found


def link_strategy(user_id: str, trade_id: str, strategy: str | None = None) -> dict:
    """Link a trade to its strategy: matched from today's signals, or the one you choose."""
    from app.strategies.library import describe
    rows = db().select("trades", {"id": eq(trade_id), "user_id": eq(user_id)})
    if not rows:
        raise LookupError("no such trade")
    t = rows[0]
    ctx = dict(t.get("entry_context") or {})
    if strategy:
        known = {x["id"] for x in describe()}
        if strategy not in known:
            raise ValueError(f"unknown strategy {strategy!r}")
        ctx.update(strategy=strategy, strategy_version=ctx.get("strategy_version") or "1", strategy_link={"by": "you"})
    else:
        lim = db().select("risk_limits", {"user_id": eq(user_id)})
        matches = match_strategy(t, setup_for(t, Limits.from_row(lim[0] if lim else {}).flat_by))
        if len(matches) != 1:
            return {"linked": False, "matches": matches}
        m = matches[0]
        ctx.update(strategy=m["strategy"], strategy_version=m["version"],
                   strategy_link={"by": "matched", "signal_time": m["signal_time"], "on": "direction, stop and target"})
        if not ctx.get("summary"):
            ctx["summary"] = m["summary"]
    db().update("trades", {"id": eq(trade_id)}, {"entry_context": ctx})
    _cache.pop(trade_id, None)
    _state.pop(trade_id, None)               # its checks change: start the alert history afresh
    return {"linked": True, "strategy": ctx["strategy"], "link": ctx.get("strategy_link")}


def analyze_trade(user_id: str, trade_id: str, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(IST)
    hit = _cache.get(trade_id)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    rows = db().select("trades", {"id": eq(trade_id), "user_id": eq(user_id)})
    if not rows:
        raise LookupError("no such trade")
    t = rows[0]
    ctx = t.get("entry_context") or {}
    lim_rows = db().select("risk_limits", {"user_id": eq(user_id)})
    flat_by = Limits.from_row(lim_rows[0] if lim_rows else {}).flat_by
    s = setup_for(t, flat_by)
    if not s.strategy and not t.get("exit_ts") and trade_id not in _match_tried:
        _match_tried.add(trade_id)            # once per trade: find which of today's signals made it
        try:
            if link_strategy(user_id, trade_id).get("linked"):
                t = db().select("trades", {"id": eq(trade_id)})[0]
                ctx = t.get("entry_context") or {}
                s = setup_for(t, flat_by)
        except Exception:  # noqa: BLE001 - generic checks still work without a strategy
            pass
    futures = ctx.get("kind") == "option" or str(t["symbol"]).endswith("FUT") or ctx.get("kind") == "future"
    exp = expected_path(s.strategy, ctx.get("strategy_version"), futures and s.symbol in ("NIFTY", "BANKNIFTY"))
    try:
        b1 = _bars(s.symbol, "1m", "1d")
        b5 = _bars(s.symbol, "5m", "5d")
        a = analyze(s, b1, b5, now, exp) if not t.get("exit_ts") else {"health": "closed", "headline": "This trade is closed", "checks": [], "sr": []}
    except yahoo.MarketDataError as e:
        a = {"health": "no data", "headline": f"No live bars for {s.symbol}: {e}", "checks": [], "sr": []}
    mg = ctx.get("manage")
    if not t.get("exit_ts"):
        _management_alerts(t["id"], user_id, s.symbol, mg, now)
    out = {
        "trade": {"id": t["id"], "mode": t["mode"], "symbol": t["symbol"], "side": t["side"], "qty": t["qty"], "entry": t["entry"],
                  "entry_ts": t["entry_ts"], "exit_ts": t.get("exit_ts"), "kind": ctx.get("kind", "equity"),
                  "strategy": s.strategy, "strategy_link": ctx.get("strategy_link"), "summary": ctx.get("summary")},
        "underlying": s.symbol, "direction": "long" if s.direction > 0 else "short",
        "levels": {"entry": s.entry, "stop": s.live_stop, "initial_stop": s.stop, "target": s.target, "entry_t": s.entry_t},
        "management": None if not mg else {"description": manage.describe(mg.get("rules")), "moves": mg.get("moves") or [],
                                           "partial": mg.get("partial"), "notes": mg.get("notes") or [],
                                           "open_qty": paper.open_qty(t) if ctx.get("kind") != "option" else None},
        **a,
        "option": _option_now(ctx) if ctx.get("kind") == "option" and not t.get("exit_ts") else None,
        "as_of": now.isoformat(timespec="seconds"),
        "data_source": _sources.get(s.symbol), "data_source_label": feed.label(_sources[s.symbol]) if s.symbol in _sources else None,
    }
    out["alerts"] = _alerts(t["id"], user_id, s.symbol, a, now) if a.get("checks") else list(_state.get(t["id"], {}).get("alerts", []))
    _cache[trade_id] = (time.monotonic(), out)
    return out


def overview(user_id: str, now: dt.datetime | None = None) -> list[dict]:
    """Every open trade, with its health and alerts (the shell polls this to raise alerts on any page)."""
    rows = db().select("trades", {"user_id": eq(user_id), "exit_ts": "is.null"}, order="entry_ts")
    out = []
    for t in rows:
        try:
            a = analyze_trade(user_id, t["id"], now)
        except Exception as e:  # noqa: BLE001 - one bad trade must not hide the others
            a = {"trade": {"id": t["id"], "symbol": t["symbol"], "mode": t["mode"]}, "health": "no data", "headline": str(e)[:200], "alerts": []}
        out.append({"trade_id": t["id"], "mode": t["mode"], "symbol": t["symbol"], "underlying": a.get("underlying"),
                    "health": a.get("health"), "headline": a.get("headline"), "r": (a.get("progress") or {}).get("r"),
                    "option_pnl": (a.get("option") or {}).get("pnl"), "alerts": a.get("alerts", [])[-5:],
                    "data_source": a.get("data_source")})
    return out

