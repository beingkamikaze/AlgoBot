"""The Lab's option-selling path: ONE held-back test of the weekly Nifty iron condor, recorded as a Lab proposal
so it counts toward the month's rising bar and shows on the Lab page.

Rules, fixed on 2026-11-13 before any held-back result was computed:
  - the condor rule in app.fno_research.condor, at the setting named in the proposal (one setting per test);
  - cutoff: calendar.CUTOFF (2025-02-05), the NSE research's own; trades ENTERED on or after it are held back;
  - each trade is scored in % return on its margin after slippage and today's Zerodha charges;
  - baseline: the same margin left in cash at the risk-free rate (pricing.RATE) for the same calendar days;
  - verdict: app.lab.loop.evaluate with that fixed cutoff: >= 30 held-back trades, a positive average after
    charges, at least +0.1% of margin per trade better than cash (about 5% a year on top of the risk-free
    rate), and the bootstrap bar for the number of Lab tests this month.
A pass is a research result: option structures from research have no automatic paper path, so it cannot be
approved as a strategy version. Paper-trade it by hand from the Options page if it passes.

    .venv/bin/python -m app.fno_research.lab propose --days-before 2 --short-delta 0.15 --wing 300
    .venv/bin/python -m app.fno_research.lab test <proposal-id>          # the one-time test
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading

from app.core.calendar import IST
from app.db import db, eq
from app.fno_research import calendar as fcal
from app.fno_research import condor
from app.lab import loop
from app.options import pricing

KIND = "fno_condor"
STRATEGY = "nifty_weekly_iron_condor"
MIN_IMPROVEMENT_PCT = 0.1
BASELINE = "keeping the margin in cash at the risk-free rate"


def check_params(params: dict) -> dict:
    d = condor.DEFAULTS
    bad = [k for k in params if k not in d]
    if bad:
        raise loop.LabError(f"the condor has no parameter(s) {', '.join(bad)}; it has: {', '.join(d)}")
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise loop.LabError(f"{k} must be a number")
    p = {**d, **params}
    if int(p["days_before"]) != p["days_before"] or not 1 <= p["days_before"] <= 4:
        raise loop.LabError("days_before must be a whole number from 1 to 4")
    if not 0.05 <= p["short_delta"] <= 0.4:
        raise loop.LabError("short_delta must be between 0.05 and 0.40")
    if p["wing"] <= 0 or p["wing"] % 50:
        raise loop.LabError("wing must be a positive multiple of 50 points (defined risk only)")
    if p["stop_k"] <= 0:
        raise loop.LabError("stop_k must be positive")
    return p


def scored(trades: list[condor.Trade]) -> tuple[list[dict], list[dict]]:
    """Candidate (% return on margin) and baseline (cash at the risk-free rate) per trade, keyed by entry session."""
    cand, base = [], []
    for t in trades:
        s = t.entry.isoformat()
        cand.append({"session": s, "r": t.rom_pct, "symbol": "NIFTY"})
        base.append({"session": s, "r": round(pricing.RATE * t.days_held / 365 * 100, 4), "symbol": "NIFTY"})
    return cand, base


def trades_for_test(params: dict, root=None) -> list[condor.Trade]:
    """Every trade over ALL stored days, held-back ones included: run only as the one-time test."""
    assert fcal.nse_cutoff_from_store() == fcal.CUTOFF.isoformat(), "the NSE held-back cutoff moved"
    cal = fcal.load(root)
    return condor.run(params, cal, condor.Days(root)).trades


def verdict(params: dict, prior_tests: int, trades: list[condor.Trade] | None = None) -> dict:
    tr = trades if trades is not None else trades_for_test(params)
    cand, base = scored(tr)
    v = loop.evaluate(base, cand, prior_tests, cut=fcal.CUTOFF.isoformat(), min_improvement=MIN_IMPROVEMENT_PCT,
                      unit="%")
    held = [t for t in tr if t.entry >= fcal.CUTOFF]
    extra = condor.summary(held)
    extra.pop("by_year", None)
    return {**v, "unit": "%", "baseline_is": BASELINE, "held_back_rupees_per_lot": extra}


def propose(user_id: str, params: dict, hypothesis: str) -> dict:
    change = {"kind": KIND, "strategy": STRATEGY, "params": check_params(params), "origin": "fno-research"}
    return db().insert("proposals", {"user_id": user_id, "hypothesis": hypothesis.strip()[:500], "change": change,
                                     "evidence": {"tested": False}, "status": "new"})[0]


def start_test(user_id: str, proposal_id: str, wait: bool = False) -> dict:
    p = loop._get(user_id, proposal_id)
    ch = p["change"] or {}
    if ch.get("kind") != KIND:
        raise loop.LabError("not an option-selling proposal")
    if p.get("test"):
        raise loop.LabError("this proposal was already tested: a held-back test is spent once")
    params = check_params(ch.get("params") or {})
    prior = loop.tests_this_month(user_id)
    test = {"status": "running", "started_at": dt.datetime.now(IST).isoformat(), "universe": "NIFTY options"}
    db().update("proposals", {"id": eq(proposal_id)}, {"status": "testing", "test": test})

    def run() -> None:
        try:
            v = verdict(params, prior)
            db().update("proposals", {"id": eq(proposal_id)}, {"test": {**test, "status": "done", **v},
                                                                "status": "passed" if v["passed"] else "failed"})
        except Exception as e:  # noqa: BLE001 - the proposal shows what went wrong
            db().update("proposals", {"id": eq(proposal_id)}, {"test": {**test, "status": "failed", "error": str(e)[:300]},
                                                                "status": "failed"})
    if wait:
        run()
        return loop._get(user_id, proposal_id)
    threading.Thread(target=run, daemon=True, name=f"fno-lab-{proposal_id[:8]}").start()
    return {**p, "status": "testing", "test": test}


def main(argv: list[str] | None = None) -> int:
    from app.swing.lab import _user
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("propose", help="record an iron condor proposal (runs nothing)")
    a1.add_argument("--days-before", type=int, default=condor.DEFAULTS["days_before"])
    a1.add_argument("--short-delta", type=float, default=condor.DEFAULTS["short_delta"])
    a1.add_argument("--wing", type=int, default=condor.DEFAULTS["wing"])
    a1.add_argument("--stop-k", type=float, default=condor.DEFAULTS["stop_k"])
    a1.add_argument("--hypothesis", default="")
    a1.add_argument("--user-id", default="")
    a2 = sub.add_parser("test", help="spend the one-time held-back test on a condor proposal")
    a2.add_argument("proposal_id")
    a2.add_argument("--user-id", default="")
    a2.add_argument("--accept-higher-bar", action="store_true",
                    help="run even though Lab tests were already spent this month (the bar is then higher)")
    a = ap.parse_args(argv)
    user = _user(a.user_id)
    if a.cmd == "propose":
        params = {"days_before": a.days_before, "short_delta": a.short_delta, "wing": a.wing, "stop_k": a.stop_k}
        hyp = a.hypothesis or (f"A weekly Nifty iron condor ({params['days_before']} days before expiry, "
                               f"{params['short_delta']} delta shorts, {params['wing']}-point wings, stop at "
                               f"{params['stop_k']}x credit) beats {BASELINE} on the held-back weeks after charges")
        row = propose(user, params, hyp)
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
    print(f"held back from {t['held_back_from']}: condor {t['candidate']['held_back']}% of margin per trade "
          f"({t['candidate']['held_back_trades']} trades), {BASELINE} {t['baseline']['held_back']}%")
    print(f"better in {t['bootstrap_share_better']:.0%} of resamples, {t['bootstrap_needed']:.0%} needed")
    for r in t.get("reasons", []) + t.get("warnings", []):
        print(" -", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
