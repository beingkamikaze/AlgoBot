"""/api/global/...: the global setup's own routes."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.config import get_settings
from app.db import DBError, db, eq
from app.global_markets import indicators, journal, macro, news, paper, research, scanner
from app.global_markets.agent import desk
from app.global_markets.clock import IST, day_window, market_status
from app.global_markets.costs import spread_by_hour
from app.global_markets.data import backfill, check, feed, store, twelvedata, yahoo_futures
from app.global_markets.data.bars import TF_SECONDS
from app.global_markets.instruments import resolve
from app.global_markets.strategies.library import describe

router = APIRouter(prefix="/global", tags=["global"])


def _instrument(symbol: str):
    try:
        return resolve(symbol)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/status")
def status() -> dict:
    s = market_status(dt.datetime.now(dt.timezone.utc))
    return {"is_open": s.is_open, "phase": s.phase, "session": s.session, "reason": s.reason,
            "trading_day": s.trading_day.isoformat(), "next_open": s.next_open.isoformat(), "next_close": s.next_close.isoformat(),
            "now_ist": s.now.astimezone(IST).isoformat()}


@router.get("/chart")
async def chart(symbol: str = "XAUUSD", tf: str = "5m", source: str | None = None, ind: str = Query("", max_length=300)) -> dict:
    inst = _instrument(symbol)
    if tf not in TF_SECONDS:
        raise HTTPException(422, f"tf must be one of {', '.join(TF_SECONDS)}")
    if source is not None and source not in feed.SOURCES:
        raise HTTPException(422, f"source must be one of {', '.join(feed.SOURCES)}")
    try:
        specs = indicators.parse_specs(ind)
    except indicators.SpecError as e:
        raise HTTPException(422, str(e))
    try:
        s = await run_in_threadpool(feed.series, inst.symbol, tf, source)
    except yahoo_futures.FuturesError as e:
        raise HTTPException(502, str(e))
    notes = list(s.notes)
    series = await run_in_threadpool(indicators.compute, s.bars, tf, specs, notes)
    return {"symbol": inst.symbol, "name": inst.name, "tf": tf, "range": "", "source": s.provider, "price_source": s.source,
            "delayed": s.delayed, "prev_close": s.prev_close, "last_price": s.last_price, "as_of": s.as_of,
            "bars": [asdict(b) for b in s.bars], "indicators": series, "notes": notes}


@router.get("/data/status")
def data_status(symbol: str = "XAUUSD") -> dict:
    inst = _instrument(symbol)
    cfg = get_settings()
    start = dt.date.fromisoformat(cfg.global_backfill_start)
    return {"history": backfill.coverage(inst.symbol, start),
            "download": {**backfill.state, "enabled": cfg.global_backfill_enabled},
            "live": {"twelvedata_configured": bool(cfg.twelvedata_api_key), "calls_today": twelvedata.calls_today(), "daily_limit": twelvedata.DAILY_CALLS},
            "default_source": feed.default_source()}


@router.get("/data/spread")
def data_spread(symbol: str = "XAUUSD", days: int = Query(60, ge=1, le=3650)) -> dict:
    """The real spread by New York hour, from stored bid and ask."""
    inst = _instrument(symbol)
    columns = store.read_columns(inst.symbol, int(time.time()) - days * 86400)
    if columns is None:
        return {"days": days, "minutes": 0, "by_new_york_hour": []}
    return {"days": days, "minutes": len(columns["t"]), "by_new_york_hour": spread_by_hour(columns)}


@router.get("/data/check")
async def data_check(symbol: str = "XAUUSD") -> dict:
    inst = _instrument(symbol)
    try:
        return await run_in_threadpool(check.compare_now, inst.symbol)
    except yahoo_futures.FuturesError as e:
        raise HTTPException(502, str(e))


@router.get("/strategies")
def strategies() -> list[dict]:
    return describe()


@router.get("/backtests")
def backtests(symbol: str = "XAUUSD") -> dict:
    return research.overview(_instrument(symbol).symbol)


@router.post("/backtests/run")
async def run_backtests(symbol: str = "XAUUSD") -> dict:
    """Runs every strategy, at its fixed settings, on research days only."""
    inst = _instrument(symbol)
    try:
        await run_in_threadpool(research.run_all, inst.symbol)
    except research.NoResearchData as e:
        raise HTTPException(409, str(e))
    return await run_in_threadpool(research.overview, inst.symbol)


# ---------------------------------------------------------------- paper trading (USD)

async def _paper(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except paper.PaperError as e:
        raise HTTPException(422, str(e))
    except DBError as e:
        msg = str(e)
        if "global_" in msg and ("does not exist" in msg or "schema cache" in msg or "404" in msg):
            if "route" in msg or "global_trades_mode" in msg:
                msg = "the MT5 demo columns are missing: run supabase/migrations/0011_mt5_demo.sql in the Supabase SQL editor"
            elif "global_agent_ideas" in msg:
                msg = "the agent table is missing: run supabase/migrations/0009_global_agent.sql in the Supabase SQL editor"
            elif "global_signals" in msg:
                msg = "the live signals table is missing: run supabase/migrations/0008_global_signals.sql in the Supabase SQL editor"
            elif "global_trade_journal" in msg or "global_reviews" in msg:
                msg = "the global journal tables are missing: run supabase/migrations/0007_global_journal.sql in the Supabase SQL editor"
            else:
                msg = "the global paper tables are missing: run supabase/migrations/0006_global_markets.sql in the Supabase SQL editor"
        raise HTTPException(503, f"database: {msg}")


class GlobalTicket(BaseModel):
    symbol: str = Field(default="XAUUSD", min_length=1, max_length=20)
    side: str = Field(pattern="^(buy|sell)$")
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    lots: float | None = Field(default=None, gt=0)
    why: str = Field(default="", max_length=300)


class Approval(BaseModel):
    override: bool = False
    reason: str = Field(default="", max_length=300)


@router.get("/paper")
async def paper_overview(user: User = Depends(current_user)) -> dict:
    return await _paper(paper.overview, user.id)


@router.post("/paper/tickets")
async def paper_ticket(body: GlobalTicket, user: User = Depends(current_user)) -> dict:
    return await _paper(paper.propose, user.id, symbol=body.symbol, side=body.side, stop=body.stop, target=body.target,
                        lots=body.lots, why=body.why)


@router.post("/paper/orders/{order_id}/approve")
async def paper_approve(order_id: str, body: Approval | None = None, user: User = Depends(current_user)) -> dict:
    b = body or Approval()
    return await _paper(paper.approve, user.id, order_id, override=b.override, reason=b.reason)


@router.post("/paper/orders/{order_id}/cancel")
async def paper_cancel(order_id: str, user: User = Depends(current_user)) -> dict:
    return await _paper(paper.cancel, user.id, order_id)


@router.post("/paper/positions/{trade_id}/close")
async def paper_close(trade_id: str, user: User = Depends(current_user)) -> dict:
    return await _paper(paper.close_position, user.id, trade_id)


@router.post("/paper/kill-switch")
async def paper_kill(user: User = Depends(current_user)) -> dict:
    return await _paper(paper.kill_switch, user.id)


class Route(BaseModel):
    route: str


@router.put("/paper/route")
async def paper_route(body: Route, user: User = Depends(current_user)) -> dict:
    """Where your confirmed tickets go: the simulator ("paper") or the MT5 demo account ("mt5_demo")."""
    return await _paper(paper.set_route, user.id, body.route)


@router.put("/paper/limits")
async def paper_limits(values: dict, user: User = Depends(current_user)) -> dict:
    return await _paper(paper.update_limits, user.id, values)


# ---------------------------------------------------------------- news, US releases, journal and reviews

@router.get("/news")
async def gold_news(symbol: str = "XAUUSD", days: int = 1) -> dict:
    """Gold headlines (Google News, cached 10 minutes). Leads, not facts."""
    return await run_in_threadpool(news.headlines, _instrument(symbol).symbol, max(1, min(days, 7)))


@router.get("/macro")
def us_releases(days: int = 14) -> dict:
    """US CPI, jobs and Fed decisions from the official schedules, from 15 minutes ago to `days` ahead."""
    now = dt.datetime.now(dt.timezone.utc)
    return {"events": [e.to_dict(now) for e in macro.upcoming(now, max(1, min(days, 90)))],
            "near": macro.warnings_for(now), "covered_until": macro.coverage_ends().isoformat(),
            "sources": ["https://www.bls.gov/schedule/news_release/cpi.htm",
                        "https://www.bls.gov/schedule/news_release/empsit.htm",
                        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"]}


class Notes(BaseModel):
    notes: str = Field(default="", max_length=2000)


def _journal_error(e: ValueError) -> HTTPException:
    return HTTPException(404 if "no such" in str(e) else 409, str(e))


@router.get("/paper/trades/{trade_id}/journal")
async def trade_journal(trade_id: str, user: User = Depends(current_user)) -> dict:
    return {"journal": await _paper(journal.entry, user.id, trade_id)}


@router.post("/paper/trades/{trade_id}/postmortem")
async def trade_postmortem(trade_id: str, user: User = Depends(current_user)) -> dict:
    """Write (or rewrite) a closed trade's post-mortem now."""
    rows = await _paper(lambda: db().select("global_trades", {"id": eq(trade_id), "user_id": eq(user.id)}, columns="id"))
    if not rows:
        raise HTTPException(404, "no such trade")
    try:
        return await _paper(journal.write, trade_id)
    except ValueError as e:
        raise _journal_error(e)


