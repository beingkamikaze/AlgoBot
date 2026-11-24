from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.db import DBError
from app.paper import service
from app.paper.service import PaperError

router = APIRouter(prefix="/paper", tags=["paper"])


async def _call(fn, *args, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except PaperError as e:
        raise HTTPException(422, str(e))
    except DBError as e:
        raise HTTPException(503, f"database: {e}")


@router.get("")
async def overview(user: User = Depends(current_user)) -> dict:
    return await _call(service.overview, user.id, user.email)


class TicketFromSignal(BaseModel):
    signal: dict                                   # the signal card exactly as /signals/scan returned it


class ManualTicket(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    side: str = Field(pattern="^(buy|sell)$")
    stop: float = Field(gt=0)
    target: float | None = Field(default=None, gt=0)
    qty: int | None = Field(default=None, ge=1)
    limit: float | None = Field(default=None, gt=0)
    reason: str = Field(default="", max_length=500)
    signal: dict | None = None             # the signal card a futures ticket comes from


@router.post("/tickets/from-signal")
async def ticket_from_signal(body: TicketFromSignal, user: User = Depends(current_user)) -> dict:
    s = body.signal
    try:
        plan = s["explanation"]["plan"]
        symbol, side = s["symbol"], s["side"]
    except (KeyError, TypeError):
        raise HTTPException(422, "not a signal card")
    return await _call(service.propose, user.id, user.email, symbol=symbol, side=side, stop=float(plan["stop"]),
                       target=plan.get("target"), source="strategy", card=s)


@router.post("/tickets/manual")
async def ticket_manual(body: ManualTicket, user: User = Depends(current_user)) -> dict:
    return await _call(service.propose, user.id, user.email, symbol=body.symbol.upper(), side=body.side,
                       stop=body.stop, target=body.target, qty=body.qty, limit=body.limit,
                       source="strategy" if body.signal else "manual", card=body.signal)


class Approve(BaseModel):
    override: bool = False                 # paper only: take a ticket blocked by one of your own limits
    reason: str = Field(default="", max_length=300)


@router.post("/orders/{order_id}/approve")
async def approve(order_id: str, body: Approve | None = None, user: User = Depends(current_user)) -> dict:
    b = body or Approve()
    return await _call(service.approve, user.id, user.email, order_id, b.override, b.reason)


@router.post("/orders/{order_id}/cancel")
async def cancel(order_id: str, user: User = Depends(current_user)) -> dict:
    return await _call(service.reject, user.id, order_id)


@router.post("/positions/{trade_id}/close")
async def close(trade_id: str, user: User = Depends(current_user)) -> dict:
    return await _call(service.close_position, user.id, trade_id)


@router.post("/kill-switch")
async def kill(user: User = Depends(current_user)) -> dict:
    return await _call(service.kill_switch, user.id)


@router.put("/limits")
async def limits(values: dict, user: User = Depends(current_user)) -> dict:
    return await _call(service.update_limits, user.id, user.email, values)


@router.post("/tick")
async def tick(user: User = Depends(current_user)) -> dict:
    """Run the simulator now instead of waiting for the next loop (handy when testing)."""
    return await _call(service.tick)
