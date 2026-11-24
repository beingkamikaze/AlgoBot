"""Backtest jobs: fetch data for a universe, run a strategy, save the report.

Results live in backend/data/backtests/<id>.json until paper trading moves storage
to Supabase in Phase 3. Each trade carries regime tags that were KNOWN AT THE OPEN
(yesterday's India VIX, Nifty's opening gap), so the same tags can describe a live
signal without looking ahead.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from app.backtest.engine import BacktestConfig, Trade, run_symbol
from app.backtest.metrics import full_report
from app.core.calendar import IST
from app.core.instruments import INDEX_FUTURE_SYMBOLS, resolve
from app.market import nse, store, yahoo
from app.strategies.base import Candidate, Frame
from app.options.backtest import OPTION_STRATEGIES, Cfg as OptCfg
from app.strategies.library import make

DIR = Path(__file__).resolve().parents[2] / "data" / "backtests"
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- regimes known at the open

def vix_bucket(v: float | None) -> str | None:
    if v is None:
        return None
    return "low VIX (<13)" if v < 13 else ("normal VIX (13–18)" if v <= 18 else "high VIX (>18)")


def gap_bucket(pct: float | None) -> str | None:
    if pct is None:
        return None
    return "Nifty gap up (>0.3%)" if pct > 0.3 else ("Nifty gap down (<-0.3%)" if pct < -0.3 else "Nifty flat open")


class RegimeBook:
    """Per session: yesterday's VIX close and Nifty's open vs yesterday's close."""

    def __init__(self) -> None:
        self.vix_prev: dict[str, float] = {}
        self.nifty_gap: dict[str, float] = {}
        try:
            vix = yahoo.candles("INDIAVIX", "1d", "10y").bars
            for a, b in zip(vix, vix[1:]):
                self.vix_prev[dt.datetime.fromtimestamp(b.t, IST).date().isoformat()] = a.c
            nifty = yahoo.candles("NIFTY", "1d", "10y").bars
            for a, b in zip(nifty, nifty[1:]):
                self.nifty_gap[dt.datetime.fromtimestamp(b.t, IST).date().isoformat()] = (b.o / a.c - 1) * 100
        except yahoo.MarketDataError:
            pass                                  # trades simply carry no regime tags

    def tags(self, session: str, _cand: Candidate | None = None) -> dict:
        v, g = self.vix_prev.get(session), self.nifty_gap.get(session)
        return {"vix": vix_bucket(v), "vix_prev_close": round(v, 2) if v else None,
                "nifty_gap": gap_bucket(g), "nifty_gap_pct": round(g, 2) if g is not None else None}


# ---------------------------------------------------------------- universe

def resolve_universe(universe: str, symbols: list[str] | None) -> list[str]:
    if universe == "INDEXFUT":
        return list(INDEX_FUTURE_SYMBOLS)
    if universe == "INDEXOPT":
        return ["NIFTY", "BANKNIFTY"]
    if universe == "custom":
        out = [s.strip().upper() for s in (symbols or []) if s.strip()]
        if not out:
            raise ValueError("a custom universe needs symbols")
        return out[:200]
    return nse.constituents(universe)


# ---------------------------------------------------------------- jobs

def _save(job: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{job['id']}.json").write_text(json.dumps(job, separators=(",", ":")))


def start(strategy_id: str, params: dict, universe: str, symbols: list[str] | None,
          config: dict | None = None) -> dict:
    if strategy_id in OPTION_STRATEGIES:
        strategy = OPTION_STRATEGIES[strategy_id](**params)
        universe = "INDEXOPT"                     # option strategies run on the indices' own chains
    else:
        strategy = make(strategy_id, params)       # validates parameters now, not in the thread
    syms = resolve_universe(universe, symbols)
    cfg = BacktestConfig(**(config or {}))
    if any(s.endswith("-FUT") for s in syms) and cfg.fixed_lots is None:
        cfg.fixed_lots = 1          # one lot: futures risk per trade is set by the lot size, not by a rupee budget
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "created_at": int(time.time()),
           "strategy": {"id": strategy.id, "version": strategy.version, "version_key": strategy.version_key,
                        "name": strategy.name, "timeframe": strategy.timeframe, "params": strategy.params,
                        "modeled": strategy_id in OPTION_STRATEGIES},
           "universe": universe, "symbols": syms, "config": asdict(cfg), "progress": {"done": 0, "total": len(syms)},
           "errors": [], "report": None, "trades": []}
    with _lock:
        _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, strategy, cfg), daemon=True).start()
    return _public(job)


