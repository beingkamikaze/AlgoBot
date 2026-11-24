"""The analyst's two open-ended jobs: answering questions and the pre-market brief.

Both get a context built by code (your account, trades, limits, reviews, backtest
results, the indicator guide; or market levels and news) and must answer from it.
Answers that quote numbers not in the context are flagged, not hidden.
"""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor

from app.ai.client import chat_json, chat_model, strict_object
from app.ai.grounding import ungrounded
from app.ai.knowledge import R_NOTE, as_text
from app.backtest import runner
from app.core.calendar import IST, market_status
from app.db import db, eq
from app.market import news, yahoo
from app.paper import service

ASK = strict_object({
    "answer": {"type": "string"},
    "based_on": {"type": "array", "items": {"type": "string"}, "description": "which parts of the context were used"},
})

ASK_SYSTEM = (
    "You are the analyst inside a personal trading app for NSE intraday equity and Nifty F&O (paper trading for now). "
    "Answer the trader's question using ONLY the context JSON: their account, limits, trades, journal, reviews, "
    "backtest results and the indicator guide. Every number you quote must be in the context. If the context does "
    "not contain the answer, say what is missing. Never tell them to buy or sell a specific stock, never predict "
    "prices, and never claim a strategy works without the backtest or paper numbers. Be short and plain: use words, "
    "not field names (say 'average R after costs', not 'expectancy_r'), and format rupees like ₹2,14,635. "
    "Quote times exactly as they appear in the context; they are India time (IST). " + R_NOTE
)


def ask(user_id: str, email: str | None, question: str) -> dict:
    ov = service.overview(user_id, email)
    d = db()
    journal = d.select("trade_journal", {"user_id": eq(user_id)}, columns="*")
    try:
        reviews = d.select("reviews", {"user_id": eq(user_id)}, order="created_at.desc", limit=3, columns="kind,period_end,stats,body")
    except Exception:  # noqa: BLE001 - the reviews table arrives with migration 0002
        reviews = []
    backtests = [{k: b.get(k) for k in ("strategy", "universe", "period", "trades", "expectancy_r", "profit_factor",
                                        "net_pnl", "verdict")} for b in runner.listing(10) if b.get("status") == "done"]
    def ist(iso):
        return dt.datetime.fromisoformat(iso).astimezone(IST).strftime("%Y-%m-%d %H:%M IST") if iso else None
    trades = [{**{k: t.get(k) for k in ("symbol", "side", "qty", "entry", "exit", "exit_reason", "net_pnl", "r_multiple")},
               "entry_time": ist(t.get("entry_ts")), "exit_time": ist(t.get("exit_ts"))} for t in ov["trades"][:30]]
    context = {
        "today_ist": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        "account": ov["account"], "limits": ov["limits"], "open_positions": ov["positions"],
        "recent_orders": [{"state": o["state"], "params": o["params"], "risk": (o.get("risk_verdict") or {}).get("reasons")}
                          for o in ov["orders"][:15]],
        "closed_trades": trades, "journal": journal[:30], "reviews": reviews, "backtests": backtests,
        "indicator_guide": as_text(),
    }
    out = chat_json(ASK_SYSTEM, json.dumps({"question": question, "context": context}, default=str, ensure_ascii=False), ASK,
                    name="ask", purpose="ask", user_id=user_id, effort="low")
    bad = ungrounded(out["answer"], context)
    return {**out, "model": chat_model(), "unverified_numbers": bad[:5]}


BRIEF = strict_object({
    "market": {"type": "string", "description": "two or three sentences on Nifty, Bank Nifty and India VIX"},
    "results_and_events": {"type": "array", "items": {"type": "string"}},
    "stock_notes": {"type": "array", "items": strict_object({"symbol": {"type": "string"}, "note": {"type": "string"}})},
    "watch_out": {"type": "array", "items": {"type": "string"}},
})

BRIEF_SYSTEM = (
    "You write a short pre-market brief for an Indian intraday trader from the JSON facts only: index levels, "
    "yesterday's moves, India VIX, and for each watchlist stock its NSE filings, next board meeting and headlines. "
    "Headlines are leads, not facts: say 'reported' for them. Every number must be in the facts. No predictions, "
    "no buy or sell advice. Mention a stock only if something in the facts concerns it."
)


def brief(user_id: str, symbols: list[str]) -> dict:
    symbols = [s.upper() for s in symbols if s.upper() not in ("NIFTY", "BANKNIFTY", "INDIAVIX", "FINNIFTY", "NIFTYIT")][:15]
    levels = {}
    for idx in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
        try:
            bars = yahoo.candles(idx, "1d", "1mo").bars
            last, prev = bars[-1], bars[-2]
            levels[idx] = {"name": {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank", "INDIAVIX": "India VIX"}[idx],
                           "date": dt.datetime.fromtimestamp(last.t, IST).date().isoformat(), "close": round(last.c, 2),
                           "change_pct": round((last.c / prev.c - 1) * 100, 2), "high": round(last.h, 2), "low": round(last.l, 2)}
        except (yahoo.MarketDataError, IndexError):
            levels[idx] = None
    with ThreadPoolExecutor(max_workers=5) as pool:
        stock_news = list(pool.map(lambda s: news.for_symbol(s, days=2), symbols))
    facts = {"generated_ist": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M"), "market_phase": market_status().phase,
             "indices": levels,
             "stocks": [{"symbol": n["symbol"], "filings": n["filings"][:5], "next_board_meeting": n["next_board_meeting"],
                         "headlines": [h["title"] + f" ({h['source']})" for h in n["headlines"][:5]]} for n in stock_news]}
    body = chat_json(BRIEF_SYSTEM, json.dumps(facts, default=str, ensure_ascii=False), BRIEF, name="brief", purpose="brief", user_id=user_id)
    bad = ungrounded(json.dumps(body, ensure_ascii=False), facts)
    today = dt.datetime.now(IST).date()
    row = db().insert("reviews", {"user_id": user_id, "kind": "brief", "period_start": today.isoformat(),
                                  "period_end": today.isoformat(), "stats": facts, "body": {**body, "unverified_numbers": bad[:5]},
                                  "model": chat_model()})[0]
    return row
