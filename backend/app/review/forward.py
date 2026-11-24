"""Forward results: each strategy's closed paper trades next to what its backtest expected.

Forward trades are the honest test: a backtest that looks good but fails here should not go live. This module only
describes. It grades nothing, suggests no settings, and never changes the confidence rule.

Groups are kept apart:
  - clean trades linked to a strategy, one group per (strategy, version, instrument): the default parameters and each
    approved Lab version are separate groups;
  - overridden trades (the paper risk override, "rules not followed"): never mixed into a strategy's numbers;
  - trades with no linked strategy.

The expectation for a group comes from the newest finished backtest of that exact strategy version on the same kind of
instrument (`backtest.runner.latest_for`, the same evidence the signal cards show). The verdict says nothing until a
group has MIN_TRADES trades; after that it says whether the forward average R lies inside a rough 95% range of what the
backtest's own trades would give over the same number of trades (a bootstrap of the backtest's R values).
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from app.backtest import runner
from app.core.calendar import IST
from app.db import DBError, db, eq
from app.lab.registry import all_strategy_classes
from app.options.backtest import OPTION_STRATEGIES

MIN_TRADES = 30
BOOTSTRAP_REPS = 2000
DAYS_PER_MONTH = 30.44
INSTRUMENT_LABEL = {"equity": "stocks", "future": "index futures", "option": "options"}


# ---------------------------------------------------------------- numbers for one group of trades

def _epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def stats(rows: list[dict]) -> dict:
    """rows: {"net": ₹ after charges, "r": R after charges or None, "exit_ts": iso}, in any order."""
    rows = sorted(rows, key=lambda x: x["exit_ts"])
    rs = [float(x["r"]) for x in rows if x.get("r") is not None]
    wins = sum(1 for x in rows if float(x["net"] or 0) > 0)
    streak = worst = 0
    for x in rows:
        streak = streak + 1 if float(x["net"] or 0) < 0 else 0
        worst = max(worst, streak)
    cum = peak = dd = 0.0
    curve = []
    for x in rows:
        if x.get("r") is None:
            continue
        cum += float(x["r"])
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append({"t": _epoch(x["exit_ts"]), "equity": round(cum, 3)})
    return {"trades": len(rows), "trades_with_r": len(rs), "wins": wins,
            "win_rate": round(wins / len(rows), 4) if rows else None,
            "average_r": round(sum(rs) / len(rs), 3) if rs else None,
            "total_r": round(sum(rs), 3) if rs else None,
            "net_pnl": round(sum(float(x["net"] or 0) for x in rows), 2),
            "worst_losing_streak": worst, "max_drawdown_r": round(dd, 3), "curve": curve}


def bootstrap_range(backtest_r: list[float], n: int, seed: int = 0) -> tuple[float, float]:
    """The middle 95% of average R over n trades drawn from the backtest's own trades."""
    a = np.asarray(backtest_r, dtype=float)
    if n * BOOTSTRAP_REPS > 4_000_000:            # large samples: the normal approximation is as good and far cheaper
        half = 1.96 * float(a.std(ddof=1)) / math.sqrt(n)
        return float(a.mean()) - half, float(a.mean()) + half
    means = np.random.default_rng(seed).choice(a, size=(BOOTSTRAP_REPS, n), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def verdict(forward: dict, expected: dict | None) -> dict:
    """Plain words. Too few trades: nothing else is said."""
    n = forward["trades_with_r"]
    if n < MIN_TRADES:
        return {"kind": "too_few", "text": f"Too few trades to judge ({n} of {MIN_TRADES})."}
    if not expected or expected.get("range") is None:
        return {"kind": "no_backtest", "text": "No backtest of this exact version to compare with."}
    lo, hi = expected["range"]
    avg = forward["average_r"]
    span = f"{lo:+.2f}R to {hi:+.2f}R"
    if avg < lo:
        return {"kind": "below", "text": f"Worse than the backtest expected: {avg:+.2f}R a trade over {n} trades, below the "
                                         f"rough 95% range of {span} that the backtest's own trades give for {n} trades."}
    if avg > hi:
        return {"kind": "above", "text": f"Better than the backtest expected: {avg:+.2f}R a trade over {n} trades, above the "
                                         f"rough 95% range of {span}. A run of luck is the first thing to rule out."}
    return {"kind": "inside", "text": f"In line with the backtest: {avg:+.2f}R a trade over {n} trades is inside the rough "
                                      f"95% range of {span} for {n} trades."}


def _gap(forward: dict, expected: dict | None) -> dict | None:
    if not expected or forward["average_r"] is None or forward["trades_with_r"] < MIN_TRADES:
        return None                                  # too few trades: no gap either, it would read as a judgment
    return {"average_r": round(forward["average_r"] - expected["average_r"], 3) if expected.get("average_r") is not None else None,
            "win_rate": round(forward["win_rate"] - expected["win_rate"], 4)
            if forward["win_rate"] is not None and expected.get("win_rate") is not None else None}


def _months(start: dt.date, end: dt.date) -> float:
    return max((end - start).days + 1, 1) / DAYS_PER_MONTH


# ---------------------------------------------------------------- NSE

def _nse_expected(strategy_id: str, version: str, kind: str, n: int) -> tuple[dict | None, list[str]]:
    notes = []
    option_strategy = strategy_id in OPTION_STRATEGIES
    futures = kind in ("future", "option") and not option_strategy
    if kind == "option" and not option_strategy:
        notes.append("These trades were options; the backtest traded the index futures, so R per trade is not like for like.")
    bt = runner.latest_for(strategy_id, version, futures=futures)
    if bt is None:
        notes.append("No finished backtest of this exact version: run one on the Backtests page.")
        return None, notes
    s = (bt.get("report") or {}).get("summary") or {}
    rs = [float(t["r"]) for t in bt.get("trades", []) if t.get("r") is not None]
    if not s.get("trades") or not rs:
        notes.append("The newest backtest of this version has no trades.")
        return None, notes
    period = bt.get("period") or {}
    per_month = None
    if period.get("from") and period.get("to"):
        per_month = round(s["trades"] / _months(dt.date.fromisoformat(period["from"]), dt.date.fromisoformat(period["to"])), 1)
    exp = {"backtest_id": bt["id"], "universe": bt.get("universe"), "symbols": len(bt.get("symbols") or []),
           "period": period, "trades": s["trades"], "average_r": s.get("expectancy_r"), "win_rate": s.get("win_rate"),
           "trades_per_month": per_month, "held_back_average_r": ((bt["report"].get("out_of_sample") or {}).get("expectancy_r")),
           "range": None, "range_method": "bootstrap of the backtest's trades"}
    if n >= MIN_TRADES:
        exp["range"] = [round(x, 3) for x in bootstrap_range(rs, n)]
    if per_month is not None and (bt.get("symbols") and len(bt["symbols"]) > 2):
        notes.append(f"Trades per month in the backtest count every signal across its {len(bt['symbols'])} symbols, "
                     "not just the ones you would take.")
    return exp, notes


def _nse_rows(user_id: str, since: dt.date | None) -> tuple[list[dict], dt.date | None]:
    d = db()
    every = d.select("trades", {"user_id": eq(user_id), "mode": eq("paper")}, columns="entry_ts", order="entry_ts", limit=1)
    first = dt.datetime.fromisoformat(every[0]["entry_ts"].replace("Z", "+00:00")).astimezone(IST).date() if every else None
    since = since or first
    if since is None:
        return [], None
    start = dt.datetime.combine(since, dt.time(0), IST).astimezone(dt.timezone.utc).isoformat()
    rows = d.select("trades", {"user_id": eq(user_id), "mode": eq("paper"), "exit_ts": "not.is.null"},
                    columns="id,symbol,side,entry_ts,exit_ts,net_pnl,r_multiple,rules_followed,entry_context", order="exit_ts")
    return [t for t in rows if _epoch(t["exit_ts"]) >= _epoch(start)], since


def _is_override(t: dict) -> bool:
    return t.get("rules_followed") is False or bool((t.get("entry_context") or {}).get("override"))


def _row(t: dict) -> dict:
    return {"net": t.get("net_pnl"), "r": t.get("r_multiple"), "exit_ts": t["exit_ts"]}


def nse(user_id: str, since: dt.date | None = None, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(IST).date()
    trades, since = _nse_rows(user_id, since)
    classes = all_strategy_classes()
    overridden, unlinked, groups = [], [], {}
    for t in trades:
        ctx = t.get("entry_context") or {}
        if _is_override(t):
            overridden.append(t)
        elif not ctx.get("strategy"):
            unlinked.append(t)
        else:
            key = (ctx["strategy"], str(ctx.get("strategy_version") or "1"), ctx.get("kind") or "equity")
            groups.setdefault(key, []).append(t)
    months = _months(since, today) if since else None
    out = []
    for (sid, version, kind), rows in sorted(groups.items()):
        cls = classes.get(sid)
        fwd = stats([_row(t) for t in rows])
        fwd["trades_per_month"] = round(fwd["trades"] / months, 1) if months else None
        expected, notes = _nse_expected(sid, version, kind, fwd["trades_with_r"])
        if fwd["trades_with_r"] < fwd["trades"]:
            notes.append(f"{fwd['trades'] - fwd['trades_with_r']} trade(s) have no recorded risk, so no R: counted in ₹ only.")
        out.append({"key": f"{sid}|{version}|{kind}", "strategy": sid, "name": cls.name if cls else sid, "version": version,
                    "version_kind": "default" if cls is None or version == cls.version else "lab",
                    "instrument": INSTRUMENT_LABEL.get(kind, kind), "forward": fwd, "expected": expected,
                    "gap": _gap(fwd, expected), "verdict": verdict(fwd, expected), "notes": notes})
    by_strategy: dict[str, list[dict]] = {}
    for t in overridden:
        by_strategy.setdefault((t.get("entry_context") or {}).get("strategy") or "no strategy", []).append(_row(t))
    over = stats([_row(t) for t in overridden])
    over["by_strategy"] = [{"strategy": k, "trades": len(v), "average_r": stats(v)["average_r"], "net_pnl": stats(v)["net_pnl"]}
                           for k, v in sorted(by_strategy.items())]
    return {"since": since.isoformat() if since else None, "strategies": out, "overridden": over,
            "unlinked": stats([_row(t) for t in unlinked])}


# ---------------------------------------------------------------- XAUUSD (the global_markets setup, read only)

def _global_expected(report: dict, n: int) -> dict | None:
    s = report.get("summary") or {}
    if not s.get("trades") or s.get("expectancy_r") is None:
        return None
    m, p = float(s["expectancy_r"]), float(s.get("win_rate") or 0)
    aw, al = float(s.get("avg_win_r") or 0), float(s.get("avg_loss_r") or 0)
    sd = math.sqrt(max(p * aw * aw + (1 - p) * al * al - m * m, 0.0))   # wins and losses at their averages: a rough spread
    per_month = None
    if report.get("first_session") and report.get("last_session"):
        per_month = round(s["trades"] / _months(dt.date.fromisoformat(report["first_session"]),
                                                dt.date.fromisoformat(report["last_session"])), 1)
    rng = None
    if n >= MIN_TRADES and sd > 0:
        half = 1.96 * sd / math.sqrt(n)
        rng = [round(m - half, 3), round(m + half, 3)]
    return {"trades": s["trades"], "average_r": m, "win_rate": s.get("win_rate"), "trades_per_month": per_month,
            "period": {"from": report.get("first_session"), "to": report.get("last_session")}, "range": rng,
            "range_method": "standard error from the backtest's win rate and average win and loss",
            "held_back_average_r": None}


def xauusd(user_id: str, since: dt.date | None = None, today: dt.date | None = None) -> dict:
    from app.global_markets import research
    from app.global_markets.strategies.library import STRATEGIES as GLOBAL
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        rows = db().select("global_trades", {"user_id": eq(user_id), "mode": eq("paper"), "exit_ts": "not.is.null"},
                           columns="id,symbol,entry_ts,exit_ts,net_pnl,r_multiple,rules_followed,entry_context", order="exit_ts")
    except DBError as e:
        return {"available": False, "note": f"XAUUSD paper trades could not be read: {e}"}
    first = min((r["entry_ts"] for r in rows), default=None)
    since = since or (dt.datetime.fromisoformat(first.replace("Z", "+00:00")).date() if first else None)
    if since:
        cut = dt.datetime.combine(since, dt.time(0), dt.timezone.utc).timestamp()
        rows = [r for r in rows if _epoch(r["exit_ts"]) >= cut]
    by_name = {c.name: sid for sid, c in GLOBAL.items()}
    try:
        reports = research.latest()
    except Exception:  # noqa: BLE001 - no stored reports: forward numbers still show
        reports = {}
    overridden, unlinked, groups = [], [], {}
    for t in rows:
        source = ((t.get("entry_context") or {}).get("card") or {}).get("source")
        sid = by_name.get(source) or (source if source in GLOBAL else None)
        if _is_override(t):
            overridden.append(t)
        elif sid is None:
            unlinked.append(t)
        else:
            groups.setdefault(sid, []).append(t)
    months = _months(since, today) if since else None
    out = []
    for sid, ts in sorted(groups.items()):
        fwd = stats([_row(t) for t in ts])
        fwd["trades_per_month"] = round(fwd["trades"] / months, 1) if months else None
        expected = _global_expected(reports[sid], fwd["trades_with_r"]) if sid in reports else None
        notes = ["The backtest ran on research days only (before the held-back line), at fixed settings."]
        if expected is None:
            notes.append("No stored backtest report for this strategy: run them on the XAUUSD backtests page.")
        out.append({"key": f"xau|{sid}", "strategy": sid, "name": GLOBAL[sid].name, "version": GLOBAL[sid].version,
                    "version_kind": "default", "instrument": "XAUUSD", "forward": fwd, "expected": expected,
                    "gap": _gap(fwd, expected), "verdict": verdict(fwd, expected), "notes": notes})
    return {"available": True, "since": since.isoformat() if since else None, "currency": "USD", "strategies": out,
            "overridden": stats([_row(t) for t in overridden]), "unlinked": stats([_row(t) for t in unlinked])}


def overview(user_id: str, since: dt.date | None = None) -> dict:
    return {"min_trades": MIN_TRADES, "as_of": dt.datetime.now(IST).isoformat(timespec="seconds"),
            "nse": nse(user_id, since), "xauusd": xauusd(user_id, since)}
