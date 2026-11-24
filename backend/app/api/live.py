from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.config import get_settings
from app.db import DBError
from app.live import service, session
from app.live.broker import BrokerError

router = APIRouter(prefix="/live", tags=["live"])


async def _call(fn, *args, **kw):
    try:
        return await run_in_threadpool(fn, *args, **kw)
    except (service.LiveOrderError, session.LiveError) as e:
        raise HTTPException(422, str(e))
    except BrokerError as e:
        raise HTTPException(502, str(e))
    except DBError as e:
        raise HTTPException(503, f"database: {e} (have you run migration 0005_live.sql?)")


@router.get("")
async def overview(user: User = Depends(current_user)) -> dict:
    return await _call(service.overview, user.id, user.aal)


@router.get("/kite/login")
async def kite_login(user: User = Depends(current_user)) -> dict:
    return {"url": await _call(session.login_url, user.id)}


@router.get("/kite/callback")
async def kite_callback(request_token: str = "", state: str = "", status: str = "") -> RedirectResponse:
    """Kite sends your browser here after you log in on kite.zerodha.com. No app session: `state` says who it is."""
    web = get_settings().web_url.rstrip("/")
    if status != "success" or not request_token:
        return RedirectResponse(f"{web}/live?kite=cancelled", status_code=303)
    try:
        await run_in_threadpool(session.complete_login, state, request_token)
    except session.LiveError as e:
        from urllib.parse import quote
        return RedirectResponse(f"{web}/live?kite=error&detail={quote(str(e)[:200])}", status_code=303)
    return RedirectResponse(f"{web}/live?kite=ok", status_code=303)


@router.post("/kite/logout")
async def kite_logout(user: User = Depends(current_user)) -> dict:
    await _call(session.logout, user.id)
    return {"logged_in": False}


class Arm(BaseModel):
    confirm: str


@router.post("/arm")
async def arm(body: Arm, user: User = Depends(current_user)) -> dict:
    return await _call(service.arm, user.id, user.aal, body.confirm)


@router.post("/disarm")
async def disarm(user: User = Depends(current_user)) -> dict:
    return await _call(service.disarm, user.id)


@router.post("/orders/{order_id}/preview")
async def preview(order_id: str, user: User = Depends(current_user)) -> dict:
    return await _call(service.preview, user.id, order_id)


class Approve(BaseModel):
    qty: int


@router.post("/orders/{order_id}/approve")
async def approve(order_id: str, body: Approve, user: User = Depends(current_user)) -> dict:
    return await _call(service.approve, user.id, order_id, body.qty, user.aal)


@router.post("/positions/{trade_id}/close")
async def close(trade_id: str, user: User = Depends(current_user)) -> dict:
    return await _call(service.close, user.id, trade_id)


@router.post("/kill")
async def kill(user: User = Depends(current_user)) -> dict:
    return await _call(service.kill_switch, user.id)


@router.post("/sync")
async def sync(user: User = Depends(current_user)) -> dict:
    return await _call(service.sync, user.id)


@router.put("/limits")
async def limits(body: dict, user: User = Depends(current_user)) -> dict:
    return await _call(service.update_live_limits, user.id, body)
