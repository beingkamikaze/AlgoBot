from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import User, optional_user
from app.backtest import runner
from app.signals import autoscan
from app.lab.loop import LabError
from app.lab.loop import params_for as lab_params
from app.core.instruments import INDEX_FUTURE_SYMBOLS
from app.market import nse
from app.signals.scan import scan
from app.options.backtest import OPTION_STRATEGIES
from app.options.backtest import describe as describe_options
from app.strategies.library import describe

router = APIRouter(tags=["strategies"])


@router.get("/strategies")
def strategies() -> list[dict]:
    return describe() + describe_options()


class BacktestRequest(BaseModel):
    strategy: str
    params: dict = Field(default_factory=dict)
    universe: str = "NIFTY50"           # NIFTY50, NIFTY100, NIFTYBANK or custom
    symbols: list[str] | None = None
    config: dict = Field(default_factory=dict)


@router.post("/backtests")
async def start_backtest(req: BacktestRequest) -> dict:
    allowed = {"risk_per_trade", "capital_per_trade", "slippage_bps", "max_trades_per_session"}
    if set(req.config) - allowed:
        raise HTTPException(422, f"config keys allowed: {', '.join(sorted(allowed))}")
    try:
        return await run_in_threadpool(runner.start, req.strategy, req.params, req.universe, req.symbols, req.config)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/backtests")
def list_backtests() -> list[dict]:
    return runner.listing()


@router.get("/backtests/{job_id}")
def get_backtest(job_id: str, trades: bool = True) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "no such backtest")
    job = runner.get(job_id, with_trades=trades)
    if job is None:
        raise HTTPException(404, "no such backtest")
    return job


@router.get("/signals/scan")
async def signals_scan(strategy: str = "orb", universe: str = "NIFTY50",
                       symbols: str | None = Query(None, max_length=2000),
                       at: str | None = Query(None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$"),
                       version: str | None = Query(None, max_length=200)) -> dict:
    if strategy in OPTION_STRATEGIES:
        raise HTTPException(422, "option strategies are backtest-only (modeled prices); build live option trades on the Options page")
    try:
        if universe == "INDEXFUT":
            syms = list(INDEX_FUTURE_SYMBOLS)
        elif universe == "custom" and symbols:
            syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            syms = await run_in_threadpool(nse.constituents, universe)
        params = await run_in_threadpool(lab_params, strategy, version) if version else {}
        return await run_in_threadpool(scan, strategy, params, syms[:100], at)
    except (ValueError, LabError) as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


class AutoScan(BaseModel):
    at: str | None = Field(None, pattern=r"^([01]?\d|2[0-3]):[0-5]\d$")


@router.post("/signals/autoscan")
async def autoscan_start(body: AutoScan, user: User | None = Depends(optional_user)) -> dict:
    """Every strategy (default + your approved lab versions) on the Nifty 100 and index futures, ranked."""
    return await run_in_threadpool(autoscan.start, user.id if user else None, body.at)


@router.get("/signals/autoscan/{job_id}")
async def autoscan_get(job_id: str, user: User | None = Depends(optional_user)) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "no such scan")
    j = autoscan.get(job_id, user.id if user else None)
    if j is None:
        raise HTTPException(404, "no such scan (it expires after an hour)")
    return j


@router.get("/fno/plan")
async def fno_plan(symbol: str = Query(..., pattern=r"^[A-Za-z0-9&-]{1,24}$"), side: str = Query(..., pattern="^(buy|sell)$"),
                   entry: float = Query(..., gt=0), stop: float = Query(..., gt=0), target: float | None = Query(None, gt=0),
                   user: User | None = Depends(optional_user)) -> dict:
    """The signal as futures and options: exact contracts, cost, estimated result at its stop and target."""
    from app.db import db, eq
    from app.risk.engine import Limits
    from app.signals import fno_plan as F
    lim = None
    if user:
        try:
            rows = await run_in_threadpool(lambda: db().select("risk_limits", {"user_id": eq(user.id)}))
            lim = Limits.from_row(rows[0]) if rows else None
        except Exception:  # noqa: BLE001 - without limits the plan just does not say whether it fits
            lim = None
    return await run_in_threadpool(F.plan, symbol, side, entry, stop, target, lim)
