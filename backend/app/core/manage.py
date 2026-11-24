"""Rule-based trade management: an open trade's stop can move, and a share can be booked, by rules fixed
BEFORE entry. The same functions run in the backtester (5-minute bars) and the paper trader (1-minute bars,
or the index level for option structures), so what was tested is what trades.

The rules are strategy parameters shared by every strategy (0 = off, the default, so nothing changes until
you set one; a set value is a new strategy version like any other parameter):

  breakeven_at_r     once the trade has been at +X R, the stop moves to the entry price
  trail_atr          after breakeven (or from the start when breakeven is off), the stop trails
                     N x ATR behind the best price since entry; it only ever tightens
  partial_at_r       a resting limit books `partial_pct` % of the position at +X R; the rest runs
  partial_pct
  time_stop_minutes  N minutes after entry, a trade that is not at least +0.3R is closed

Fill order inside one bar stays pessimistic: the stop in force when the bar OPENED is checked first (a bar
touching both the stop and the partial or target counts as the stop), then the partial, then the target.
A bar's high and low move the stop only from the NEXT bar on: inside a bar we cannot know whether the best
price came before or after the low. R is always measured on the ORIGINAL risk (entry to the first stop).
"""

from __future__ import annotations

from app.core.costs import round_to_tick

EXIT_DEFAULTS: dict = {"breakeven_at_r": 0.0, "trail_atr": 0.0, "partial_at_r": 0.0, "partial_pct": 50,
                       "time_stop_minutes": 0}
EXIT_HELP: dict = {
    "breakeven_at_r": "trade management: move the stop to the entry once the trade has been at +X R (0 = off)",
    "trail_atr": ("trade management: trail the stop N x ATR(14) behind the best price, starting at breakeven "
                  "(or at entry when breakeven_at_r is 0) (0 = off)"),
    "partial_at_r": "trade management: book part of the position at +X R, the rest runs (0 = off)",
    "partial_pct": "trade management: the share booked at partial_at_r, in % of the quantity",
    "time_stop_minutes": "trade management: exit if not at least +0.3R this many minutes after entry (0 = off)",
}
TIME_STOP_MIN_R = 0.3
ATR_PERIOD = 14


def rules_from(params: dict) -> dict | None:
    """The management rules a strategy's parameters switch on, or None when every rule is off."""
    r = {k: params.get(k, d) for k, d in EXIT_DEFAULTS.items()}
    active = r["breakeven_at_r"] > 0 or r["trail_atr"] > 0 or r["partial_at_r"] > 0 or r["time_stop_minutes"] > 0
    return r if active else None


def describe(rules: dict | None) -> str | None:
    if not rules:
        return None
    parts = []
    if rules.get("breakeven_at_r", 0) > 0:
        parts.append(f"stop to entry once at +{rules['breakeven_at_r']:g}R")
    if rules.get("trail_atr", 0) > 0:
        parts.append(f"then trail {rules['trail_atr']:g}x ATR behind the best price"
                     if rules.get("breakeven_at_r", 0) > 0 else f"trail {rules['trail_atr']:g}x ATR behind the best price")
    if rules.get("partial_at_r", 0) > 0:
        parts.append(f"book {rules.get('partial_pct', 50):g}% at +{rules['partial_at_r']:g}R")
    if rules.get("time_stop_minutes", 0) > 0:
        parts.append(f"exit after {int(rules['time_stop_minutes'])} min unless at +{TIME_STOP_MIN_R:g}R")
    return "; ".join(parts)


