"""Runs a strategy over today's bars for a list of symbols and returns explained signals.

`at` replays today up to a time (e.g. "10:15"), using only bars that had closed by
then, so you can see what the app would have said at that moment."""

from __future__ import annotations

import dataclasses
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from app.core.calendar import CLOSE, IST, OPEN, is_trading_day
from app.market import feed, news, yahoo
from app.market.models import Candles
from app.signals.explain import build, regime_now
from app.core.instruments import resolve
from app.strategies.base import Frame
from app.strategies.library import make


def last_session_day(now: dt.datetime) -> dt.date:
    """Today if the market has opened today, else the most recent trading day."""
    d = now.date()
    if is_trading_day(d)[0] and now.time() >= OPEN:
        return d
    d -= dt.timedelta(days=1)
    while not is_trading_day(d)[0]:
        d -= dt.timedelta(days=1)
    return d


def _regime(session: str) -> dict:
    vix_prev = gap = None
    try:
        vix = feed.candles("INDIAVIX", "1d", "1mo").bars
        prior = [b for b in vix if dt.datetime.fromtimestamp(b.t, IST).date().isoformat() < session]
        vix_prev = prior[-1].c if prior else None
        nifty = feed.candles("NIFTY", "1d", "1mo").bars
        days = {dt.datetime.fromtimestamp(b.t, IST).date().isoformat(): b for b in nifty}
        before = [d for d in sorted(days) if d < session]
        if session in days and before:
            gap = (days[session].o / days[before[-1]].c - 1) * 100
    except yahoo.MarketDataError:
        pass
    return regime_now(session, vix_prev, gap)


def scan(strategy_id: str, params: dict, symbols: list[str], at: str | None = None) -> dict:
    strategy = make(strategy_id, params)
    now = dt.datetime.now(IST)
    day = last_session_day(now)
    cutoff = now if day == now.date() else dt.datetime.combine(day, CLOSE, IST)
    if at:
        hh, mm = (int(x) for x in at.split(":"))
        cutoff = dt.datetime.combine(day, dt.time(hh, mm), IST)   # replay the latest session up to `at`

    sources: list[str] = []

    def one(sym: str):
        inst = resolve(sym, day)
        c = feed.candles(inst.data_symbol, strategy.timeframe, "5d" if strategy.timeframe in ("1m", "5m") else "1mo")
        sources.append(c.source)
        c = dataclasses.replace(c, symbol=inst.symbol)
        secs = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}[strategy.timeframe]
        bars = [b for b in c.bars if b.t + secs <= cutoff.timestamp()]
        if not bars:
            return []
        f = Frame(dataclasses.replace(c, bars=bars))
        session = day.isoformat()              # signals from the latest session only
        out = []
        for i in range(len(f)):
            if f.session[i] != session:
                continue
            cand = strategy.check(f, i)
            if cand is not None:
                out.append((f, cand))
        return out

    found, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for sym, res in zip(symbols, pool.map(lambda s: _safe(one, s), symbols)):
            if isinstance(res, Exception):
                errors.append(f"{sym}: {res}")
            else:
                found.extend(res)
    session = day.isoformat()
    regime = _regime(session)
    with ThreadPoolExecutor(max_workers=6) as pool:        # warm the news cache for every stock with a signal
        list(pool.map(lambda s: _safe(lambda x: news.for_symbol(x, days=2, headlines=False), s),
                      sorted({f.symbol for f, _ in found if not f.symbol.endswith("-FUT")})))
    signals = []
    for f, cand in sorted(found, key=lambda x: -int(x[0].t[x[1].signal_index])):
        sig = build(strategy, f, cand, regime)
        age_bars = (len(f) - 1) - cand.signal_index
        signals.append({**sig.model_dump(mode="json"), "fresh": age_bars == 0, "bars_ago": age_bars})
    return {"strategy": {"id": strategy.id, "name": strategy.name, "version_key": strategy.version_key},
            "as_of": cutoff.isoformat(timespec="minutes"), "session": session, "replay": bool(at),
            "market_open_now": day == now.date() and now.time() < CLOSE, "regime": regime,
            "scanned": len(symbols), "signals": signals, "errors": errors,
            "data_source": feed.worst(sources), "data_source_label": feed.label(feed.worst(sources) or "yahoo")}


def _safe(fn, arg):
    try:
        return fn(arg)
    except Exception as e:   # noqa: BLE001 - reported per symbol
        return e


__all__ = ["scan", "Candles"]
