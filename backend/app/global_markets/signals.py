"""The "why this trade" card for a global ticket. Built by code from facts only: your reason, the plan in dollars, where the
price sits against today's levels, the typical move, US releases until flat-by, and the latest gold headlines. No model
writes it, so every number on it is one the app measured.

Confidence is never raised here. A manual ticket grades low: no tested strategy is behind it. A strategy signal brings its
grade from the shared fixed rule in `domain/signal.py`, on the research-days backtest (`scanner.py`).
"""

from __future__ import annotations

import datetime as dt

from app.global_markets import macro
from app.global_markets.clock import IST, NEW_YORK, session_name, trading_day
from app.market.models import Bar

SESSION_LABEL = {"asia": "Asian session (usually quiet)", "london": "London session",
                 "new_york": "New York session (the busiest hours)"}
ATR_BARS = 14


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _five_minute(bars: list[Bar]) -> list[tuple[float, float, float]]:
    """(high, low, close) of 5-minute buckets from 1-minute bars."""
    out: dict[int, list[float]] = {}
    for b in bars:
        k = b.t // 300
        if k not in out:
            out[k] = [b.h, b.l, b.c]
        else:
            g = out[k]
            g[0], g[1], g[2] = max(g[0], b.h), min(g[1], b.l), b.c
    return [tuple(out[k]) for k in sorted(out)]


def atr_5m(bars: list[Bar], n: int = ATR_BARS) -> float | None:
    """The average 5-minute true range over the last `n` five-minute bars: how far gold typically moves in 5 minutes."""
    hlc = _five_minute(bars)
    if len(hlc) < n + 1:
        return None
    trs = [max(h - lo, abs(h - pc), abs(lo - pc)) for (h, lo, _), (_, _, pc) in zip(hlc[1:], hlc[:-1])]
    return round(sum(trs[-n:]) / n, 2)


def levels(bars: list[Bar], now: dt.datetime) -> dict:
    """Today's range so far, the Asian range and yesterday's high, low and close, from 1-minute bars."""
    utc = dt.timezone.utc
    day = trading_day(now)
    by_day: dict[dt.date, list[Bar]] = {}
    for b in bars:
        if b.t + 60 > now.timestamp():
            continue
        by_day.setdefault(trading_day(dt.datetime.fromtimestamp(b.t, utc)), []).append(b)
    out: dict = {"trading_day": day.isoformat()}
    today = by_day.get(day, [])
    if today:
        out["today_high"], out["today_low"] = max(b.h for b in today), min(b.l for b in today)
        asia = [b for b in today if session_name(dt.datetime.fromtimestamp(b.t, utc)) == "asia"]
        if asia and session_name(now) != "asia":
            out["asia_high"], out["asia_low"] = max(b.h for b in asia), min(b.l for b in asia)
    earlier = sorted(d for d in by_day if d < day)
    if earlier:
        y = by_day[earlier[-1]]
        out.update(yesterday=earlier[-1].isoformat(), yesterday_high=max(b.h for b in y), yesterday_low=min(b.l for b in y),
                   yesterday_close=y[-1].c)
    return out


def _where(price: float, lv: dict) -> list[str]:
    lines = []
    if "today_high" in lv:
        lines.append(f"Today's range so far: {_money(lv['today_low'])} to {_money(lv['today_high'])}.")
    if "asia_high" in lv:
        pos = ("above" if price > lv["asia_high"] else "below" if price < lv["asia_low"] else "inside")
        lines.append(f"Asian range {_money(lv['asia_low'])} to {_money(lv['asia_high'])}: the entry is {pos} it.")
    if "yesterday_high" in lv:
        pos = ("above yesterday's high" if price > lv["yesterday_high"] else
               "below yesterday's low" if price < lv["yesterday_low"] else "inside yesterday's range")
        lines.append(f"Yesterday: high {_money(lv['yesterday_high'])}, low {_money(lv['yesterday_low'])}, "
                     f"close {_money(lv['yesterday_close'])}. The entry is {pos}.")
    return lines


def card(*, symbol: str, side: str, lots: float, units: float, limit: float, stop: float, target: float | None, why: str,
         max_loss: float | None, source: str, bars: list[Bar], now: dt.datetime, flat_by: dt.datetime | None,
         headlines: list[dict] | None = None, warnings: list[str] | None = None, strategy: str | None = None,
         confidence: dict | None = None) -> dict:
    long = side == "buy"
    risk = abs(limit - stop)
    reward = abs(target - limit) if target is not None else None
    atr = atr_5m(bars)
    lv = levels(bars, now)
    sess = session_name(now)
    events = macro.between(now - macro.WARN_AFTER, flat_by) if flat_by else macro.near(now)
    context = [f"Now: {now.astimezone(IST):%H:%M} IST ({now.astimezone(NEW_YORK):%H:%M} New York), "
               f"{SESSION_LABEL.get(sess or '', 'market closed')}."]
    context += _where(limit, lv)
    if atr:
        context.append(f"Gold's typical 5-minute move lately is {_money(atr)}; your stop is {risk / atr:.1f} of those away "
                       + ("(tight: ordinary noise can reach it)." if risk / atr < 1.5 else "."))
    plan = {"entry": limit, "stop": stop, "target": target, "risk_per_oz": round(risk, 2),
            "reward_per_oz": round(reward, 2) if reward is not None else None,
            "reward_risk": round(reward / risk, 2) if reward is not None and risk else None,
            "lots": lots, "ounces": units, "max_loss": max_loss,
            "flat_by_ist": flat_by.astimezone(IST).strftime("%H:%M") if flat_by else None}
    summary = (f"{'Buy' if long else 'Sell'} {lots:g} lot ({units:g} oz) {symbol} at about {_money(limit)}, stop {_money(stop)}"
               + (f", target {_money(target)}" if target is not None else ", no target (exit at the stop, by hand or at flat-by)")
               + (f". Max loss about {_money(max_loss)}." if max_loss is not None else "."))
    reason = why.strip() or "No reason was written. Every trade should say why: write it before you confirm."
    return {
        "title": f"{'Buy' if long else 'Sell'} {symbol}",
        "source": strategy or "manual ticket",
        "why": reason,
        "summary": summary,
        "plan": plan,
        "context": context,
        "levels": lv,
        "typical_5m_move": atr,
        "events": [e.to_dict(now) for e in events],
        "headlines": (headlines or [])[:3],
        "risks": list(warnings or []),
        "confidence": confidence or {"grade": "low", "why": "A manual trade has no tested strategy behind it, and none of gold's "
                                                            "strategies has passed its backtest yet."},
        "prices_from": "COMEX futures (GC=F), about 10 minutes late" if source == "futures" else "spot (Twelve Data)",
        "made_at": now.astimezone(dt.timezone.utc).isoformat(),
    }
