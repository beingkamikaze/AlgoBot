from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.ai import analyst, explain
from app.ai.client import AIError, AIUnavailable, tokens_used_today
from app.ai.knowledge import INDICATORS
from app.auth import User, current_user
from app.config import get_settings
from app.db import DBError, db, eq
from app.journal import memory, postmortem, reviews

router = APIRouter(tags=["journal", "ai"])
UUID = re.compile(r"^[0-9a-f-]{36}$")


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except AIUnavailable as e:
        raise HTTPException(503, str(e))
    except AIError as e:
        raise HTTPException(502, str(e))
    except DBError as e:
        raise HTTPException(503, f"database: {e}")
    except ValueError as e:
        raise HTTPException(422, str(e))


def _own_trade(user_id: str, trade_id: str) -> dict:
    if not UUID.match(trade_id):
        raise HTTPException(404, "no such trade")
    rows = db().select("trades", {"id": eq(trade_id), "user_id": eq(user_id)})
    if not rows:
        raise HTTPException(404, "no such trade")
    return rows[0]


class SignalBody(BaseModel):
    signal: dict


@router.post("/ai/summary")
async def summary(body: SignalBody, user: User = Depends(current_user)) -> dict:
    return await _call(explain.summarize, body.signal, user.id)


@router.post("/journal/similar")
async def similar(body: SignalBody, user: User = Depends(current_user)) -> dict:
    return await _call(memory.similar, user.id, body.signal)


@router.get("/journal")
async def journal(user: User = Depends(current_user)) -> dict:
    def load():
        d = db()
        trades = d.select("trades", {"user_id": eq(user.id), "exit_ts": "not.is.null"}, order="exit_ts.desc", limit=100)
        notes = {j["trade_id"]: j for j in d.select("trade_journal", {"user_id": eq(user.id)})}
        revs = d.select("reviews", {"user_id": eq(user.id)}, order="created_at.desc", limit=20)
        props = d.select("proposals", {"user_id": eq(user.id)}, order="created_at.desc", limit=20)
        return {"trades": [{**t, "journal": notes.get(t["id"])} for t in trades], "reviews": revs, "proposals": props}
    return await _call(load)


@router.post("/journal/{trade_id}/postmortem")
async def rerun_postmortem(trade_id: str, user: User = Depends(current_user)) -> dict:
    _own_trade(user.id, trade_id)
    return await _call(postmortem.write, trade_id)


class Notes(BaseModel):
    user_notes: str = Field(default="", max_length=4000)
    emotion: str = Field(default="", max_length=40)


@router.put("/journal/{trade_id}")
async def save_notes(trade_id: str, body: Notes, user: User = Depends(current_user)) -> dict:
    trade = _own_trade(user.id, trade_id)

    def save():
        d = db()
        row = {"user_notes": body.user_notes, "emotion": body.emotion,
               "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        if d.select("trade_journal", {"trade_id": eq(trade_id)}, columns="trade_id"):
            out = d.update("trade_journal", {"trade_id": eq(trade_id)}, row)[0]
        else:
            out = d.insert("trade_journal", {"trade_id": trade_id, "user_id": user.id, **row})[0]
        try:
            memory.store(user.id, trade, out)       # your note becomes part of what similar signals recall
        except (AIError, DBError):
            pass
        return out
    return await _call(save)


class ReviewBody(BaseModel):
    kind: str = Field(pattern="^(daily|weekly)$")
    date: dt.date | None = None


@router.post("/reviews")
async def make_review(body: ReviewBody, user: User = Depends(current_user)) -> dict:
    fn = reviews.daily if body.kind == "daily" else reviews.weekly
    return await _call(fn, user.id, body.date)


class BriefBody(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=30)


@router.post("/ai/brief")
async def brief(body: BriefBody, user: User = Depends(current_user)) -> dict:
    return await _call(analyst.brief, user.id, body.symbols)


@router.get("/ai/brief/latest")
async def latest_brief(user: User = Depends(current_user)) -> dict | None:
    rows = await _call(db().select, "reviews", {"user_id": eq(user.id), "kind": eq("brief")}, "created_at.desc", 1)
    return rows[0] if rows else None


class AskBody(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


@router.post("/ai/ask")
async def ask(body: AskBody, user: User = Depends(current_user)) -> dict:
    return await _call(analyst.ask, user.id, user.email, body.question)


@router.get("/ai/status")
async def status(user: User = Depends(current_user)) -> dict:
    s = get_settings()
    used = await run_in_threadpool(tokens_used_today)
    return {"configured": s.llm_configured, "provider": s.llm_provider,
            "model": s.azure_openai_deployment if s.llm_provider == "azure" else s.openai_model,
            "tokens_today": used, "daily_cap": s.llm_daily_token_cap}


@router.get("/learn/indicators")
def indicators() -> dict:
    return INDICATORS
