"""The gold trade monitor: each open XAUUSD trade (paper or MT5 demo) read against the live chart every 30 seconds.

Every check is a fixed rule with the numbers it used, never a model's opinion:

  thesis    the reason the trade was taken, on the last closed 5-minute bar:
              asia-breakout   price stays beyond the Asian range
              ny-open-range   price stays beyond the New York open range (08:20-08:50 New York)
              failed-break    price stays back inside yesterday's range
              agent idea      cannot be checked by code: the agent's own "wrong if" is shown for you to judge
  trend     5-minute close on the trade's side of EMA 20; the 15-minute trend not against the trade
  momentum  the last 15 minutes moved with the trade
  shock     no 1-minute bar against the trade bigger than 2.5x the usual 1-minute range
  stop      price is not within 0.4R of the stop
  room      no established level (Asian or yesterday's high / low / close) close in the way to the target
  release   no US CPI / jobs report / Fed decision within 30 minutes (or just out): gold often jumps on them
  prices    the latest bar is fresh (a stale feed makes every other check stale too)
  time      more than 20 minutes to flat-by

Health: "on track", "watch" (one warning), "at risk" (a serious warning or several), "thesis broken" (the reason for the
trade no longer holds while it is losing, or it is 0.8R down). An alert is raised when a passing check starts failing or
health gets worse; alerts go to the page, to risk_events, and to Telegram when it is linked. Nothing here trades.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from dataclasses import dataclass

import numpy as np

from app.db import db, eq
from app.global_markets import macro, signals
from app.global_markets.agent import snapshot
from app.global_markets.clock import IST, NEW_YORK, day_window, market_status
from app.global_markets.data import feed
from app.global_markets.risk import Limits
from app.global_markets.strategies.library import COMEX_OPEN, NY_RANGE_MINUTES
from app.indicators import core as ind
from app.market.models import Bar

log = logging.getLogger("global.monitor")
SHOCK_ATR = 2.5
NEAR_STOP_R = 0.4               # warn when price is this close to the stop, in R
FLAT_WARN_MIN = 20
STALE_SECONDS = 180
SEVERITY = {"high": 3, "medium": 2, "low": 1}
HEALTH_ORDER = {"no data": 0, "on track": 0, "watch": 1, "at risk": 2, "thesis broken": 3}
STRATEGY_NAMES = {"asia-breakout": "Asian range breakout", "ny-open-range": "New York open range",
                  "failed-break": "Failed break of yesterday's high or low", "agent": "Gold agent idea"}

_state: dict[str, dict] = {}     # trade id -> {"checks": {key: ok}, "health": str, "alerts": [...]}
_lock = threading.Lock()
listeners: list = []             # called as fn(user_id, alert) for each new alert (Telegram registers here)


@dataclass
class Setup:
    side: str                    # buy / sell
    entry: float
    stop: float                  # the stop at entry: R is measured on the risk taken
    target: float | None
    entry_t: int
    flat_by: dt.datetime | None
    strategy: str | None = None  # asia-breakout / ny-open-range / failed-break / agent / None
    wrong_if: str | None = None  # the agent's own words

    @property
    def d(self) -> int:
        return 1 if self.side == "buy" else -1


def r_of(price: float, s: Setup) -> float | None:
    return None if s.entry == s.stop else s.d * (price - s.entry) / abs(s.entry - s.stop)


def _n(x) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def _ny_range(m1: list[Bar], now: dt.datetime) -> tuple[float, float] | None:
    day = now.astimezone(NEW_YORK).date()
    inside = [b for b in m1 if (t := dt.datetime.fromtimestamp(b.t, NEW_YORK)).date() == day
              and COMEX_OPEN <= t.hour * 60 + t.minute < COMEX_OPEN + NY_RANGE_MINUTES]
    if len(inside) < NY_RANGE_MINUTES * 0.8:                    # not formed yet, or too many bars missing
        return None
    return max(b.h for b in inside), min(b.l for b in inside)


def _thesis(s: Setup, c5: float, lv: dict, m1: list[Bar], now: dt.datetime) -> dict | None:
    up = s.d > 0
    if s.strategy == "asia-breakout" and "asia_high" in lv:
        edge = lv["asia_high"] if up else lv["asia_low"]
        ok = c5 > edge if up else c5 < edge
        return {"key": "thesis", "name": "Breakout of the Asian range holds", "ok": ok, "severity": "high",
                "detail": f"5m close {c5:,.2f} vs the Asian {'high' if up else 'low'} {edge:,.2f}"
                          + ("" if ok else ": back inside the Asian range, the breakout is failing")}
    if s.strategy == "ny-open-range":
        rng = _ny_range(m1, now)
        if rng:
            edge = rng[0] if up else rng[1]
            ok = c5 > edge if up else c5 < edge
            return {"key": "thesis", "name": "Breakout of the New York open range holds", "ok": ok, "severity": "high",
                    "detail": f"5m close {c5:,.2f} vs the range {'high' if up else 'low'} {edge:,.2f}"
                              + ("" if ok else ": back inside the range, the breakout is failing")}
    if s.strategy == "failed-break" and "yesterday_high" in lv:
        edge = lv["yesterday_low"] if up else lv["yesterday_high"]
        ok = c5 > edge if up else c5 < edge
        return {"key": "thesis", "name": "Stays back inside yesterday's range", "ok": ok, "severity": "high",
                "detail": f"5m close {c5:,.2f} vs yesterday's {'low' if up else 'high'} {edge:,.2f}"
                          + ("" if ok else ": it broke out again, so the break did not fail after all")}
    return None


def key_levels(lv: dict, price: float) -> list[dict]:
    """The named levels nearest the price: up to three below (support) and three above (resistance)."""
    names = {"asia_high": "Asian high", "asia_low": "Asian low", "yesterday_high": "yesterday's high",
             "yesterday_low": "yesterday's low", "yesterday_close": "yesterday's close", "today_high": "today's high",
             "today_low": "today's low"}
    pts = sorted((float(lv[k]), n) for k, n in names.items() if lv.get(k) is not None)
    merged: list[tuple[float, str]] = []
    for x, n in pts:                                             # within 0.03% (about $1) of each other: one level
        if merged and abs(x - merged[-1][0]) / x < 0.0003:
            merged[-1] = (merged[-1][0], merged[-1][1] + " / " + n)
        else:
            merged.append((x, n))
    below = [m for m in merged if m[0] < price][-3:]
    above = [m for m in merged if m[0] >= price][:3]
    return ([{"price": round(x, 2), "label": n, "kind": "support"} for x, n in below]
            + [{"price": round(x, 2), "label": n, "kind": "resistance"} for x, n in above])


def analyze(s: Setup, m1: list[Bar], m5: list[Bar], m15: list[Bar], now: dt.datetime) -> dict:
    """Pure: closed bars in, the read out."""
    m1 = [b for b in m1 if b.complete and b.t + 60 <= now.timestamp()]
    m5 = [b for b in m5 if b.complete and b.t + 300 <= now.timestamp()]
    if not m1 or not m5:
        return {"health": "no data", "headline": "No live bars for XAUUSD yet", "checks": [], "sr": []}
    price = m1[-1].c
    since = [b for b in m1 if b.t >= s.entry_t - 60]
    hi = max((b.h for b in since), default=price)
    lo = min((b.l for b in since), default=price)
    best, worst = (hi, lo) if s.d > 0 else (lo, hi)
    r_now = r_of(price, s)
    progress = {"price": round(price, 2), "r": _n(r_now), "mfe_r": _n(r_of(best, s)), "mae_r": _n(r_of(worst, s)),
                "price_age_seconds": max(0, int(now.timestamp() - (m1[-1].t + 60)))}
    minutes_in = max(0, int((now.timestamp() - s.entry_t) // 60))
    minutes_left = int((s.flat_by - now).total_seconds() // 60) if s.flat_by else None
    lv = signals.levels(m1, now)
    c5 = m5[-1].c
    checks: list[dict] = []

    th = _thesis(s, c5, lv, m1, now)
    if th:
        checks.append(th)
    closes5 = np.array([b.c for b in m5])
    ema20 = float(ind.ema(closes5, 20)[-1]) if len(m5) >= 20 else float("nan")
    if not math.isnan(ema20):
        ok = c5 > ema20 if s.d > 0 else c5 < ema20
        checks.append({"key": "ema", "name": "Trend (EMA 20, 5m)", "ok": ok, "severity": "medium",
                       "detail": f"5m close {c5:,.2f} {'above' if c5 > ema20 else 'below'} EMA 20 {ema20:,.2f}"})
    t15 = snapshot.trend(m15)
    if t15:
        against = t15["direction"] == ("down" if s.d > 0 else "up")
        checks.append({"key": "trend15", "name": "15-minute trend not against", "ok": not against, "severity": "medium",
                       "detail": f"15m trend is {t15['direction']} (close {t15['close']:,.2f}, EMA 20 {t15['ema20']:,.2f}, "
                                 f"EMA 50 {t15['ema50']:,.2f})"})
    if len(m1) >= 16:
        move = (m1[-1].c - m1[-16].c) * s.d
        checks.append({"key": "momentum", "name": "Momentum (last 15 min)", "ok": move >= 0, "severity": "low",
                       "detail": f"{'+' if move >= 0 else '−'}{abs(move):,.2f} {'with' if move >= 0 else 'against'} the trade"})
    if len(m1) >= 20:
        h1, l1, c1 = (np.array([getattr(b, k) for b in m1]) for k in ("h", "l", "c"))
        a1 = ind.atr(h1, l1, c1, 14)
        shock = None
        for k in range(len(m1) - 3, len(m1)):
            rng, body = m1[k].h - m1[k].l, (m1[k].c - m1[k].o) * s.d
            if k >= 1 and not math.isnan(a1[k - 1]) and body < 0 and rng > SHOCK_ATR * a1[k - 1]:
                shock = (k, rng, float(a1[k - 1]))
        if shock:
            k, rng, a = shock
            tm = dt.datetime.fromtimestamp(m1[k].t, IST).strftime("%H:%M")
            checks.append({"key": "shock", "name": "No sudden move against", "ok": False, "severity": "high",
                           "detail": f"the {tm} IST bar moved {rng:,.2f} against the trade, {rng / a:.1f}x the usual 1-minute range"})
        else:
            checks.append({"key": "shock", "name": "No sudden move against", "ok": True, "severity": "high",
                           "detail": "no unusual bar against the trade in the last 3 minutes"})
    if r_now is not None:
        gap = r_now + 1                                          # distance to the stop, in R
        ok = gap > NEAR_STOP_R
        checks.append({"key": "stop", "name": "Away from the stop", "ok": ok, "severity": "high",
                       "detail": f"{r_now:+.2f}R now" + ("" if ok else f": within {gap:.2f}R of the stop at {s.stop:,.2f}")})
    sr = key_levels(lv, price)
    if s.target is not None:
        # today's own high or low is where price is making the move, not a barrier set before it: only established levels
        fixed = [x for x in sr if x["label"] not in ("today's high", "today's low")]
        between = [x for x in fixed if (price < x["price"] < s.target if s.d > 0 else s.target < x["price"] < price)]
        near = [x for x in between if abs(x["price"] - price) < 0.5 * abs(s.target - price)]
        checks.append({"key": "room", "name": "Room to target", "ok": not near, "severity": "low",
                       "detail": (f"{near[0]['label']} at {near[0]['price']:,.2f} stands before the target {s.target:,.2f}"
                                  if near else "no key level close in the way to the target")})
    soon = macro.near(now)
    if soon:
        e = soon[0]
        mins = round((e.at - now).total_seconds() / 60)
        when = f"in {mins} min" if mins > 0 else ("now" if mins == 0 else f"{-mins} min ago")
        checks.append({"key": "release", "name": "No US release close", "ok": False, "severity": "high" if mins >= -5 else "medium",
                       "detail": f"{e.name} {when} ({e.at.astimezone(IST).strftime('%H:%M')} IST): {e.note}"})
    else:
        checks.append({"key": "release", "name": "No US release close", "ok": True, "severity": "high",
                       "detail": "no US CPI, jobs report or Fed decision in the next 30 minutes"})
    age = int(now.timestamp() - (m1[-1].t + 60))
    if market_status(now).is_open:
        checks.append({"key": "prices", "name": "Prices are fresh", "ok": age <= STALE_SECONDS, "severity": "medium",
                       "detail": f"latest 1-minute bar closed {age // 60} min {age % 60} s ago"
                                 + ("" if age <= STALE_SECONDS else ": every other check is that old too")})
    if minutes_left is not None:
        checks.append({"key": "time", "name": "Time left", "ok": minutes_left > FLAT_WARN_MIN, "severity": "low",
                       "detail": f"{max(minutes_left, 0)} min to the flat-by exit at {s.flat_by.astimezone(IST).strftime('%H:%M')} IST"})

    for c in checks:
        c["ok"] = bool(c["ok"])
    failing = [c for c in checks if not c["ok"]]
    worst_sev = max((SEVERITY[c["severity"]] for c in failing), default=0)
    thesis_failed = any(c["key"] == "thesis" for c in failing)
    if (thesis_failed and (r_now or 0) < 0) or (r_now is not None and r_now <= -0.8):
        health = "thesis broken"
    elif worst_sev == 3 or sum(1 for c in failing if c["severity"] == "medium") >= 2:
        health = "at risk"
    elif failing:
        health = "watch"
    else:
        health = "on track"
    top = sorted(failing, key=lambda c: -SEVERITY[c["severity"]])
    headline = {"on track": "Moving the way the trade needs",
                "watch": f"Watch: {top[0]['name'].lower()}: {top[0]['detail']}" if top else "",
                "at risk": f"At risk: {top[0]['detail']}" if top else "",
                "thesis broken": "The reason for this trade no longer holds" + (f": {top[0]['detail']}" if top else "")}[health]
    return {"health": health, "headline": headline, "progress": progress, "checks": checks, "sr": sr,
            "time": {"minutes_in": minutes_in, "minutes_to_flat": max(minutes_left, 0) if minutes_left is not None else None},
            "indicators": {"ema20": _n(ema20)}, "levels_today": {k: _n(v) for k, v in lv.items() if isinstance(v, float)}}


# ---------------------------------------------------------------- your trades

def _origin(t: dict) -> tuple[str | None, str | None]:
    """Which strategy (or the agent) took this trade, and the agent's "wrong if" when it was the agent."""
    ctx = t.get("entry_context") or {}
    if not ctx.get("order_id"):
        return None, None
    d = db()
    rows = d.select("global_orders", {"id": eq(ctx["order_id"])})
    p = (rows[0].get("params") or {}) if rows else {}
    if p.get("idea_id"):
        idea = d.select("global_agent_ideas", {"id": eq(p["idea_id"])})
        return "agent", ((idea[0].get("proposal") or {}).get("wrong_if") if idea else None)
    if p.get("signal_id"):
        sig = d.select("global_signals", {"id": eq(p["signal_id"])})
        return (sig[0]["strategy_id"] if sig else None), None
    return None, None


