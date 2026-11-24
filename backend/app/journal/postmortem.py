"""Post-mortem for every closed trade (guide section 3.9).

Code computes the facts (how long it was held, how far it went against and for
you in R, whether the target was hit after you were stopped, how the entry
compared with the signal); GPT-5 writes the review from those facts only and
picks mistake tags from a fixed list. If the model is unavailable or quotes a
number that is not in the facts, a plain template is stored instead.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from app.ai.client import AIError, chat_json, chat_model, strict_object
from app.ai.grounding import ungrounded
from app.ai.knowledge import R_NOTE
from app.config import get_settings
from app.core.calendar import IST
from app.db import db, eq
from app.journal import memory
from app.market import feed, yahoo

log = logging.getLogger("postmortem")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="postmortem")

MISTAKE_TAGS = [
    "no mistake",
    "entered late (price had moved past the signal)",
    "stop too tight for normal noise",
    "target too far for the time left",
    "traded against the market's direction",
    "traded with results or news risk",
    "high volatility day",
    "exited early by hand",
    "held to the time exit without a reason",
    "rule not followed (manual override)",
    "setup was weak (low confidence evidence)",
]

SCHEMA = strict_object({
    "summary": {"type": "string", "description": "3 to 5 plain sentences: what the trade was, what happened, why"},
    "went_right": {"type": "array", "items": {"type": "string"}},
    "went_wrong": {"type": "array", "items": {"type": "string"}},
    "lesson": {"type": "string", "description": "one concrete lesson, or 'none' if the trade was fine and simply lost"},
    "mistake_tags": {"type": "array", "items": {"type": "string", "enum": MISTAKE_TAGS}},
})

SYSTEM = (
    "You review one finished intraday trade for an Indian retail trader, in plain English. "
    "Use ONLY the facts in the JSON you are given. Do not invent prices, percentages, statistics or news. "
    "Every number you write must appear in the facts. A loss is not automatically a mistake: a well-taken "
    "trade that hits its stop is normal. Be specific and kind, never preachy. Pick mistake tags only from the "
    "allowed list; use 'no mistake' when the rules were followed and the setup simply did not work. " + R_NOTE
)


def facts_for(trade: dict, signal: dict | None) -> dict:
    ctx = trade.get("entry_context") or {}
    entry_t = dt.datetime.fromisoformat(trade["entry_ts"]).astimezone(IST)
    exit_t = dt.datetime.fromisoformat(trade["exit_ts"]).astimezone(IST)
    long = trade["side"] == "buy"
    risk_share = abs(float(trade["entry"]) - float(ctx.get("stop") or trade["entry"])) or None
    f: dict = {
        "symbol": trade["symbol"], "side": trade["side"], "quantity": trade["qty"],
        "strategy": ctx.get("strategy") or ctx.get("source") or "manual",
        "entry_time": entry_t.strftime("%H:%M"), "exit_time": exit_t.strftime("%H:%M"),
        "minutes_held": int((exit_t - entry_t).total_seconds() // 60),
        "entry_price": trade["entry"], "exit_price": trade["exit"], "stop": ctx.get("stop"), "target": ctx.get("target"),
        "exit_reason": trade["exit_reason"], "gross_pnl_rupees": trade["gross_pnl"], "charges_rupees": trade["costs"],
        "net_pnl_rupees": trade["net_pnl"], "result_in_R": trade["r_multiple"],
        "worst_move_against_R": round(float(trade["mae"] or 0) / risk_share, 2) if risk_share else None,
        "best_move_in_favour_R": round(float(trade["mfe"] or 0) / risk_share, 2) if risk_share else None,
        "rules_followed": trade.get("rules_followed"),
    }
    if ctx.get("override"):
        f["risk_check_overridden"] = {"blocked_because": (ctx["override"] or {}).get("reasons", []),
                                      "your_note": (ctx["override"] or {}).get("note") or None}
    if ctx.get("kind") == "option":
        f.update({
            "instrument": "option structure", "structure": ctx.get("template"), "expiry": ctx.get("expiry"),
            "legs": [f"{lg['side']} {lg['lots']} lot {lg['strike']:g} {lg['kind']} at {lg['price']}" for lg in ctx.get("legs", [])],
            "net_premium_per_unit": ctx.get("net_premium"), "planned_loss_rupees": (ctx.get("exits") or {}).get("planned_loss"),
            "stop_at_pnl_rupees": (ctx.get("exits") or {}).get("stop_pnl"), "target_at_pnl_rupees": (ctx.get("exits") or {}).get("target_pnl"),
            "max_loss_at_expiry_rupees": ctx.get("max_loss"), "index_at_entry": ctx.get("spot_at_entry"),
            "index_at_exit": ctx.get("spot_at_exit"), "prices_source": ctx.get("source"),
            "worst_move_against_rupees": trade.get("mae"), "best_move_in_favour_rupees": trade.get("mfe"),
            "bid_ask_spread_cost_at_entry_rupees": ctx.get("spread_cost_entry"),
            "note_on_spreads": ("The position is valued at the price you could close it at (long legs at the bid, short "
                                "legs at the ask), so crossing each leg's bid-ask spread on the way in and out costs money "
                                "even if the index does not move; that is not the premium rising."),
        })
        f.pop("stop", None), f.pop("target", None), f.pop("worst_move_against_R", None), f.pop("best_move_in_favour_R", None)
    if signal:
        e = signal.get("explanation") or {}
        f["why_it_was_taken"] = e.get("rule")
        f["conditions"] = [f"{c['name']}: {c['detail']} ({'met' if c['met'] else 'not met'})" for c in e.get("conditions", [])]
        f["signal_entry_price"] = (e.get("plan") or {}).get("entry")
        f["confidence_at_signal"] = e.get("confidence")
        f["risks_at_signal"] = e.get("risks", [])
        if f["signal_entry_price"]:
            f["entry_vs_signal_pct"] = round((float(trade["entry"]) / float(f["signal_entry_price"]) - 1) * 100, 2)
    # after a stop: did price go on to reach the target later in the session?
    if trade["exit_reason"] == "stop" and ctx.get("target"):
        try:
            bars = [b for b in feed.candles(trade["symbol"], "1m", "5d").bars
                    if b.t > int(exit_t.timestamp()) and dt.datetime.fromtimestamp(b.t, IST).date() == exit_t.date()]
            tgt = float(ctx["target"])
            f["target_reached_after_stop"] = any((b.h >= tgt) if long else (b.l <= tgt) for b in bars) if bars else None
        except yahoo.MarketDataError:
            f["target_reached_after_stop"] = None
    return f


def _template(f: dict) -> dict:
    won = (f.get("net_pnl_rupees") or 0) > 0
    s = (f"{f['side'].upper()} {f['quantity']} {f['symbol']} at {f['entry_price']} ({f['entry_time']}), "
         f"closed at {f['exit_price']} ({f['exit_time']}) by {f['exit_reason']} after {f['minutes_held']} minutes: "
         f"{f['net_pnl_rupees']} rupees after {f['charges_rupees']} charges ({f['result_in_R']} R).")
    return {"summary": s, "went_right": ["The trade had a stop and was sized to the max loss."],
            "went_wrong": [] if won else ["The trade did not work."], "lesson": "none",
            "mistake_tags": ["no mistake"] if f.get("rules_followed", True) else ["rule not followed (manual override)"]}


def write(trade_id: str) -> dict:
    d = db()
    trade = d.select("trades", {"id": eq(trade_id)})[0]
    if not trade.get("exit_ts"):
        raise ValueError("the trade is still open")
    signal = None
    if trade.get("signal_id"):
        rows = d.select("signals", {"id": eq(trade["signal_id"])}, columns="explanation")
        signal = rows[0] if rows else None
    f = facts_for(trade, signal)
    model = None
    body = None
    if get_settings().llm_configured:
        try:
            body = chat_json(SYSTEM, json.dumps(f, default=str, ensure_ascii=False), SCHEMA, name="postmortem", purpose="postmortem",
                             user_id=trade["user_id"])
            text = " ".join([body["summary"], *body["went_right"], *body["went_wrong"], body["lesson"]])
            bad = ungrounded(text, f)
            if bad:
                log.warning("post-mortem for %s quoted numbers not in the facts: %s", trade_id, bad[:5])
                body = None
            else:
                model = chat_model()
        except AIError as e:
            log.warning("post-mortem model call failed: %s", e)
    body = body or _template(f)
    tags = [t for t in body["mistake_tags"] if t in MISTAKE_TAGS] or ["no mistake"]
    text = body["summary"]
    if body["went_right"]:
        text += "\n\nWent right: " + "; ".join(body["went_right"])
    if body["went_wrong"]:
        text += "\n\nWent wrong: " + "; ".join(body["went_wrong"])
    row = {"trade_id": trade_id, "user_id": trade["user_id"], "ai_postmortem": text, "mistake_tags": tags,
           "ai_model": model or "template", "lessons": None if body["lesson"] == "none" else body["lesson"],
           "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    existing = d.select("trade_journal", {"trade_id": eq(trade_id)}, columns="user_notes,emotion")
    if existing:
        d.update("trade_journal", {"trade_id": eq(trade_id)}, row)
        row.update(existing[0])
    else:
        d.insert("trade_journal", row)
    try:
        memory.store(trade["user_id"], trade, row)
    except Exception as e:  # noqa: BLE001 - the post-mortem is saved even if the memory step fails
        log.warning("memory store failed for %s: %s", trade_id, e)
    return {**row, "facts": f}


def schedule(trade_id: str) -> None:
    """Called when a paper trade closes: write its post-mortem in the background."""
    def run():
        try:
            write(trade_id)
        except Exception as e:  # noqa: BLE001
            log.exception("post-mortem failed for %s: %s", trade_id, e)
    _pool.submit(run)
