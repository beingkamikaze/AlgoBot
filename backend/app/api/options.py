from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.options import chain as ch
from app.options import structures as S
from app.paper import service
from app.paper.service import PaperError

router = APIRouter(tags=["options"])


def _chain(underlying: str, expiry: dt.date | None) -> ch.Chain:
    try:
        return ch.chain(underlying, expiry)
    except ch.ChainError as e:
        raise HTTPException(502, str(e))


@router.get("/options/recorder")
async def recorder_status() -> dict:
    from app.fno_research import recorder
    return await run_in_threadpool(recorder.status)


@router.get("/options/templates")
def templates() -> dict:
    return {"templates": S.TEMPLATES, "defaults": S.DEFAULT_PARAMS}


@router.get("/options/chain")
async def chain(underlying: str = Query("NIFTY", pattern="^[A-Z0-9&-]{2,20}$"), expiry: dt.date | None = None,
                width: int = Query(15, ge=3, le=40)) -> dict:
    c = await run_in_threadpool(_chain, underlying, expiry)
    atm = c.atm()
    ks = c.strikes()
    i = ks.index(atm)
    rows = []
    for k in ks[max(0, i - width): i + width + 1]:
        row = {"strike": k}
        for kind in ("CE", "PE"):
            q = c.quote(k, kind)
            if q:
                g = c.greeks(k, kind)
                row[kind] = {"ltp": q.ltp, "bid": q.bid, "ask": q.ask, "iv": q.iv, "oi": q.oi, "chg_oi": q.chg_oi,
                             "volume": q.volume, "spread_pct": q.spread_pct, "delta": round(g.delta, 3) if g else None,
                             "tradingsymbol": c.tradingsymbol(k, kind)}
        rows.append(row)
    return {"underlying": c.underlying, "expiry": c.expiry.isoformat(), "expiries": [e.isoformat() for e in c.expiries[:12]],
            "spot": c.spot, "forward": c.forward(), "as_of": c.as_of, "source": c.source, "lot_size": c.lot_size,
            "atm": atm, "pcr": c.pcr(), "max_pain": c.max_pain(), "atm_iv": c.atm_iv(), "rows": rows}


class BuildBody(BaseModel):
    underlying: str = Field(pattern="^[A-Z0-9&-]{2,20}$")
    expiry: dt.date | None = None
    template: str
    params: dict = Field(default_factory=dict)
    exits: dict = Field(default_factory=dict)
    signal: dict | None = None             # the signal card this ticket comes from (strategy, rule, levels)


@router.post("/options/build")
async def build(body: BuildBody) -> dict:
    def run():
        c = _chain(body.underlying, body.expiry)
        s = S.build(c, body.template, body.params)
        from app.paper.options import plan_exits
        return {**s.to_dict(), "exits": plan_exits(s, body.exits)}
    try:
        return await run_in_threadpool(run)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/paper/tickets/option")
async def option_ticket(body: BuildBody, user: User = Depends(current_user)) -> dict:
    try:
        return await run_in_threadpool(service.propose_structure, user.id, user.email, underlying=body.underlying,
                                       template=body.template, params=body.params, expiry=body.expiry,
                                       exit_overrides=body.exits, source="strategy" if body.signal else "manual",
                                       card=body.signal)
    except PaperError as e:
        raise HTTPException(422, str(e))