@router.put("/paper/trades/{trade_id}/notes")
async def trade_notes(trade_id: str, body: Notes, user: User = Depends(current_user)) -> dict:
    try:
        return await _paper(journal.save_notes, user.id, trade_id, body.notes)
    except ValueError as e:
        raise _journal_error(e)


@router.get("/reviews")
async def global_reviews(user: User = Depends(current_user)) -> dict:
    return {"reviews": await _paper(journal.reviews, user.id)}


@router.post("/reviews/{kind}")
async def write_review(kind: str, user: User = Depends(current_user)) -> dict:
    if kind not in ("daily", "weekly"):
        raise HTTPException(404, "kind must be daily or weekly")
    return await _paper(journal.review, user.id, kind)


# ---------------------------------------------------------------- live signals

@router.get("/signals")
async def live_signals(symbol: str = "XAUUSD", user: User = Depends(current_user)) -> dict:
    """Recent live signals from the gold strategies (fixed settings), newest first, and the scanner's last pass."""
    rows = await _paper(scanner.recent, _instrument(symbol).symbol)
    if rows:
        mine = await _paper(lambda: db().select("global_orders", {"user_id": eq(user.id)}, order="created_at.desc", limit=100))
        taken = {(o.get("params") or {}).get("signal_id"): o["state"] for o in mine}
        for r in rows:
            r["your_ticket"] = taken.get(r["id"])
    now = dt.datetime.now(dt.timezone.utc)
    w = market_status(now)
    return {"signals": rows, "scanner": scanner.scan_state, "market_open": w.is_open,
            "strategies": [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in describe()]}