def setup_for(t: dict, flat_by: dt.datetime | None) -> Setup:
    ctx = t.get("entry_context") or {}
    strategy, wrong_if = _origin(t)
    return Setup(side=t["side"], entry=float(t["entry"]), stop=float(ctx.get("stop") or t["entry"]),
                 target=float(ctx["target"]) if ctx.get("target") is not None else None,
                 entry_t=int(dt.datetime.fromisoformat(t["entry_ts"]).timestamp()), flat_by=flat_by,
                 strategy=strategy, wrong_if=wrong_if)


def _flat_by(user_id: str, t: dict) -> dt.datetime | None:
    rows = db().select("global_limits", {"user_id": eq(user_id)})
    w = day_window(dt.date.fromisoformat(t["trading_day"]), Limits.from_row(rows[0] if rows else {}).window())
    return w.flat_by if w else None


def _bars(source: str) -> tuple[list[Bar], list[Bar], list[Bar]]:
    return tuple(feed.series("XAUUSD", tf, source).bars for tf in ("1m", "5m", "15m"))  # type: ignore[return-value]


def _alerts(trade: dict, a: dict, now: dt.datetime) -> list[dict]:
    tid, uid = trade["id"], trade["user_id"]
    label = f"XAUUSD {trade['side']}"
    with _lock:
        st = _state.setdefault(tid, {"checks": {}, "health": None, "alerts": []})
        new = []
        for c in a.get("checks", []):
            was = st["checks"].get(c["key"])
            if was is True and not c["ok"] and c["severity"] in ("high", "medium"):
                new.append({"level": "high" if c["severity"] == "high" else "medium",
                            "title": f"{label}: {c['name'].lower()} failed", "detail": c["detail"]})
            st["checks"][c["key"]] = c["ok"]
        h, prev = a.get("health"), st["health"]
        if prev is not None and HEALTH_ORDER.get(h, 0) > HEALTH_ORDER.get(prev, 0):
            new.append({"level": "high" if h in ("at risk", "thesis broken") else "medium",
                        "title": f"{label}: {prev} → {h}", "detail": a.get("headline", "")})
        st["health"] = h
        for n in new:
            n.update(at=now.astimezone(dt.timezone.utc).isoformat(), trade_id=tid, id=f"{tid[:8]}-{len(st['alerts']) + 1}")
            st["alerts"].append(n)
        history = list(st["alerts"])
    for n in new:
        try:
            db().insert("risk_events", {"user_id": uid, "kind": "global_monitor_alert", "detail": n})
        except Exception:  # noqa: BLE001 - the alert is still shown
            pass
        for fn in listeners:
            try:
                fn(uid, n)
            except Exception as e:  # noqa: BLE001 - a notification never stops the monitor
                log.warning("monitor listener failed: %s", e)
    return history


