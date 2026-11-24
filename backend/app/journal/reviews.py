"""Daily and weekly reviews (guide sections 3.10 and 3.11).

The statistics are computed here, in code. GPT-5 only turns them into plain words
and, weekly, suggests at most three hypotheses. A hypothesis is stored in
`proposals` as "new" and UNTESTED: it changes nothing until it has passed a
backtest on data it was not found in, a paper period, and your approval (Phase 7).
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

from app.ai.client import AIError, chat_json, chat_model, strict_object
from app.ai.grounding import ungrounded
from app.ai.knowledge import R_NOTE
from app.config import get_settings
from app.core.calendar import IST
from app.db import db, eq


MIN_TRADES_FOR_PROPOSALS = 20


def _range_utc(start: dt.date, end: dt.date) -> tuple[str, str]:
    a = dt.datetime.combine(start, dt.time(0, 0), IST).astimezone(dt.timezone.utc).isoformat()
    b = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(0, 0), IST).astimezone(dt.timezone.utc).isoformat()
    return a, b


def _bucket(iso: str) -> str:
    t = dt.datetime.fromisoformat(iso).astimezone(IST)
    m = t.hour * 60 + t.minute
    return "9:15-10:30" if m < 630 else ("10:30-12:00" if m < 720 else ("12:00-14:00" if m < 840 else "after 14:00"))


def stats(user_id: str, start: dt.date, end: dt.date) -> dict:
    a, b = _range_utc(start, end)
    d = db()
    trades = d.select("trades", {"user_id": eq(user_id), "and": f'(exit_ts.gte."{a}",exit_ts.lt."{b}")'},
                      order="entry_ts")
    journal = {j["trade_id"]: j for j in d.select("trade_journal", {"user_id": eq(user_id)},
                                                   columns="trade_id,mistake_tags,user_notes,emotion")} if trades else {}
    n = len(trades)
    if n == 0:
        return {"period": f"{start} to {end}", "trades": 0}
    nets = [float(t["net_pnl"] or 0) for t in trades]
    rs = [float(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    by_strategy: dict = defaultdict(lambda: {"trades": 0, "net": 0.0, "wins": 0})
    by_time: dict = defaultdict(lambda: {"trades": 0, "net": 0.0})
    for t, net in zip(trades, nets):
        s = (t.get("entry_context") or {}).get("strategy") or (t.get("entry_context") or {}).get("source") or "manual"
        by_strategy[s]["trades"] += 1
        by_strategy[s]["net"] = round(by_strategy[s]["net"] + net, 2)
        by_strategy[s]["wins"] += net > 0
        tb = _bucket(t["entry_ts"])
        by_time[tb]["trades"] += 1
        by_time[tb]["net"] = round(by_time[tb]["net"] + net, 2)
    tags = Counter(tag for t in trades for tag in (journal.get(t["id"], {}).get("mistake_tags") or []))
    best = max(trades, key=lambda t: float(t["net_pnl"] or 0))
    worst = min(trades, key=lambda t: float(t["net_pnl"] or 0))
    return {
        "period": f"{start} to {end}",
        "trades": n,
        "wins": sum(1 for x in nets if x > 0),
        "losses": sum(1 for x in nets if x <= 0),
        "net_pnl_rupees": round(sum(nets), 2),
        "charges_rupees": round(sum(float(t["costs"] or 0) for t in trades), 2),
        "average_R": round(sum(rs) / len(rs), 3) if rs else None,
        "exit_reasons": dict(Counter(t["exit_reason"] for t in trades)),
        "rules_followed_pct": round(100 * sum(1 for t in trades if t.get("rules_followed")) / n, 1),
        "by_strategy": dict(by_strategy),
        "by_entry_time": dict(by_time),
        "mistake_tags": dict(tags),
        "best_trade": {"symbol": best["symbol"], "net_pnl_rupees": best["net_pnl"], "R": best["r_multiple"]},
        "worst_trade": {"symbol": worst["symbol"], "net_pnl_rupees": worst["net_pnl"], "R": worst["r_multiple"]},
        "trader_notes": [journal[t["id"]]["user_notes"] for t in trades if journal.get(t["id"], {}).get("user_notes")][:10],
    }


DAILY = strict_object({
    "headline": {"type": "string"},
    "what_happened": {"type": "string"},
    "discipline": {"type": "string", "description": "how well the rules were followed, from the numbers"},
    "patterns": {"type": "array", "items": {"type": "string"}},
    "for_tomorrow": {"type": "array", "items": {"type": "string"}, "description": "at most 3 concrete points"},
})

WEEKLY = strict_object({
    "headline": {"type": "string"},
    "what_happened": {"type": "string"},
    "discipline": {"type": "string"},
    "patterns": {"type": "array", "items": {"type": "string"}},
    "proposals": {"type": "array", "items": strict_object({
        "hypothesis": {"type": "string", "description": "a testable statement, e.g. 'ORB entries after 12:00 lose money'"},
        "strategy_id": {"type": "string"},
        "change": {"type": "string", "description": "the exact rule change to test"},
        "evidence": {"type": "string", "description": "which numbers in the stats suggest it"},
    }), "description": "at most 3"},
})

SYSTEM = (
    "You are a trading coach reviewing an Indian retail trader's paper trades. Use ONLY the statistics in the JSON. "
    "Every number you write must appear in it. Small samples prove nothing: say so when there are few trades. "
    "Never recommend buying or selling any stock. Be direct, short and kind. Use plain words, never JSON field "
    "names (say 'rules followed on 100% of trades', not 'rules_followed_pct'). A hypothesis must be a testable "
    "trading-rule change, not a comment on a single trade. " + R_NOTE
)


def _save(user_id: str, kind: str, start: dt.date, end: dt.date, st: dict, body: dict, model: str | None) -> dict:
    return db().insert("reviews", {"user_id": user_id, "kind": kind, "period_start": start.isoformat(),
                                   "period_end": end.isoformat(), "stats": st, "body": body, "model": model})[0]


def _write(user_id: str, kind: str, start: dt.date, end: dt.date, schema: dict) -> dict:
    st = stats(user_id, start, end)
    if st["trades"] == 0:
        body = {"headline": "No trades closed in this period.", "what_happened": "Nothing to review.",
                "discipline": "", "patterns": [], "for_tomorrow": [], "proposals": []}
        return _save(user_id, kind, start, end, st, body, None)
    model = None
    body = None
    if get_settings().llm_configured:
        try:
            body = chat_json(SYSTEM, json.dumps(st, default=str, ensure_ascii=False), schema, name=f"{kind}_review", purpose="review",
                             user_id=user_id, effort="medium" if kind == "weekly" else "low")
            if ungrounded(json.dumps(body, ensure_ascii=False), st):
                body = None
            else:
                model = chat_model()
        except AIError:
            body = None
    if body is None:
        body = {"headline": f"{st['trades']} trades, net {st['net_pnl_rupees']} rupees after {st['charges_rupees']} charges.",
                "what_happened": f"{st['wins']} won, {st['losses']} lost; exits: {st['exit_reasons']}.",
                "discipline": f"Rules followed on {st['rules_followed_pct']}% of trades.",
                "patterns": [], "for_tomorrow": [], "proposals": []}
    if body.get("proposals") and st["trades"] < MIN_TRADES_FOR_PROPOSALS:
        # a sample this small proves nothing: keep the review, drop the rule changes
        body["proposals"] = []
        body["proposals_note"] = (f"No rule changes proposed: {st['trades']} trades is too few "
                                  f"(at least {MIN_TRADES_FOR_PROPOSALS} needed).")
    review = _save(user_id, kind, start, end, st, body, model)
    for p in (body.get("proposals") or [])[:3]:
        db().insert("proposals", {"user_id": user_id, "strategy_id": None, "hypothesis": p["hypothesis"],
                                  "change": {"strategy": p["strategy_id"], "change": p["change"]},
                                  "evidence": {"from": f"weekly review {review['id']}", "note": p["evidence"],
                                               "tested": False}, "status": "new"})
    return review


def daily(user_id: str, day: dt.date | None = None) -> dict:
    day = day or dt.datetime.now(IST).date()
    return _write(user_id, "daily", day, day, DAILY)


def weekly(user_id: str, end: dt.date | None = None) -> dict:
    end = end or dt.datetime.now(IST).date()
    review = _write(user_id, "weekly", end - dt.timedelta(days=6), end, WEEKLY)
    try:
        from app.lab.loop import review_variants
        retired = review_variants(user_id)             # a variant losing money on paper is retired
        if retired:
            review["retired_variants"] = [f"{v['strategy_id']} v{v['version']}" for v in retired]
    except Exception:  # noqa: BLE001 - the review stands without the variant check
        pass
    return review
