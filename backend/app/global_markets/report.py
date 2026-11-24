"""Backtest statistics, all after costs. Held-back results stay hidden unless asked for: settings are chosen on the first 70 %
of sessions and must never be chosen while looking at the rest. The gates are the same numbers NSE strategies must pass."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import replace

import numpy as np

from app.global_markets.backtest import BacktestConfig, Execution, Trade, run
from app.global_markets.strategies.base import Frame, Strategy

GATES = {"min_trades": 100, "min_expectancy_r": 0.10, "min_profit_factor": 1.3}
IN_SAMPLE_FRACTION = 0.7
EXIT_REASONS = ("target", "stop", "time", "session-end", "time-stop")


def _max_drawdown(pnl: np.ndarray) -> float:
    if not len(pnl):
        return 0.0
    curve = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return float(np.max(peak - curve))


def summary(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([t.r for t in trades])
    net = np.array([t.net for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    streak = worst = 0
    for x in net:
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    return {"trades": len(trades), "win_rate": round(float((net > 0).mean()), 4), "expectancy_r": round(float(r.mean()), 4),
            "avg_win_r": round(float(r[net > 0].mean()), 3) if len(wins) else None,
            "avg_loss_r": round(float(r[net <= 0].mean()), 3) if len(losses) else None,
            "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
            "net_pnl": round(float(net.sum()), 2), "gross_pnl": round(sum(t.gross for t in trades), 2), "costs": round(sum(t.costs for t in trades), 2),
            "max_drawdown": round(_max_drawdown(net), 2), "worst_losing_streak": worst, "sessions": len({t.session for t in trades}),
            "exit_reasons": {k: sum(t.exit_reason == k for t in trades) for k in EXIT_REASONS}}


def by(trades: list[Trade], key) -> dict[str, dict]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[str(key(t))].append(t)
    keep = ("trades", "win_rate", "expectancy_r", "profit_factor", "net_pnl")
    return {k: {x: summary(v).get(x) for x in keep} for k, v in sorted(groups.items())}


def cutoff(days: np.ndarray, fraction: float = IN_SAMPLE_FRACTION) -> str | None:
    """The first held-back trading day: the first `fraction` of the SESSIONS IN THE DATA come before it, however many trades
    fell in each. None if there is too little data to split."""
    sessions = sorted({str(d) for d in days})
    return sessions[int(len(sessions) * fraction)] if len(sessions) >= 5 else None


def verdict(s: dict) -> dict:
    checks = {"enough_trades": s.get("trades", 0) >= GATES["min_trades"],
              "positive_expectancy": (s.get("expectancy_r") or -1) >= GATES["min_expectancy_r"],
              "profit_factor": (s.get("profit_factor") or 0) >= GATES["min_profit_factor"]}
    if not checks["enough_trades"]:
        text = f"Too few trades ({s.get('trades', 0)}) to judge: need {GATES['min_trades']}."
    elif all(checks.values()):
        text = "Passes the backtest gates on these sessions. The held-back sessions must still agree."
    elif (s.get("expectancy_r") or 0) <= 0:
        text = "Loses money after costs in this test. Do not trade it as is."
    else:
        text = "Positive but below the gates: not good enough to paper trade yet."
    return {"checks": checks, "text": text, "gates": GATES}


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
    return {"runs": runs, "max_drawdown_median": round(dds[runs // 2], 2), "max_drawdown_95": round(dds[int(runs * 0.95)], 2)}


def report(strategy: Strategy, f: Frame, ex: Execution, cfg: BacktestConfig = BacktestConfig(), reveal_held_back: bool = False,
           held_back_from: str | None = None) -> dict:
    """The full report. `held_back_from` is the research line (`research_window.py`); without it, 70 % of the sessions given. Costs are also run at double the slippage, since slippage is an assumption: a result that needs cheap
    fills to survive is not one to trust."""
    cut = held_back_from or cutoff(ex.day)
    all_trades = run(strategy, f, ex, cfg)
    stressed = run(strategy, f, ex, replace(cfg, costs=replace(cfg.costs, slippage=cfg.costs.slippage * 2)))
    before = lambda ts: [t for t in ts if cut is None or t.session < cut]  # noqa: E731
    chosen = before(all_trades)
    s = summary(chosen)
    out = {"strategy": strategy.id, "version": strategy.version, "params": strategy.params, "symbol": f.symbol, "tf": f.tf, "data_kind": ex.data_kind,
           "sessions_in_data": len(set(ex.day.tolist())), "held_back_from": cut, "summary": s, "verdict": verdict(s),
           "double_slippage": summary(before(stressed)), "monte_carlo": monte_carlo(chosen), "by_side": by(chosen, lambda t: t.side),
           "by_market_session": by(chosen, lambda t: t.market_session), "by_month": by(chosen, lambda t: t.session[:7]),
           "equity_curve": _curve(chosen), "held_back": "hidden: settings are still being chosen"}
    if reveal_held_back:
        held = [t for t in all_trades if cut is not None and t.session >= cut]
        out["held_back"] = {"summary": summary(held), "double_slippage": summary([t for t in stressed if cut is not None and t.session >= cut])}
    return out


def _curve(trades: list[Trade]) -> list[dict]:
    total, out = 0.0, []
    for t in sorted(trades, key=lambda x: x.exit_t):
        total += t.net
        out.append({"t": t.exit_t, "equity": round(total, 2)})
    return out
