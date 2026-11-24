"""The market watcher: plain code that turns live gold bars into a snapshot of facts, and decides when something happened
that is worth the agent's attention.

The snapshot is the only thing the agent sees. Every number in it is measured here from closed bars, so the agent cannot
quote a price or level that was not on the screen (the grounding check holds it to that).
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from app.global_markets import macro, signals
from app.global_markets.clock import IST, NEW_YORK, day_window, market_status, session_name
from app.indicators import core
from app.market.models import Bar

TIMEFRAMES = ("5m", "15m", "1h")
RECENT_5M = 12                     # the last hour of 5-minute bars, written out
BIG_BAR_ATR = 2.0                  # a 5-minute bar this many typical moves long is news in itself
HEARTBEAT_MINUTES = 60             # look at least this often inside the window, even if nothing happened


def _r(x) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def _ny(t: int | dt.datetime) -> str:
    if isinstance(t, int):
        t = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return t.astimezone(NEW_YORK).strftime("%H:%M")


def trend(bars: list[Bar]) -> dict | None:
    """Closed bars of one timeframe -> where price sits against its 20 and 50 EMAs, RSI 14, ATR 14 and the last 12 bars'
    range. "up" needs close > EMA20 > EMA50 with EMA20 rising; "down" the mirror; anything else is "mixed"."""
    done = [b for b in bars if b.complete]
    if len(done) < 52:
        return None
    c = np.array([b.c for b in done])
    h = np.array([b.h for b in done])
    lo = np.array([b.l for b in done])
    e20, e50 = core.ema(c, 20), core.ema(c, 50)
    rsi, atr = core.rsi(c, 14), core.atr(h, lo, c, 14)
    rising = e20[-1] > e20[-4]
    if c[-1] > e20[-1] > e50[-1] and rising:
        direction = "up"
    elif c[-1] < e20[-1] < e50[-1] and not rising:
        direction = "down"
    else:
        direction = "mixed"
    return {"direction": direction, "close": _r(c[-1]), "ema20": _r(e20[-1]), "ema50": _r(e50[-1]),
            "ema20_rising": bool(rising), "rsi14": _r(rsi[-1]), "atr14": _r(atr[-1]),
            "last_12_high": _r(h[-12:].max()), "last_12_low": _r(lo[-12:].min()), "last_bar_closed_ny": _ny(done[-1].t)}


def build(now: dt.datetime, bars_by_tf: dict[str, list[Bar]], *, source: str, headlines: list[dict] | None = None,
          symbol: str = "XAUUSD") -> dict | None:
    """The facts for one look, or None when there are no closed bars to look at."""
    m1 = [b for b in bars_by_tf.get("1m", []) if b.complete and b.t + 60 <= now.timestamp()]
    m5 = [b for b in bars_by_tf.get("5m", []) if b.complete and b.t + 300 <= now.timestamp()]
    if not m1 and not m5:
        return None
    last = m1[-1] if m1 else m5[-1]
    last_close_t = last.t + (60 if m1 else 300)
    status = market_status(now)
    w = day_window(status.trading_day)
    price = last.c
    lv = signals.levels(m1, now) if m1 else {}
    atr5 = signals.atr_5m(m1) if m1 else None
    frames = {tf: trend(bars_by_tf.get(tf, [])) for tf in TIMEFRAMES}
    if atr5 is None and frames.get("5m"):
        atr5 = frames["5m"]["atr14"]
    events = macro.between(now - macro.WARN_AFTER, w.flat_by) if w else macro.near(now)
    facts: dict = {
        "symbol": symbol,
        "price_source": ("COMEX futures (GC=F), about 10 minutes late: levels differ a little from spot XAUUSD"
                         if source == "futures" else "spot XAUUSD"),
        "now": {"ist": now.astimezone(IST).strftime("%H:%M"), "new_york": _ny(now),
                "session": session_name(now) or "closed", "trading_day": status.trading_day.isoformat()},
        "price": {"last": _r(price), "bar_closed_ny": _ny(last_close_t),
                  "age_seconds": max(0, int(now.timestamp()) - last_close_t)},
        "typical_5m_move": atr5,
        "levels": {k: _r(v) if isinstance(v, float) else v for k, v in lv.items()},
        "trend": {tf: f for tf, f in frames.items() if f},
        "recent_5m_bars": [{"closed_ny": _ny(b.t + 300), "open": _r(b.o), "high": _r(b.h), "low": _r(b.l), "close": _r(b.c)}
                           for b in m5[-RECENT_5M:]],
        "us_releases": [e.to_dict(now) for e in events],
        "headlines": [{"ist": h.get("ist"), "title": h.get("title")} for h in (headlines or [])[:5]],
    }
    if w:
        facts["window"] = {"new_entries_until_ny": _ny(w.entry_end), "flat_by_ny": _ny(w.flat_by),
                           "flat_by_ist": w.flat_by.astimezone(IST).strftime("%H:%M"),
                           "minutes_to_flat_by": max(0, round((w.flat_by - now).total_seconds() / 60))}
    return facts


def triggers(prev: dict | None, cur: dict, last_look: dt.datetime | None, now: dt.datetime) -> list[str]:
    """Why the agent should look now, in plain words. Empty means nothing happened since the last look."""
    out: list[str] = []
    if prev is None or prev["now"]["trading_day"] != cur["now"]["trading_day"]:
        return ["first look of the trading day"]
    p, c = prev["price"]["last"], cur["price"]["last"]
    lv = cur.get("levels") or {}
    names = {"asia_high": "the Asian high", "asia_low": "the Asian low", "yesterday_high": "yesterday's high",
             "yesterday_low": "yesterday's low"}
    for key, name in names.items():
        level = lv.get(key)
        if level is None or p is None or c is None:
            continue
        if p <= level < c:
            out.append(f"price rose through {name} ({level})")
        elif p >= level > c:
            out.append(f"price fell through {name} ({level})")
    bars = cur.get("recent_5m_bars") or []
    atr = cur.get("typical_5m_move")
    if bars and atr and bars[-1] != (prev.get("recent_5m_bars") or [None])[-1]:
        rng = bars[-1]["high"] - bars[-1]["low"]
        if rng >= BIG_BAR_ATR * atr:
            out.append(f"a big 5-minute bar: {rng:.2f} from high to low, about {rng / atr:.1f} typical moves")
    for tf in ("15m", "1h"):
        a, b = (prev.get("trend") or {}).get(tf), (cur.get("trend") or {}).get(tf)
        if a and b and a["direction"] != b["direction"]:
            out.append(f"the {tf} trend changed from {a['direction']} to {b['direction']}")
    if cur["now"]["session"] != prev["now"]["session"]:
        out.append(f"the {cur['now']['session'].replace('_', ' ')} session began")
    seen = {h["title"] for h in prev.get("headlines") or []}
    fresh = [h["title"] for h in cur.get("headlines") or [] if h["title"] not in seen]
    if fresh:
        out.append(f"new headline: {fresh[0]}")
    released = {e["name"] for e in cur.get("us_releases") or [] if -15 <= e.get("minutes_away", 99) <= 0}
    released -= {e["name"] for e in prev.get("us_releases") or [] if -15 <= e.get("minutes_away", 99) <= 0}
    out += [f"{name} was just released" for name in sorted(released)]
    if not out and last_look and (now - last_look) >= dt.timedelta(minutes=HEARTBEAT_MINUTES):
        out.append(f"routine check: {HEARTBEAT_MINUTES} minutes since the last look")
    return out
