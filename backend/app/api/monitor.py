from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.monitor import service

router = APIRouter(prefix="/monitor", tags=["monitor"])


@router.get("")
async def overview(user: User = Depends(current_user)) -> list[dict]:
    """Every open trade with its health and recent alerts."""
    return await run_in_threadpool(service.overview, user.id)


@router.get("/{trade_id}")
async def one(trade_id: str, user: User = Depends(current_user)) -> dict:
    try:
        return await run_in_threadpool(service.analyze_trade, user.id, trade_id)
    except LookupError as e:
        raise HTTPException(404, str(e))


class Link(BaseModel):
    strategy: str | None = None          # empty: match it from today's signals


@router.post("/{trade_id}/strategy")
async def link(trade_id: str, body: Link, user: User = Depends(current_user)) -> dict:
    """Link a trade to the strategy that took it (for trades whose ticket did not carry its signal)."""
    try:
        return await run_in_threadpool(service.link_strategy, user.id, trade_id, body.strategy)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
