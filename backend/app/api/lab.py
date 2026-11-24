from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.db import DBError, db, eq
from app.lab import loop

router = APIRouter(prefix="/lab", tags=["lab"])


async def _call(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except loop.LabError as e:
        raise HTTPException(422, str(e))
    except DBError as e:
        raise HTTPException(503, f"database: {e}")


@router.get("")
async def overview(user: User = Depends(current_user)) -> dict:
    def load():
        return {"proposals": db().select("proposals", {"user_id": eq(user.id)}, order="created_at.desc", limit=50),
                "variants": loop.variants(user.id), "tests_this_month": loop.tests_this_month(user.id),
                "rules": {"min_held_back_trades": loop.MIN_OOS_TRADES, "min_improvement_r": loop.MIN_IMPROVEMENT_R,
                          "base_alpha": loop.BASE_ALPHA, "paper_trades_to_judge": loop.PAPER_TRADES_TO_JUDGE}}
    return await _call(load)


class NewProposal(BaseModel):
    hypothesis: str = Field(min_length=5, max_length=500)
    strategy_id: str
    params: dict


@router.post("/proposals")
async def create(body: NewProposal, user: User = Depends(current_user)) -> dict:
    return await _call(loop.create, user.id, body.hypothesis, body.strategy_id, body.params)


@router.post("/proposals/{pid}/translate")
async def translate(pid: str, user: User = Depends(current_user)) -> dict:
    return await _call(loop.translate, user.id, pid)


class TestBody(BaseModel):
    universe: str | None = None


@router.post("/proposals/{pid}/test")
async def test(pid: str, body: TestBody, user: User = Depends(current_user)) -> dict:
    return await _call(loop.start_test, user.id, pid, body.universe)


@router.post("/proposals/{pid}/approve")
async def approve(pid: str, user: User = Depends(current_user)) -> dict:
    return await _call(loop.approve, user.id, pid)


@router.post("/proposals/{pid}/reject")
async def reject(pid: str, user: User = Depends(current_user)) -> dict:
    return await _call(loop.reject, user.id, pid)


@router.post("/variants/review")
async def review(user: User = Depends(current_user)) -> list[dict]:
    return await _call(loop.review_variants, user.id)
