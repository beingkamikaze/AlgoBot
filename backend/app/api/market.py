from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.indicators.registry import DEFAULTS, SpecError, compute, parse_specs
from app.market import feed, nse, yahoo
from app.market.models import RANGES, TIMEFRAMES

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=40)) -> list[dict]:
    try:
        return await run_in_threadpool(nse.search, q)
    except RuntimeError as e:
        raise HTTPException(503, f"NSE equity list unavailable: {e}")


@router.get("/chart")
async def chart(symbol: str = Query(min_length=1, max_length=30), tf: str = "5m",
                range: str | None = None,  # noqa: A002 - the query parameter is called range
                ind: str = Query("", max_length=300)) -> dict:
    if tf not in TIMEFRAMES:
        raise HTTPException(422, f"tf must be one of {', '.join(TIMEFRAMES)}")
    if range is not None and range not in RANGES:
        raise HTTPException(422, f"range must be one of {', '.join(RANGES)}")
    try:
        specs = parse_specs(ind)
    except SpecError as e:
        raise HTTPException(422, str(e))
    try:
        c = await run_in_threadpool(feed.candles, symbol, tf, range)
    except yahoo.MarketDataError as e:
        raise HTTPException(502, f"{symbol}: {e}")
    notes: list[str] = []
    series = compute(c, specs, notes)
    return {
        "symbol": c.symbol, "name": c.name, "provider_symbol": c.provider_symbol, "tf": tf,
        "range": range or TIMEFRAMES[tf][2], "source": c.source, "source_label": feed.label(c.source), "delayed": c.delayed,
        "prev_close": c.prev_close, "last_price": c.last_price, "as_of": c.as_of,
        "bars": [asdict(b) for b in c.bars],
        "indicators": [asdict(s) for s in series],
        "notes": notes,
    }


@router.get("/quotes")
async def quotes(symbols: str = Query(min_length=1, max_length=2000)) -> list[dict]:
    syms = [s for s in (x.strip() for x in symbols.split(",")) if s][:100]
    try:
        return [asdict(q) for q in await run_in_threadpool(feed.quotes, syms)]
    except yahoo.MarketDataError as e:
        raise HTTPException(502, str(e))


@router.get("/holidays")
async def holidays(refresh: bool = False) -> dict[str, str]:
    return await run_in_threadpool(nse.load_holidays, refresh)


@router.get("/source")
def source() -> dict:
    """Where NSE prices come from right now: Kite (logged in today) or the Yahoo / NSE fallback, and the tick socket."""
    from app.market import kite, ticks
    on = kite.current() is not None
    return {"kite": on, "source": "kite" if on else "yahoo", "label": "Kite" if on else "Yahoo, delayed",
            "ticks": ticks.status()}


@router.get("/meta")
def meta() -> dict:
    return {"timeframes": {k: {"default_range": v[2], "max_range": v[3]} for k, v in TIMEFRAMES.items()},
            "ranges": list(RANGES), "indicators": {k: v for k, v in DEFAULTS.items()}}
