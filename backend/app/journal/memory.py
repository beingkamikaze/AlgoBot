"""Trade memory (guide section 3.9): each closed trade becomes a short text, embedded
and stored in `trade_memory`; a new signal is described the same way and matched
against it. The numbers shown about similar trades come from the trades table,
never from the model."""

from __future__ import annotations

import datetime as dt
import logging

from app.ai.client import AIError, embed
from app.core.calendar import IST
from app.db import DBError, db, eq

log = logging.getLogger("memory")


def _bucket(t: dt.datetime) -> str:
    m = t.hour * 60 + t.minute
    return "first hour" if m < 10 * 60 + 30 else ("late morning" if m < 12 * 60 else ("midday" if m < 14 * 60 else "afternoon"))


def describe_trade(t: dict, journal: dict | None = None) -> str:
    ctx = t.get("entry_context") or {}
    entry_t = dt.datetime.fromisoformat(t["entry_ts"]).astimezone(IST)
    exit_t = dt.datetime.fromisoformat(t["exit_ts"]).astimezone(IST) if t.get("exit_ts") else None
    held = f"{int((exit_t - entry_t).total_seconds() // 60)} minutes" if exit_t else "still open"
    regime = t.get("regime") or {}
    parts = [
        f"{t['side'].upper()} {t['symbol']} ({ctx.get('strategy') or ctx.get('source') or 'manual'} strategy), "
        f"entered in the {_bucket(entry_t)} at {entry_t:%H:%M} IST on {entry_t:%A}",
        f"stop {ctx.get('stop')}, target {ctx.get('target')}",
        f"result: {t.get('exit_reason') or 'open'} after {held}, {t.get('r_multiple')} R after charges",
    ]
    if regime:
        parts.append(f"market: {regime.get('vix') or ''} {regime.get('nifty_gap') or ''}".strip())
    if ctx.get("summary"):
        parts.append(f"why it was taken: {ctx['summary']}")
    if journal:
        if journal.get("mistake_tags"):
            parts.append("mistakes: " + ", ".join(journal["mistake_tags"]))
        if journal.get("ai_postmortem"):
            parts.append("post-mortem: " + journal["ai_postmortem"][:600])
        if journal.get("user_notes"):
            parts.append("trader's note: " + journal["user_notes"][:400])
    return ". ".join(parts) + "."


def describe_signal(card: dict) -> str:
    e = card.get("explanation") or {}
    t = dt.datetime.fromisoformat(card["created_at"]).astimezone(IST)
    return (f"{card['side'].upper()} {card['symbol']} ({card.get('strategy_id')} strategy), entered in the {_bucket(t)} "
            f"at {t:%H:%M} IST on {t:%A}. why it was taken: {e.get('rule', '')} "
            + " ".join(f"{c['name']}: {c['detail']}." for c in e.get("conditions", [])))


def store(user_id: str, trade: dict, journal: dict | None) -> None:
    text = describe_trade(trade, journal)
    vec = embed([text], user_id=user_id)[0]
    d = db()
    d.delete("trade_memory", {"trade_id": eq(trade["id"])})
    ctx = trade.get("entry_context") or {}
    d.insert("trade_memory", {"user_id": user_id, "trade_id": trade["id"], "content": text, "embedding": vec,
                              "metadata": {"symbol": trade["symbol"], "side": trade["side"], "strategy": ctx.get("strategy"),
                                           "r": trade.get("r_multiple"), "exit_reason": trade.get("exit_reason"),
                                           "mode": trade.get("mode")}})


def similar(user_id: str, card: dict, k: int = 6) -> dict:
    """Nearest past trades in meaning, with their outcomes from the trades table."""
    try:
        vec = embed([describe_signal(card)], user_id=user_id)[0]
        hits = db().rpc("match_trade_memory", {"query_embedding": vec, "match_user": user_id, "match_count": k})
    except (AIError, DBError) as e:
        return {"available": False, "reason": str(e)[:200], "trades": []}
    if not hits:
        return {"available": True, "trades": [], "note": "No closed trades in your journal yet."}
    ids = ",".join(h["trade_id"] for h in hits if h.get("trade_id"))
    rows = {t["id"]: t for t in db().select("trades", {"id": f"in.({ids})"},
                                            columns="id,symbol,side,entry_ts,exit_reason,net_pnl,r_multiple")} if ids else {}
    out = []
    for h in hits:
        t = rows.get(h["trade_id"])
        if t:
            out.append({"trade_id": t["id"], "symbol": t["symbol"], "side": t["side"], "entry_ts": t["entry_ts"],
                        "exit_reason": t["exit_reason"], "net_pnl": t["net_pnl"], "r": t["r_multiple"],
                        "similarity": round(float(h["similarity"]), 3)})
    wins = sum(1 for t in out if (t["net_pnl"] or 0) > 0)
    rs = [t["r"] for t in out if t["r"] is not None]
    return {"available": True, "trades": out, "wins": wins, "losses": len(out) - wins,
            "average_r": round(sum(rs) / len(rs), 3) if rs else None}
