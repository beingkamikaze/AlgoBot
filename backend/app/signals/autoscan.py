"""Scan every strategy, on every universe it suits, with every version you are allowed to trade, and rank
the results. Runs in the background (a full sweep takes about a minute on free data).

Parameters are NOT tuned here. A scan that picked whatever parameters fitted recent bars best would always
look good and fail on new days (overfitting). Instead each strategy runs with:
  - its default parameters, and
  - every version you approved in the Lab (passed the held-back test) that is not retired.

Signals are grouped into trade ideas: one per stock and direction, holding every strategy that flags it.
Ranking, best first:
  1. confidence of the idea's best signal (a fixed rule from the strategy's record after costs:
     high, medium, low, no track record)
  2. agreement: how many different strategies flag the same stock in the same direction this session
  3. fresh before older (an older signal: price has moved since)
  4. the best signal's average result per trade after costs (its largest evidence sample)
  5. newest first
Repeats are dropped: one signal per strategy version, symbol and side (the latest). An idea is marked as a
conflict when another strategy flags the same stock in the opposite direction.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.instruments import INDEX_FUTURE_SYMBOLS
from app.db import db, eq
from app.market import feed, nse
from app.signals.scan import scan
from app.strategies.library import describe

CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1, "no track record": 0}
STOCK_UNIVERSE = "NIFTY100"            # covers the Nifty 50 and most of Nifty Bank
JOB_TTL = 3600
REUSE_SECONDS = 90                     # a sweep finished this recently for the same request is reused
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def plan(user_id: str | None) -> list[dict]:
    """What will be scanned: (strategy, version, params, universe) for every tradable strategy version."""
    out = []
    variants: list[dict] = []
    if user_id:
        try:
            variants = [v for v in db().select("strategy_versions", {"user_id": eq(user_id)})
                        if v.get("status") not in ("retired",)]
        except Exception:  # noqa: BLE001 - before the lab migration there are no variants
            variants = []
    for s in describe():
        if s["market"] == "index_options":
            continue                    # option strategies are backtest-only (modeled prices)
        if not s.get("auto_scan", True):
            continue                    # kept for research, not for signals
        versions = [{"version": s["version"], "params": {}, "label": "default"}]
        for v in variants:
            if v["strategy_id"] == s["id"]:
                changed = {k: val for k, val in (v.get("params") or {}).items() if s["defaults"].get(k) != val}
                if changed:
                    versions.append({"version": v["version"], "params": changed, "label": "lab"})
        for ver in versions:
            for universe in (STOCK_UNIVERSE, "INDEXFUT"):
                out.append({"strategy": s["id"], "name": s["name"], "timeframe": s["timeframe"], **ver, "universe": universe})
    return out


def _best_evidence(sig: dict) -> dict | None:
    ev = [e for e in sig["explanation"].get("evidence") or [] if e.get("expectancy_r") is not None]
    return max(ev, key=lambda e: e.get("trades") or 0, default=None)


def _has_fno(symbol: str) -> bool:
    try:
        from app.signals.fno_plan import available
        return available(symbol)
    except Exception:  # noqa: BLE001 - NSE's lot file unavailable: no badge, nothing else changes
        return False


def _key(s: dict) -> tuple:
    ri = s["rank_info"]
    return (CONFIDENCE_ORDER.get(ri["confidence"], 0), bool(s.get("fresh")),
            ri["evidence_r"] if ri["evidence_r"] is not None else -9, s["created_at"])


def rank(signals: list[dict]) -> list[dict]:
    """Signals -> trade ideas (one per stock and direction), best first. Pure: no I/O.

    Repeats of the same strategy version on the same stock and side collapse to the latest. Each idea keeps every
    strategy's signal (best first) and is ranked by its best signal, with more agreeing strategies ranking higher."""
    latest: dict[tuple, dict] = {}
    for s in signals:
        k = (s["strategy_id"], s["strategy_version"], s["symbol"], s["side"])
        if k not in latest or s["created_at"] > latest[k]["created_at"]:
            latest[k] = s
    ideas: dict[tuple, list[dict]] = {}
    for s in latest.values():
        ev = _best_evidence(s)
        s = {**s, "rank_info": {"confidence": s["explanation"]["confidence"],
                                "evidence_r": ev.get("expectancy_r") if ev else None,
                                "evidence_trades": ev.get("trades") if ev else 0}}
        ideas.setdefault((s["symbol"], s["side"]), []).append(s)
    out = []
    for (symbol, side), sigs in ideas.items():
        sigs.sort(key=_key, reverse=True)
        best = sigs[0]
        out.append({"symbol": symbol, "side": side, "best": best, "signals": sigs, "fno": _has_fno(symbol),
                    "segment": "index" if symbol.endswith("-FUT") else "stock",
                    "strategies": sorted({x["strategy_id"] for x in sigs}),
                    "confidence": best["rank_info"]["confidence"], "fresh": any(x.get("fresh") for x in sigs),
                    "latest": max(x["created_at"] for x in sigs),
                    "conflict": (symbol, "sell" if side == "buy" else "buy") in ideas})
    out.sort(key=lambda i: (CONFIDENCE_ORDER.get(i["confidence"], 0), len(i["strategies"]), i["fresh"],
                            i["best"]["rank_info"]["evidence_r"] if i["best"]["rank_info"]["evidence_r"] is not None else -9,
                            i["latest"]), reverse=True)
    for n, i in enumerate(out, 1):
        i["rank"] = n
    return out


