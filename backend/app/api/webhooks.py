from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.db import DBError
from app.webhooks import tradingview as tv

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _call(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except tv.WebhookError as e:
        raise HTTPException(e.status, str(e))
    except DBError as e:
        raise HTTPException(503, f"database: {e} (have you run migration 0004_webhooks_lab.sql?)")


@router.post("/tradingview/{source_id}")
async def tradingview(source_id: str, request: Request) -> dict:
    """TradingView posts here. No session: the secret in the body is the credential."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "the alert message must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, "the alert message must be a JSON object")
    try:
        return await run_in_threadpool(tv.receive, source_id, body, {k.lower(): v for k, v in request.headers.items()},
                                       request.client.host if request.client else None)
    except tv.WebhookError as e:
        raise HTTPException(e.status, str(e))
    except DBError:
        raise HTTPException(503, "temporarily unavailable")


class NewSource(BaseModel):
    name: str = Field(min_length=2, max_length=60)


@router.get("/sources")
async def sources(user: User = Depends(current_user)) -> list[dict]:
    return await _call(tv.list_sources, user.id)


@router.post("/sources")
async def new_source(body: NewSource, user: User = Depends(current_user)) -> dict:
    """The secret is shown in this answer only; it is stored as a hash."""
    return await _call(tv.create_source, user.id, body.name)


class Enabled(BaseModel):
    enabled: bool


@router.put("/sources/{source_id}")
async def enable(source_id: str, body: Enabled, user: User = Depends(current_user)) -> dict:
    return await _call(tv.set_enabled, user.id, source_id, body.enabled)


class TestAlert(BaseModel):
    symbol: str
    side: str
    stop: float
    target: float | None = None
    price: float | None = None
    note: str = "test alert from the app"


@router.post("/sources/{source_id}/test")
async def test_alert(source_id: str, body: TestAlert, user: User = Depends(current_user)) -> dict:
    """Send a sample alert through the same path, without TradingView (no IP or secret check)."""
    rows = await _call(tv.list_sources, user.id)
    if source_id not in {r["id"] for r in rows}:
        raise HTTPException(404, "no such source")
    return await _call(tv.receive, source_id, {**body.model_dump(), "strategy": "test"}, {}, None, True)


@router.get("/inbox")
async def inbox(user: User = Depends(current_user)) -> dict:
    return await _call(tv.inbox, user.id)
