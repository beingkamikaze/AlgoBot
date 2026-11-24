"""The Lab's swing path: ONE held-back test of a swing strategy, recorded as a Lab proposal so it counts
toward the month's rising bar and shows on the Lab page.

Rules, fixed on 2026-11-11 before any held-back result was computed:
  - universe: the point-in-time liquid 100 (app.swing.universe), stocks only;
  - cutoff: 2021-10-20 (app.swing.research.CUTOFF); indicators warm up on earlier days, and only trades
    ENTERED on or after the cutoff are held-back trades;
  - each trade is scored in % of the money after Zerodha delivery charges; its baseline is holding the
    whole universe in equal amounts over the same days, after the same charges (universe.baseline_pct);
  - verdict: app.lab.loop.evaluate with that fixed cutoff: >= 30 held-back trades, a positive average after
    charges, at least +0.5% per trade better than the baseline (twice the ~0.25% round-trip charges), and
    the bootstrap bar for the number of Lab tests this month.
A pass is a research result: swing strategies have no paper-trading path yet, so it cannot be approved.

    .venv/bin/python -m app.swing.lab propose --strategy swing_momentum        # records it, runs nothing
    .venv/bin/python -m app.swing.lab test <proposal-id>                       # the one-time test
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading

from app.core.calendar import IST
from app.db import db, eq
from app.lab import loop
from app.swing import research, universe as uni
from app.swing.engine import SwingConfig
from app.swing.strategies import STRATEGIES, rank_universe

MIN_IMPROVEMENT_PCT = 0.5
BASELINE = "holding the same universe for the same days"


def check_params(strategy_id: str, params: dict) -> dict:
    if strategy_id not in STRATEGIES:
        raise loop.LabError(f"unknown swing strategy {strategy_id!r}; known: {', '.join(STRATEGIES)}")
    d = STRATEGIES[strategy_id].defaults
    bad = [k for k in params if k not in d]
    if bad:
        raise loop.LabError(f"{strategy_id} has no parameter(s) {', '.join(bad)}; it has: {', '.join(d)}")
    for k, v in params.items():
        if isinstance(d[k], bool) != isinstance(v, bool) or not isinstance(v, (int, float)):
            raise loop.LabError(f"{k} must be {'true or false' if isinstance(d[k], bool) else 'a number'}")
    return dict(params)


def trades_for_test(strategy_id: str, params: dict, cfg: SwingConfig | None = None) -> tuple[list[dict], list[dict]]:
    """Candidate and baseline trades over ALL days (evaluate splits at the cutoff). Reads held-back prices:
    run only as the one-time test."""
    p = {**STRATEGIES[strategy_id].defaults, **params}
    frames, _, members = research.frames_for(research.LIQUID100)
    nifty = frames["NIFTY"]
    assert research.cutoff(nifty) == research.CUTOFF
    stocks = {s: f for s, f in frames.items() if s not in research.INDEXES}
    if strategy_id == "swing_momentum":
        rank_universe(stocks, nifty, [(p["lookback"], p["skip"], p["top"])])
    tr = research.run_setting(strategy_id, p, stocks, cfg or SwingConfig())   # stocks only: the universe is stocks
    ew = uni.equal_weight_index(stocks, members, nifty.dates)
    dates = sorted(ew)
    cand, base = [], []
    for t in tr:
        b = uni.baseline_pct(t, ew, dates)
        if b is None:
            continue
        cand.append({"session": t.entry_date, "r": t.pct, "symbol": t.symbol})
        base.append({"session": t.entry_date, "r": b, "symbol": t.symbol})
    return cand, base


def verdict(strategy_id: str, params: dict, prior_tests: int) -> dict:
    cand, base = trades_for_test(strategy_id, params)
    v = loop.evaluate(base, cand, prior_tests, cut=research.CUTOFF, min_improvement=MIN_IMPROVEMENT_PCT, unit="%")
    return {**v, "unit": "%", "baseline_is": BASELINE}


def propose(user_id: str, strategy_id: str, params: dict, hypothesis: str) -> dict:
    change = {"kind": "swing", "strategy": strategy_id, "params": check_params(strategy_id, params),
              "universe": research.LIQUID100, "origin": "swing-research"}
    return db().insert("proposals", {"user_id": user_id, "hypothesis": hypothesis.strip()[:500], "change": change,
                                     "evidence": {"tested": False}, "status": "new"})[0]


def start_test(user_id: str, proposal_id: str, wait: bool = False) -> dict:
    p = loop._get(user_id, proposal_id)
    ch = p["change"] or {}
    if ch.get("kind") != "swing":
        raise loop.LabError("not a swing proposal")
    if p.get("test"):
        raise loop.LabError("this proposal was already tested: a held-back test is spent once")
    sid, params = ch["strategy"], check_params(ch["strategy"], ch.get("params") or {})
    prior = loop.tests_this_month(user_id)
    test = {"status": "running", "started_at": dt.datetime.now(IST).isoformat(), "universe": research.LIQUID100}
    db().update("proposals", {"id": eq(proposal_id)}, {"status": "testing", "test": test})

    def run() -> None:
        try:
            v = verdict(sid, params, prior)
            db().update("proposals", {"id": eq(proposal_id)}, {"test": {**test, "status": "done", **v},
                                                                "status": "passed" if v["passed"] else "failed"})
        except Exception as e:  # noqa: BLE001 - the proposal shows what went wrong
            db().update("proposals", {"id": eq(proposal_id)}, {"test": {**test, "status": "failed", "error": str(e)[:300]},
                                                                "status": "failed"})
    if wait:
        run()
        return loop._get(user_id, proposal_id)
    threading.Thread(target=run, daemon=True, name=f"swing-lab-{proposal_id[:8]}").start()
    return {**p, "status": "testing", "test": test}


def _user(arg: str) -> str:
    if arg:
        return arg
    users = {r["user_id"] for r in db().select("proposals", {}, columns="user_id")}
    if len(users) != 1:
        raise SystemExit("pass --user-id: proposals belong to " + (f"{len(users)} users" if users else "nobody yet"))
    return users.pop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("propose", help="record a swing proposal (runs nothing)")
    a1.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    a1.add_argument("--hypothesis", default="")
    a1.add_argument("--user-id", default="")
    a2 = sub.add_parser("test", help="spend the one-time held-back test on a swing proposal")
    a2.add_argument("proposal_id")
    a2.add_argument("--user-id", default="")
    a2.add_argument("--accept-higher-bar", action="store_true",
                    help="run even though Lab tests were already spent this month (the bar is then higher)")
    a = ap.parse_args(argv)
    user = _user(a.user_id)
    if a.cmd == "propose":
        cls = STRATEGIES[a.strategy]
        hyp = a.hypothesis or (f"{cls.name} at its published defaults beats {BASELINE} on the held-back days "
                               f"(swing, delivery charges, point-in-time liquid 100)")
        row = propose(user, a.strategy, {}, hyp)
        print(f"proposal {row['id']} recorded: {hyp}")
        return 0
    prior = loop.tests_this_month(user)
    needed = 1 - loop.BASE_ALPHA / (prior + 1)
    print(f"Lab tests already spent this month: {prior}; this one needs {needed:.1%} of resamples")
    if prior and not a.accept_higher_bar:
        print("not running: wait for next month, or pass --accept-higher-bar", file=sys.stderr)
        return 3
    p = start_test(user, a.proposal_id, wait=True)
    t = p["test"] or {}
    if t.get("error"):
        print("test failed to run:", t["error"], file=sys.stderr)
        return 1
    print(f"verdict: {'PASSED' if t['passed'] else 'FAILED'}")
    print(f"held back from {t['held_back_from']}: strategy {t['candidate']['held_back']}% per trade "
          f"({t['candidate']['held_back_trades']} trades), {BASELINE} {t['baseline']['held_back']}%")
    print(f"better in {t['bootstrap_share_better']:.0%} of resamples, {t['bootstrap_needed']:.0%} needed")
    for r in t.get("reasons", []) + t.get("warnings", []):
        print(" -", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
