"""Backtest statistics (guide section 5.1). Everything is after costs."""

from __future__ import annotations

import datetime as dt
import random
from collections import defaultdict

import numpy as np

from app.backtest.engine import Trade
from app.core.calendar import IST

# Gates from the guide: what "good enough to paper trade" means, fixed before looking at results
GATES = {"min_trades": 100, "min_expectancy_r": 0.10, "min_profit_factor": 1.3}


def _max_drawdown(pnl: np.ndarray) -> float:
    if not len(pnl):
        return 0.0
    curve = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return float(np.max(peak - curve))


def summary(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    r = np.array([t.r for t in trades])
    net = np.array([t.net for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    daily = defaultdict(float)
    for t in trades:
        daily[t.session] += t.net
    d = np.array(list(daily.values()))
    sharpe = float(d.mean() / d.std() * np.sqrt(252)) if len(d) > 1 and d.std() > 0 else None
    streak = worst = 0
    for x in net:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    return {
        "trades": n,
        "win_rate": round(float((net > 0).mean()), 4),
        "expectancy_r": round(float(r.mean()), 4),
        "avg_win_r": round(float(r[net > 0].mean()), 3) if len(wins) else None,
        "avg_loss_r": round(float(r[net <= 0].mean()), 3) if len(losses) else None,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
        "net_pnl": round(float(net.sum()), 2),
        "gross_pnl": round(float(sum(t.gross for t in trades)), 2),
        "costs": round(float(sum(t.costs for t in trades)), 2),
        "max_drawdown": round(_max_drawdown(net), 2),
        "sharpe_daily": round(sharpe, 2) if sharpe is not None else None,
        "worst_losing_streak": worst,
        "sessions": len(daily),
        "exit_reasons": {k: int(sum(1 for t in trades if t.exit_reason == k)) for k in ("target", "stop", "time", "session-end", "time-stop")},
    }


def by(trades: list[Trade], key) -> dict[str, dict]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        k = key(t)
        if k is not None:
            groups[str(k)].append(t)
    return {k: _short(v) for k, v in sorted(groups.items())}


def _short(trades: list[Trade]) -> dict:
    s = summary(trades)
    return {k: s.get(k) for k in ("trades", "win_rate", "expectancy_r", "profit_factor", "net_pnl")}


def time_bucket(t: Trade) -> str:
    h = dt.datetime.fromtimestamp(t.entry_t, IST)
    m = h.hour * 60 + h.minute
    if m < 10 * 60 + 30:
        return "1. 9:15–10:30"
    if m < 12 * 60:
        return "2. 10:30–12:00"
    if m < 14 * 60:
        return "3. 12:00–14:00"
    return "4. after 14:00"


def monte_carlo(trades: list[Trade], runs: int = 1000, seed: int = 7) -> dict:
    """Reshuffle the order of the trades: how bad could the drawdown have been by luck alone?"""
    if len(trades) < 10:
        return {}
    rng = random.Random(seed)
    net = [t.net for t in trades]
    dds = []
    for _ in range(runs):
        rng.shuffle(net)
        dds.append(_max_drawdown(np.array(net)))
    dds.sort()
    return {"runs": runs, "seed": seed, "max_drawdown_median": round(dds[runs // 2], 2),
            "max_drawdown_95": round(dds[int(runs * 0.95)], 2)}


def split_in_out(trades: list[Trade], in_sample_fraction: float = 0.7) -> tuple[list[Trade], list[Trade], str | None]:
    sessions = sorted({t.session for t in trades})
    if len(sessions) < 5:
        return trades, [], None
    cut = sessions[int(len(sessions) * in_sample_fraction)]
    return [t for t in trades if t.session < cut], [t for t in trades if t.session >= cut], cut


def verdict(s: dict) -> dict:
    """Against the guide's gates, honestly."""
    checks = {
        "enough_trades": s.get("trades", 0) >= GATES["min_trades"],
        "positive_expectancy": (s.get("expectancy_r") or -1) >= GATES["min_expectancy_r"],
        "profit_factor": (s.get("profit_factor") or 0) >= GATES["min_profit_factor"],
    }
    if not checks["enough_trades"]:
        text = f"Too few trades ({s.get('trades', 0)}) to judge: need {GATES['min_trades']}."
    elif all(checks.values()):
        text = "Passes the backtest gates. Next: out-of-sample must agree, then paper trading."
    elif (s.get("expectancy_r") or 0) <= 0:
        text = "Loses money after costs in this test. Do not trade it as is."
    else:
        text = "Positive but below the gates: not good enough to paper trade yet."
    return {"checks": checks, "text": text, "gates": GATES}


def equity_curve(trades: list[Trade]) -> list[dict]:
    total = 0.0
    out = []
    for t in sorted(trades, key=lambda x: x.exit_t):
        total += t.net
        out.append({"t": t.exit_t, "equity": round(total, 2)})
    return out


def full_report(trades: list[Trade]) -> dict:
    ins, oos, cut = split_in_out(trades)
    s = summary(trades)
    return {
        "summary": s,
        "verdict": verdict(s),
        "in_sample": summary(ins),
        "out_of_sample": summary(oos),
        "split_date": cut,
        "monte_carlo": monte_carlo(trades),
        "by_side": by(trades, lambda t: t.side),
        "by_time": by(trades, time_bucket),
        "by_month": by(trades, lambda t: t.session[:7]),
        "by_vix": by(trades, lambda t: t.regime.get("vix")),
        "by_nifty_gap": by(trades, lambda t: t.regime.get("nifty_gap")),
        "by_symbol": by(trades, lambda t: t.symbol),
        "equity_curve": equity_curve(trades),
    }