def analyze_trade(user_id: str, trade_id: str, now: dt.datetime | None = None, bars=None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = db().select("global_trades", {"id": eq(trade_id), "user_id": eq(user_id)})
    if not rows:
        raise LookupError("no such trade")
    t = rows[0]
    s = setup_for(t, _flat_by(user_id, t))
    if t.get("exit_ts"):
        a = {"health": "closed", "headline": "This trade is closed", "checks": [], "sr": []}
    else:
        try:
            m1, m5, m15 = bars or _bars(t["price_source"])
            a = analyze(s, m1, m5, m15, now)
        except Exception as e:  # noqa: BLE001 - the page says why
            a = {"health": "no data", "headline": f"No live bars for XAUUSD: {e}", "checks": [], "sr": []}
    out = {
        "trade": {"id": t["id"], "mode": t["mode"], "symbol": t["symbol"], "side": t["side"], "lots": t["lots"],
                  "units": t["units"], "entry": t["entry"], "entry_ts": t["entry_ts"], "exit_ts": t.get("exit_ts"),
                  "price_source": t["price_source"], "strategy": s.strategy,
                  "strategy_name": STRATEGY_NAMES.get(s.strategy or "", None), "why": (t.get("entry_context") or {}).get("why"),
                  "closing": bool(not t.get("exit_ts") and db().select(
                      "global_orders", {"trade_id": eq(t["id"]), "leg": eq("exit"), "state": eq("working")}))},
        "direction": "long" if s.d > 0 else "short",
        "levels": {"entry": s.entry, "stop": s.stop, "target": s.target, "entry_t": s.entry_t},
        "agent_wrong_if": s.wrong_if,
        "flat_by": s.flat_by.isoformat() if s.flat_by else None,
        **a,
        "as_of": now.isoformat(timespec="seconds"),
    }
    if a.get("progress") and a["progress"].get("price") is not None:
        long = s.d > 0
        from app.global_markets.paper import QUOTES
        close_at = a["progress"]["price"] - QUOTES.half if long else a["progress"]["price"] + QUOTES.half
        out["unrealized"] = round(((close_at - s.entry) if long else (s.entry - close_at)) * float(t["units"]), 2)
    out["alerts"] = _alerts(t, a, now) if a.get("checks") else list(_state.get(t["id"], {}).get("alerts", []))
    return out


def overview(user_id: str, now: dt.datetime | None = None) -> list[dict]:
    rows = db().select("global_trades", {"user_id": eq(user_id), "exit_ts": "is.null"}, order="entry_ts")
    out = []
    for t in rows:
        try:
            a = analyze_trade(user_id, t["id"], now)
        except Exception as e:  # noqa: BLE001 - one bad trade must not hide the others
            a = {"health": "no data", "headline": str(e)[:200], "alerts": []}
        out.append({"trade_id": t["id"], "mode": t["mode"], "symbol": t["symbol"], "side": t["side"],
                    "health": a.get("health"), "headline": a.get("headline"), "r": (a.get("progress") or {}).get("r"),
                    "unrealized": a.get("unrealized"), "alerts": a.get("alerts", [])[-5:]})
    return out


def scan(now: dt.datetime | None = None) -> int:
    """Read every open gold trade once, so alerts reach you (and Telegram) even with no page open."""
    rows = db().select("global_trades", {"exit_ts": "is.null"})
    for t in rows:
        try:
            analyze_trade(t["user_id"], t["id"], now)
        except Exception as e:  # noqa: BLE001 - try again next pass
            log.warning("monitor read of %s failed: %s", t["id"], e)
    with _lock:                                  # forget closed trades
        open_ids = {t["id"] for t in rows}
        for k in [k for k in _state if k not in open_ids]:
            _state.pop(k, None)
    return len(rows)


class MonitorLoop:
    def __init__(self, every: float = 30.0):
        self.every, self._stop = every, threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="global-monitor").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                scan()
            except Exception as e:  # noqa: BLE001
                log.warning("monitor pass failed: %s", e)
            self._stop.wait(self.every)