def _run(job: dict, strategy, cfg: BacktestConfig) -> None:
    try:
        regimes = RegimeBook()
        trades: list[Trade] = []

        def one(sym: str) -> list[Trade]:
            if job["strategy"].get("modeled"):
                c = store.history(sym, strategy.timeframe)
                return strategy.run(Frame(c), sym, regimes.vix_prev, OptCfg(), regimes.tags)
            inst = resolve(sym)
            c = store.history(inst.data_symbol, strategy.timeframe)
            if len(c.bars) < 50:
                raise ValueError("not enough bars")
            c = dataclasses.replace(c, symbol=inst.symbol)   # futures: index prices, traded as the contract
            return run_symbol(strategy, Frame(c), cfg, regimes.tags, inst)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(one, s): s for s in job["symbols"]}
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    trades.extend(fut.result())
                except Exception as e:  # one bad symbol never sinks the run
                    job["errors"].append(f"{sym}: {e}")
                job["progress"]["done"] += 1
        trades.sort(key=lambda t: t.entry_t)
        sessions = sorted({t.session for t in trades})
        job["report"] = full_report(trades)
        job["period"] = {"from": sessions[0] if sessions else None, "to": sessions[-1] if sessions else None}
        job["trades"] = [t.to_dict() for t in trades]
        job["status"] = "done"
    except Exception as e:
        job["status"] = "failed"
        job["errors"].append(str(e))
    finally:
        job["finished_at"] = int(time.time())
        _save(job)


def _public(job: dict, with_trades: bool = False) -> dict:
    out = {k: v for k, v in job.items() if k != "trades"}
    if with_trades:
        out["trades"] = job.get("trades", [])
    return out


def get(job_id: str, with_trades: bool = True) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        p = DIR / f"{job_id}.json"
        if not p.exists() or not job_id.isalnum():
            return None
        job = json.loads(p.read_text())
    return _public(job, with_trades)


def listing(limit: int = 50) -> list[dict]:
    rows = []
    with _lock:
        live = {k: v for k, v in _jobs.items() if v["status"] == "running"}
    for j in live.values():
        rows.append(_brief(j))
    if DIR.exists():
        for p in sorted(DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
            j = json.loads(p.read_text())
            if j["id"] not in live:
                rows.append(_brief(j))
    return rows[:limit]


def _brief(j: dict) -> dict:
    s = (j.get("report") or {}).get("summary") or {}
    return {"id": j["id"], "status": j["status"], "created_at": j["created_at"], "strategy": j["strategy"],
            "universe": j["universe"], "symbols": len(j["symbols"]), "progress": j["progress"],
            "period": j.get("period"), "trades": s.get("trades"), "expectancy_r": s.get("expectancy_r"),
            "profit_factor": s.get("profit_factor"), "net_pnl": s.get("net_pnl"),
            "verdict": ((j.get("report") or {}).get("verdict") or {}).get("text")}


_headers: dict[str, tuple[float, tuple]] = {}         # file -> (mtime, (status, strategy, version_key, futures))
_evidence_cache: dict[str, tuple[float, dict]] = {}   # file -> (mtime, the backtest with slim trades)
_CARD_FIELDS = ("side", "entry_t", "exit_t", "net", "r", "exit_reason")


def _header(p: Path, mtime: float, j: dict | None = None) -> tuple:
    hit = _headers.get(p.name)
    if hit and hit[0] == mtime:
        return hit[1]
    j = j if j is not None else json.loads(p.read_text())
    h = (j["status"], j["strategy"]["id"], j["strategy"]["version_key"],
         any(str(s).endswith("-FUT") for s in j.get("symbols", [])))
    _headers[p.name] = (mtime, h)
    return h


def latest_for(strategy_id: str, version_key: str, futures: bool = False) -> dict | None:
    """The newest finished backtest of this exact strategy version, on the same kind of instrument
    (stocks or index futures): the evidence on a signal card. Files are read once per change (multi-year
    backtests are tens of MB) and the result keeps only the trade fields the cards use."""
    if not DIR.exists():
        return None
    files = sorted(((p, p.stat().st_mtime) for p in DIR.glob("*.json")), key=lambda x: x[1], reverse=True)
    for p, mtime in files:
        if _header(p, mtime) != ("done", strategy_id, version_key, futures):
            continue
        hit = _evidence_cache.get(p.name)
        if hit and hit[0] == mtime:
            return hit[1]
        j = json.loads(p.read_text())
        _header(p, mtime, j)
        j["trades"] = [{**{k: t.get(k) for k in _CARD_FIELDS}, "regime": {"vix": (t.get("regime") or {}).get("vix")}}
                       for t in j.get("trades", [])]
        want = ("done", strategy_id, version_key, futures)
        for name in [n for n in _evidence_cache if _headers.get(n, (0, None))[1] == want]:
            del _evidence_cache[name]                   # an older backtest of the same version
        _evidence_cache[p.name] = (mtime, j)
        return j
    return None