def start(rules: dict, *, long: bool, entry: float, stop: float, qty: int, lot: int = 1, atr: float | None = None,
          entry_t: int = 0, target_r: float | None = None, tick: float = 0.05) -> dict:
    """A plain-dict state (it is stored on the paper trade's entry_context as JSON)."""
    risk = abs(entry - stop)
    m = {"rules": dict(rules), "long": long, "entry": entry, "initial_stop": stop, "stop": stop, "risk": risk,
         "atr": atr, "entry_t": int(entry_t), "tick": tick, "best": entry, "breakeven": False, "time_checked": False,
         "partial": None, "partial_qty": 0, "moves": [], "notes": []}
    pr = rules.get("partial_at_r", 0)
    if pr > 0:
        q = int(qty * rules.get("partial_pct", 50) / 100 // lot) * lot
        if target_r is not None and pr >= target_r:
            m["notes"].append(f"partial at +{pr:g}R skipped: the target is at {target_r:g}R")
        elif q < lot or q >= qty:
            m["notes"].append(f"partial skipped: {rules.get('partial_pct', 50):g}% of {qty} is not a whole part")
        else:
            m["partial_qty"] = q
    if rules.get("trail_atr", 0) > 0 and not atr:
        m["notes"].append("trailing off: no ATR at entry")
    return m


def r_at(m: dict, price: float) -> float:
    return ((price - m["entry"]) if m["long"] else (m["entry"] - price)) / m["risk"] if m["risk"] else 0.0


def partial_level(m: dict) -> float | None:
    """The price of the resting partial limit, while it has not filled."""
    if m["partial"] is not None or m["partial_qty"] <= 0:
        return None
    k = m["rules"]["partial_at_r"] * m["risk"]
    return m["entry"] + k if m["long"] else m["entry"] - k


def partial_hit(m: dict, high: float, low: float) -> bool:
    lvl = partial_level(m)
    if lvl is None:
        return False
    return high > lvl if m["long"] else low < lvl       # a limit fills only if price trades THROUGH it


def book_partial(m: dict, t: int, price: float | None = None) -> dict:
    p = {"t": int(t), "qty": m["partial_qty"], "price": round(price if price is not None else partial_level(m), 2),
         "at_r": m["rules"]["partial_at_r"]}
    m["partial"] = p
    return p


def time_stop_due(m: dict, close: float, bar_end_t: int) -> bool:
    """True once, at the first bar that closes `time_stop_minutes` or more after entry, if the trade is below +0.3R."""
    n = m["rules"].get("time_stop_minutes", 0)
    if n <= 0 or m["time_checked"] or bar_end_t - m["entry_t"] < n * 60:
        return False
    m["time_checked"] = True
    return r_at(m, close) < TIME_STOP_MIN_R


def after_bar(m: dict, high: float, low: float, t: int) -> list[dict]:
    """Fold a finished bar's extremes into the best price, then move the stop for the bars that follow.
    Returns the moves made (each also appended to m["moves"])."""
    long, rules = m["long"], m["rules"]
    m["best"] = max(m["best"], high) if long else min(m["best"], low)
    moves = []
    be = rules.get("breakeven_at_r", 0)
    if be > 0 and not m["breakeven"] and r_at(m, m["best"]) >= be:
        m["breakeven"] = True
        if (m["entry"] > m["stop"]) if long else (m["entry"] < m["stop"]):
            moves.append(_move(m, m["entry"], t, f"breakeven: the trade reached +{be:g}R"))
    trail = rules.get("trail_atr", 0)
    if trail > 0 and m["atr"] and (m["breakeven"] or be <= 0):
        raw = m["best"] - trail * m["atr"] if long else m["best"] + trail * m["atr"]
        new = round_to_tick(raw, m["tick"], up=not long)
        if (new > m["stop"] + 1e-9) if long else (new < m["stop"] - 1e-9):
            moves.append(_move(m, new, t, f"trail: {trail:g}x ATR ({m['atr']:,.2f}) behind the best price {m['best']:,.2f}"))
    return moves


def _move(m: dict, new: float, t: int, why: str) -> dict:
    mv = {"t": int(t), "from": round(m["stop"], 2), "to": round(new, 2), "why": why}
    m["stop"] = new
    m["moves"].append(mv)
    return mv


def summary(m: dict) -> dict:
    """What a closed trade keeps: the rules, the stop's path and the partial."""
    return {"rules": m["rules"], "initial_stop": round(m["initial_stop"], 2), "final_stop": round(m["stop"], 2),
            "stop_moves": m["moves"], "partial": m["partial"], "notes": m["notes"]}
