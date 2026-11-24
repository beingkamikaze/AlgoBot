from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.review import forward

router = APIRouter(prefix="/forward", tags=["forward"])


@router.get("")
async def overview(since: dt.date | None = Query(default=None, description="YYYY-MM-DD; default: your first paper trade"),
                   user: User = Depends(current_user)) -> dict:
    """Each strategy's closed paper trades next to what its backtest expected. Describes only: no grades, no tuning."""
    return await run_in_threadpool(forward.overview, user.id, since)