@router.post("/signals/{signal_id}/ticket")
async def ticket_from_signal(signal_id: str, user: User = Depends(current_user)) -> dict:
    """Turn a live signal into a paper ticket: the strategy's stop and its target in R from the ticket's own entry. It still
    needs the risk check and your confirmation."""
    s = await _paper(scanner.get, signal_id)
    if not s:
        raise HTTPException(404, "no such signal")
    if s["state"] != "new":
        raise HTTPException(409, "this signal has expired: the moment it was tested for has passed")
    e = s.get("explanation") or {}
    return await _paper(paper.propose, user.id, symbol=s["symbol"], side=s["side"], stop=float(s["stop"]),
                        target_r=float(s["target_r"]), why=e.get("rule") or "", signal=s)


# ---------------------------------------------------------------- the gold agent (paper)

async def _desk(fn, *args, **kwargs):
    try:
        return await _paper(fn, *args, **kwargs)
    except desk.AgentError as e:
        raise HTTPException(409, str(e))


def _taken(user_id: str) -> dict:
    mine = db().select("global_orders", {"user_id": eq(user_id)}, order="created_at.desc", limit=200)
    return {(o.get("params") or {}).get("idea_id"): o["state"] for o in mine if (o.get("params") or {}).get("idea_id")}


@router.get("/agent")
async def agent_desk(user: User = Depends(current_user)) -> dict:
    """The agent's latest looks (trade ideas and passes), what it is doing now, and its forward record."""
    ideas = await _desk(desk.recent)
    taken = await _desk(_taken, user.id)
    for r in ideas:
        r["your_ticket"] = taken.get(r["id"])
    now = dt.datetime.now(dt.timezone.utc)
    m = market_status(now)
    w = day_window(m.trading_day) if m.is_open else None
    s = get_settings()
    st = {k: v for k, v in desk.state.items() if k != "last_facts"}
    if st.get("last_look"):
        st["last_look"] = st["last_look"].isoformat()
    return {"ideas": ideas, "state": st, "record": await _desk(desk.record, set(taken)), "market_open": m.is_open,
            "in_window": bool(w and w.entry_start <= now < w.entry_end), "enabled": s.global_agent_enabled,
            "ai_ready": s.llm_configured, "looks_per_day": s.global_agent_looks_per_day,
            "cooldown_minutes": s.global_agent_cooldown_minutes, "idea_minutes": desk.IDEA_MINUTES}


