"""Post-mortems and daily / weekly reviews for global paper trades, in dollars, with no NSE language.

As on NSE: code computes the facts, the model (GPT-5) only writes them up and picks mistake tags from a fixed list, and a
reply that quotes a number not in the facts is thrown away for a plain template. Reviews never propose rule changes for
gold: a change would be a new strategy version that must go through the Lab (plan section 4).
Tables: `global_trade_journal`, `global_reviews` (migration 0007).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from app.ai.client import AIError, chat_json, chat_model, strict_object
from app.ai.grounding import ungrounded
from app.config import get_settings
from app.db import db, eq
from app.global_markets import macro
from app.global_markets.clock import IST, NEW_YORK, session_name, trading_day
from app.global_markets.data import feed

log = logging.getLogger("global.journal")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="global-postmortem")

MISTAKE_TAGS = [
    "no mistake",
    "entered late (price had moved past the plan)",
    "stop too tight for normal noise",
    "target too far for the time left",
    "traded into a US release (CPI, jobs, Fed)",
    "traded in the quiet Asian hours",
    "exited early by hand",
    "held to the time exit without a reason",
    "rule not followed (manual override)",
    "no reason written for the trade",
]

R_NOTE = ("R is the result divided by the dollars at risk (entry to stop times ounces, plus the assumed slippage). A trade "
          "stopped exactly at its stop shows about -1R; a jump past the stop makes it worse. The assumed spread is already "
          "in the entry and exit prices. ")

SCHEMA = strict_object({
    "summary": {"type": "string", "description": "3 to 5 plain sentences: what the trade was, what happened, why"},
    "went_right": {"type": "array", "items": {"type": "string"}},
    "went_wrong": {"type": "array", "items": {"type": "string"}},
    "lesson": {"type": "string", "description": "one concrete lesson, or 'none' if the trade was fine and simply lost"},
    "mistake_tags": {"type": "array", "items": {"type": "string", "enum": MISTAKE_TAGS}},
})

SYSTEM = (
    "You review one finished intraday paper trade in gold (XAUUSD), priced in US dollars, for a retail trader in India, in "
    "plain English. Use ONLY the facts in the JSON you are given. Do not invent prices, percentages, statistics or news. "
    "Every number you write must appear in the facts. A loss is not automatically a mistake: a well-taken trade that hits "
    "its stop is normal. Be specific and kind, never preachy. Pick mistake tags only from the allowed list; use "
    "'no mistake' when the rules were followed and the setup simply did not work. " + R_NOTE
)

SESSIONS = {"asia": "Asian session", "london": "London session", "new_york": "New York session", None: "market closed"}


def _t(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso)


def facts_for(trade: dict, bars=None) -> dict:
    ctx = trade.get("entry_context") or {}
    card = ctx.get("card") or {}
    entry_t, exit_t = _t(trade["entry_ts"]), _t(trade["exit_ts"])
    long = trade["side"] == "buy"
    risk_oz = abs(float(trade["entry"]) - float(ctx.get("stop") or trade["entry"])) or None
    f: dict = {
        "symbol": trade["symbol"], "side": trade["side"], "lots": trade["lots"], "ounces": trade["units"],
        "prices_from": "COMEX futures (GC=F), about 10 minutes late" if trade.get("price_source") == "futures" else "spot",
        "entry_time": f"{entry_t.astimezone(IST):%H:%M} IST ({entry_t.astimezone(NEW_YORK):%H:%M} New York)",
        "exit_time": f"{exit_t.astimezone(IST):%H:%M} IST ({exit_t.astimezone(NEW_YORK):%H:%M} New York)",
        "session_at_entry": SESSIONS.get(session_name(entry_t), "market closed"),
        "minutes_held": int((exit_t - entry_t).total_seconds() // 60),
        "entry_price": trade["entry"], "exit_price": trade["exit"], "stop": ctx.get("stop"), "target": ctx.get("target"),
        "exit_reason": {"time": "flat-by time", "stop": "stop", "target": "target", "manual": "closed by hand",
                        "kill switch": "kill switch", "session-end": "the session ended"}.get(trade["exit_reason"], trade["exit_reason"]),
        "gross_pnl_dollars": trade["gross_pnl"], "assumed_costs_dollars": trade["costs"], "net_pnl_dollars": trade["net_pnl"],
        "result_in_R": trade["r_multiple"],
        "worst_move_against_R": round(float(trade["mae"] or 0) / risk_oz, 2) if risk_oz else None,
        "best_move_in_favour_R": round(float(trade["mfe"] or 0) / risk_oz, 2) if risk_oz else None,
        "rules_followed": trade.get("rules_followed"),
        "taken_from": card.get("source") or "manual ticket",
        "reason_written": card.get("why") or ctx.get("why") or None,
        "us_releases_during_trade": [f"{e.name} at {e.at.astimezone(IST):%H:%M} IST"
                                     for e in macro.between(entry_t - macro.WARN_AFTER, exit_t)],
    }
    if card:
        f["context_at_entry"] = card.get("context", [])
        f["typical_5_minute_move_dollars"] = card.get("typical_5m_move")
        f["planned_entry"] = (card.get("plan") or {}).get("entry")
        if f["planned_entry"]:
            f["entry_vs_plan_dollars"] = round(float(trade["entry"]) - float(f["planned_entry"]), 2)
    if ctx.get("override"):
        f["risk_check_overridden"] = {"blocked_because": (ctx["override"] or {}).get("reasons", []),
                                      "your_note": (ctx["override"] or {}).get("note") or None}
    # after a stop: did price go on to reach the target before the day's flat-by?
    if trade["exit_reason"] == "stop" and ctx.get("target") is not None:
        try:
            bs = bars if bars is not None else feed.series(trade["symbol"], "1m", trade.get("price_source")).bars
            day = trading_day(exit_t)
            later = [b for b in bs if b.t > int(exit_t.timestamp())
                     and trading_day(dt.datetime.fromtimestamp(b.t, dt.timezone.utc)) == day]
            tgt = float(ctx["target"])
            f["target_reached_after_stop"] = any((b.h >= tgt) if long else (b.l <= tgt) for b in later) if later else None
        except Exception:  # noqa: BLE001 - a missing fact, not a failed post-mortem
            f["target_reached_after_stop"] = None
    return f


def template(f: dict) -> dict:
    won = (f.get("net_pnl_dollars") or 0) > 0
    s = (f"{f['side'].upper()} {f['lots']} lot ({f['ounces']} oz) {f['symbol']} at {f['entry_price']} ({f['entry_time']}), "
         f"closed at {f['exit_price']} ({f['exit_time']}) by {f['exit_reason']} after {f['minutes_held']} minutes: "
         f"{f['net_pnl_dollars']} dollars after {f['assumed_costs_dollars']} assumed costs ({f['result_in_R']} R).")
    tags = []
    if not f.get("rules_followed", True):
        tags.append("rule not followed (manual override)")
    if not f.get("reason_written") or str(f["reason_written"]).startswith("No reason was written"):
        tags.append("no reason written for the trade")
    if f.get("us_releases_during_trade"):
        tags.append("traded into a US release (CPI, jobs, Fed)")
    return {"summary": s, "went_right": ["The trade had a stop and was sized to the max loss."],
            "went_wrong": [] if won else ["The trade did not work."], "lesson": "none", "mistake_tags": tags or ["no mistake"]}


def write(trade_id: str) -> dict:
    d = db()
    rows = d.select("global_trades", {"id": eq(trade_id)})
    if not rows:
        raise ValueError("no such trade")
    trade = rows[0]
    if not trade.get("exit_ts"):
        raise ValueError("the trade is still open")
    f = facts_for(trade)
    model, body = None, None
    if get_settings().llm_configured:
        try:
            body = chat_json(SYSTEM, json.dumps(f, default=str, ensure_ascii=False), SCHEMA, name="global_postmortem",
                             purpose="global_postmortem", user_id=trade["user_id"])
            bad = ungrounded(" ".join([body["summary"], *body["went_right"], *body["went_wrong"], body["lesson"]]), f)
            if bad:
                log.warning("post-mortem for %s quoted numbers not in the facts: %s", trade_id, bad[:5])
                body = None
            else:
                model = chat_model()
        except AIError as e:
            log.warning("post-mortem model call failed: %s", e)
    body = body or template(f)
    tags = [t for t in body["mistake_tags"] if t in MISTAKE_TAGS] or ["no mistake"]
    text = body["summary"]
    if body["went_right"]:
        text += "\n\nWent right: " + "; ".join(body["went_right"])
    if body["went_wrong"]:
        text += "\n\nWent wrong: " + "; ".join(body["went_wrong"])
    row = {"trade_id": trade_id, "user_id": trade["user_id"], "ai_postmortem": text, "mistake_tags": tags,
           "lessons": None if body["lesson"] == "none" else body["lesson"], "ai_model": model or "template", "facts": f,
           "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    existing = d.select("global_trade_journal", {"trade_id": eq(trade_id)}, columns="user_notes")
    if existing:
        d.update("global_trade_journal", {"trade_id": eq(trade_id)}, row)
        row["user_notes"] = existing[0].get("user_notes")
    else:
        d.insert("global_trade_journal", row)
    return row


def schedule(trade_id: str) -> None:
    """Called when a global paper trade closes: write its post-mortem in the background."""
    def run():
        try:
            write(trade_id)
        except Exception as e:  # noqa: BLE001
            log.exception("global post-mortem failed for %s: %s", trade_id, e)
    _pool.submit(run)


def entry(user_id: str, trade_id: str) -> dict | None:
    rows = db().select("global_trade_journal", {"trade_id": eq(trade_id), "user_id": eq(user_id)})
    return rows[0] if rows else None


def save_notes(user_id: str, trade_id: str, notes: str) -> dict:
    d = db()
    if not d.select("global_trades", {"id": eq(trade_id), "user_id": eq(user_id)}):
        raise ValueError("no such trade")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if d.select("global_trade_journal", {"trade_id": eq(trade_id)}):
        return d.update("global_trade_journal", {"trade_id": eq(trade_id)}, {"user_notes": notes[:2000], "updated_at": now})[0]
    return d.insert("global_trade_journal", {"trade_id": trade_id, "user_id": user_id, "user_notes": notes[:2000],
                                             "updated_at": now})[0]


# ---------------------------------------------------------------- reviews

def stats(user_id: str, start: dt.date, end: dt.date) -> dict:
    """Closed paper trades whose trading day (it rolls at 17:00 New York) falls in [start, end]."""
    d = db()
    trades = [t for t in d.select("global_trades", {"user_id": eq(user_id), "mode": eq("paper")}, order="entry_ts")
              if t.get("exit_ts") and start.isoformat() <= (t.get("trading_day") or "") <= end.isoformat()]
    if not trades:
        return {"period": f"{start} to {end}", "trades": 0}
    journal = {j["trade_id"]: j for j in d.select("global_trade_journal", {"user_id": eq(user_id)})}
    nets = [float(t["net_pnl"] or 0) for t in trades]
    rs = [float(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    by_session: dict = defaultdict(lambda: {"trades": 0, "net_dollars": 0.0})
    for t, net in zip(trades, nets):
        s = SESSIONS.get(session_name(_t(t["entry_ts"])), "market closed")
        by_session[s]["trades"] += 1
        by_session[s]["net_dollars"] = round(by_session[s]["net_dollars"] + net, 2)
    tags = Counter(tag for t in trades for tag in (journal.get(t["id"], {}).get("mistake_tags") or []))
    best = max(trades, key=lambda t: float(t["net_pnl"] or 0))
    worst = min(trades, key=lambda t: float(t["net_pnl"] or 0))
    n = len(trades)
    return {
        "period": f"{start} to {end}", "trades": n,
        "wins": sum(1 for x in nets if x > 0), "losses": sum(1 for x in nets if x <= 0),
        "net_pnl_dollars": round(sum(nets), 2), "assumed_costs_dollars": round(sum(float(t["costs"] or 0) for t in trades), 2),
        "average_R": round(sum(rs) / len(rs), 3) if rs else None,
        "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        "rules_followed_pct": round(100 * sum(1 for t in trades if t.get("rules_followed")) / n, 1),
        "by_session_at_entry": dict(by_session),
        "mistake_tags": dict(tags),
        "best_trade": {"net_pnl_dollars": best["net_pnl"], "R": best["r_multiple"]},
        "worst_trade": {"net_pnl_dollars": worst["net_pnl"], "R": worst["r_multiple"]},
        "trader_notes": [journal[t["id"]]["user_notes"] for t in trades if journal.get(t["id"], {}).get("user_notes")][:10],
    }


REVIEW = strict_object({
    "headline": {"type": "string"},
    "what_happened": {"type": "string"},
    "discipline": {"type": "string", "description": "how well the rules were followed, from the numbers"},
    "patterns": {"type": "array", "items": {"type": "string"}},
    "next_time": {"type": "array", "items": {"type": "string"}, "description": "at most 3 concrete points"},
})

REVIEW_SYSTEM = (
    "You are a trading coach reviewing a retail trader's paper trades in gold (XAUUSD), in US dollars. Use ONLY the "
    "statistics in the JSON. Every number you write must appear in it. Small samples prove nothing: say so when there are "
    "few trades. Never tell the trader to buy or sell. Do not propose changes to strategy rules. Be direct, short and kind. "
    "Use plain words, never JSON field names. " + R_NOTE
)


def review(user_id: str, kind: str, end: dt.date | None = None) -> dict:
    if kind not in ("daily", "weekly"):
        raise ValueError("kind must be daily or weekly")
    end = end or trading_day(dt.datetime.now(dt.timezone.utc))
    start = end if kind == "daily" else end - dt.timedelta(days=6)
    st = stats(user_id, start, end)
    model, body = None, None
    if st["trades"] == 0:
        body = {"headline": "No gold trades closed in this period.", "what_happened": "Nothing to review.", "discipline": "",
                "patterns": [], "next_time": []}
    elif get_settings().llm_configured:
        try:
            body = chat_json(REVIEW_SYSTEM, json.dumps(st, default=str, ensure_ascii=False), REVIEW, name=f"global_{kind}_review",
                             purpose="global_review", user_id=user_id, effort="medium" if kind == "weekly" else "low")
            if ungrounded(json.dumps(body, ensure_ascii=False), st):
                body = None
            else:
                model = chat_model()
        except AIError:
            body = None
    if body is None:
        body = {"headline": f"{st['trades']} trades, net {st['net_pnl_dollars']} dollars after {st['assumed_costs_dollars']} assumed costs.",
                "what_happened": f"{st['wins']} won, {st['losses']} lost; exits: {st['exit_reasons']}.",
                "discipline": f"Rules followed on {st['rules_followed_pct']}% of trades.", "patterns": [], "next_time": []}
    return db().insert("global_reviews", {"user_id": user_id, "kind": kind, "period_start": start.isoformat(),
                                          "period_end": end.isoformat(), "stats": st, "body": body, "model": model})[0]


def reviews(user_id: str, limit: int = 20) -> list[dict]:
    return db().select("global_reviews", {"user_id": eq(user_id)}, order="created_at.desc", limit=limit)
