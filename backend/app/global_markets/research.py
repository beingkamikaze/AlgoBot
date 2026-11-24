"""Runs the strategies on RESEARCH DAYS ONLY and keeps every report, one file per run, in backend/data/global/reports/. Nothing here
can read a held-back day: the data is cut before it reaches the backtester. Seeing those days is the Lab's job, once (phase X7).

Run by hand: python -m app.global_markets.research
"""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import numpy as np

from app.global_markets.backtest import from_columns
from app.global_markets.clock import is_trading_day, session_bounds
from app.global_markets.data import store
from app.global_markets.report import report
from app.global_markets.research_window import HELD_BACK_FROM, RESEARCH_START
from app.global_markets.strategies.library import STRATEGIES, make


class NoResearchData(Exception):
    pass


def reports_dir(root: Path | None = None) -> Path:
    return (root or store.ROOT) / "reports"


def research_columns(symbol: str, root: Path | None = None) -> dict[str, np.ndarray] | None:
    """Stored 1-minute bars of the research days: everything before the held-back line's first trading minute."""
    columns = store.read_columns(symbol, root=root)
    if columns is None:
        return None
    day = HELD_BACK_FROM
    while session_bounds(day) is None:
        day += dt.timedelta(days=1)
    keep = columns["t"] < int(session_bounds(day)[0].timestamp())
    return {k: v[keep] for k, v in columns.items()} if keep.any() else None


def research_days_possible() -> int:
    d, n = RESEARCH_START, 0
    while d < HELD_BACK_FROM:
        n += is_trading_day(d)
        d += dt.timedelta(days=1)
    return n


def run_one(strategy_id: str, symbol: str, root: Path | None = None) -> dict:
    columns = research_columns(symbol, root)
    if columns is None:
        raise NoResearchData("No research days are stored yet: the history download reaches them first, and needs the app running.")
    s = make(strategy_id)
    f, ex = from_columns(symbol, s.timeframe, columns)
    out = report(s, f, ex, reveal_held_back=False, held_back_from=HELD_BACK_FROM.isoformat())
    days = sorted(set(ex.day.tolist()))
    out.update(name=s.name, description=s.description, ran_at=int(time.time()), first_session=days[0], last_session=days[-1])
    return out


def run_all(symbol: str, root: Path | None = None) -> dict[str, dict]:
    out = {}
    folder = reports_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    for strategy_id in STRATEGIES:
        r = run_one(strategy_id, symbol, root)
        (folder / f"{strategy_id}-{r['ran_at']}.json").write_text(json.dumps(r), encoding="utf-8")
        out[strategy_id] = r
    return out


def latest(root: Path | None = None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for strategy_id in STRATEGIES:
        files = sorted(reports_dir(root).glob(f"{strategy_id}-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        if files:
            out[strategy_id] = json.loads(files[-1].read_text(encoding="utf-8"))
    return out


def overview(symbol: str, root: Path | None = None) -> dict:
    days = store.manifest(symbol, root)
    stored = sum(s == "ok" and d < HELD_BACK_FROM.isoformat() and is_trading_day(dt.date.fromisoformat(d)) for d, s in days.items())
    return {"held_back_from": HELD_BACK_FROM.isoformat(), "research_start": RESEARCH_START.isoformat(), "research_days_stored": stored,
            "research_days_possible": research_days_possible(), "strategies": [{"id": k, "name": v.name, "description": v.description} for k, v in STRATEGIES.items()],
            "reports": latest(root)}


def main() -> None:
    for strategy_id, r in run_all("XAUUSD").items():
        s = r["summary"]
        print(f"{strategy_id}: {r['sessions_in_data']} sessions, {s.get('trades', 0)} trades, {s.get('expectancy_r')}R, "
              f"double slippage {r['double_slippage'].get('expectancy_r')}R. {r['verdict']['text']}")


if __name__ == "__main__":
    main()