@router.get("/agent/ideas/{idea_id}")
async def agent_idea(idea_id: str, user: User = Depends(current_user)) -> dict:
    """One look in full, with the facts the agent was given."""
    idea = await _desk(desk.get, idea_id)
    if not idea:
        raise HTTPException(404, "no such idea")
    return idea


@router.post("/agent/look")
async def agent_look(user: User = Depends(current_user)) -> dict:
    """Ask the agent to look at the market now (counts toward today's looks)."""
    idea = await _desk(desk.think, force=True)
    return {"idea": idea}


@router.post("/agent/ideas/{idea_id}/ticket")
async def ticket_from_idea(idea_id: str, user: User = Depends(current_user)) -> dict:
    """Turn a live agent idea into a paper ticket, priced now. It still needs the risk check and your confirmation."""
    return await _desk(desk.ticket, user.id, idea_id)


# ---------------------------------------------------------------- MT5 (demo only, read-only here)

def _mt5_status() -> dict:
    from app.global_markets.mt5.broker import BridgeClient, Mt5Broker, Mt5Error
    s = get_settings()
    out: dict = {"configured": bool(s.mt5_bridge_url and s.mt5_bridge_token), "connected": False, "error": None,
                 "paper_spread": paper.QUOTES.spread}
    if not out["configured"]:
        return out
    try:
        b = Mt5Broker(BridgeClient(s.mt5_bridge_url, s.mt5_bridge_token, timeout=5))
        h = b.connect()
        out.update(connected=True, account=h["account"], symbol=b.spec, quote=b.quote(),
                   positions=len(b.client.positions()), pending=len(b.client.orders()))
    except Mt5Error as e:
        out["error"] = str(e)
    return out


@router.get("/mt5")
async def mt5_status(user: User = Depends(current_user)) -> dict:
    """Is the MT5 demo bridge reachable, which account is it on, and what spread is the broker quoting right now?"""
    return await run_in_threadpool(_mt5_status)