def _run(job: dict, items: list[dict], at: str | None) -> None:
    t0 = time.monotonic()
    try:
        stocks = nse.constituents(STOCK_UNIVERSE)[:100]
        symbols = {STOCK_UNIVERSE: stocks, "INDEXFUT": list(INDEX_FUTURE_SYMBOLS)}
        found: list[dict] = []
        meta: dict = {}
        sources: list[str] = []

        def one(item: dict):
            r = scan(item["strategy"], item["params"], symbols[item["universe"]], at)
            return item, r

        with ThreadPoolExecutor(max_workers=2) as pool:     # two scans at once; each fetches 4 symbols in parallel
            for item, r in pool.map(lambda it: _safe(one, it), items):
                if isinstance(r, Exception):
                    job["errors"].append(f"{item['strategy']} v{item['version']} on {item['universe']}: {r}")
                else:
                    meta = meta or r
                    if r.get("data_source"):
                        sources.append(r["data_source"])
                    for s in r["signals"]:
                        found.append({**s, "universe": item["universe"], "version_label": item["label"]})
                    job["errors"].extend(f"{item['strategy']}: {e}" for e in r["errors"][:3])
                job["progress"]["done"] += 1
        ideas = rank(found)
        job.update(status="done", ideas=ideas, signal_count=sum(len(i["signals"]) for i in ideas), raw_count=len(found), took_s=round(time.monotonic() - t0, 1),
                   as_of=meta.get("as_of"), session=meta.get("session"), regime=meta.get("regime"),
                   market_open_now=meta.get("market_open_now"), replay=bool(at),
                   scanned_symbols=len(stocks) + len(INDEX_FUTURE_SYMBOLS), data_source=feed.worst(sources))
    except Exception as e:  # noqa: BLE001 - the job reports it
        job.update(status="failed", errors=job["errors"] + [str(e)])


def _safe(fn, item):
    try:
        return fn(item)
    except Exception as e:  # noqa: BLE001
        return item, e


def start(user_id: str | None, at: str | None = None) -> dict:
    items = plan(user_id)
    key = f"{user_id}|{at}|{len(items)}"
    now = time.time()
    with _lock:
        for k in [k for k, j in _jobs.items() if now - j["started"] > JOB_TTL]:
            _jobs.pop(k, None)
        for j in _jobs.values():                           # a sweep running or just finished: reuse it
            if j["key"] == key and (j["status"] == "running" or now - j["started"] < REUSE_SECONDS):
                return view(j)
        job = {"id": secrets.token_hex(6), "key": key, "user_id": user_id, "status": "running", "started": now,
               "progress": {"done": 0, "total": len(items)}, "plan": [{k: it[k] for k in ("strategy", "name", "version", "label", "universe")} for it in items],
               "errors": [], "ideas": []}
        _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, items, at), daemon=True, name=f"autoscan-{job['id']}").start()
    return view(job)


def get(job_id: str, user_id: str | None) -> dict | None:
    j = _jobs.get(job_id)
    if not j or j["user_id"] != user_id:
        return None
    return view(j)


def view(j: dict) -> dict:
    return {k: v for k, v in j.items() if k not in ("key", "user_id")} | {
        "started_at": dt.datetime.fromtimestamp(j["started"], dt.timezone.utc).isoformat()}
