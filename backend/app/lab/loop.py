"""The learning loop (guide section 3.10): how the app gets better without fooling itself.

  hypothesis  ->  a concrete parameter change to one strategy (you write it, or GPT-5
                  translates a weekly-review hypothesis; it may only use real parameters)
              ->  TEST: the unchanged strategy (baseline) and the changed one (candidate)
                  are backtested on the same stocks and days; the verdict uses ONLY the
                  held-back last 30% of sessions, needs enough trades, a positive average
                  after costs, a real improvement, and a bootstrap check whose bar rises
                  with every test you run this month (more tries need stronger evidence)
              ->  YOU approve: the candidate becomes a separate strategy VERSION that runs
                  on paper next to the original
              ->  after 20 paper trades, a version that loses money is retired automatically.
Nothing here changes a strategy you are using without your approval.
"""

from __future__ import annotations

import datetime as dt
import random
import threading
import time

from app.ai.client import AIError, chat_json, strict_object
from app.backtest import runner
from app.core.calendar import IST
from app.db import db, eq
from app.lab.registry import all_strategy_classes

MIN_OOS_TRADES = 30
MIN_IMPROVEMENT_R = 0.05
BASE_ALPHA = 0.10                 # the chance of a fluke we accept for ONE test; divided by the month's tests
PAPER_TRADES_TO_JUDGE = 20


class LabError(Exception):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(IST)


def _cls(strategy_id: str):
    classes = all_strategy_classes()
    if strategy_id not in classes:
        raise LabError(f"unknown strategy {strategy_id!r}; known: {', '.join(classes)}")
    return classes[strategy_id]


def validate_change(strategy_id: str, params: dict) -> dict:
    """Only real parameters, with the same type as their default."""
    c = _cls(strategy_id)
    if not params:
        raise LabError("the change must set at least one parameter")
    out = {}
    for k, v in params.items():
        if k not in c.defaults:
            raise LabError(f"{strategy_id} has no parameter {k!r}; it has: {', '.join(c.defaults)}")
        d = c.defaults[k]
        if isinstance(d, bool):
            if not isinstance(v, bool):
                raise LabError(f"{k} must be true or false")
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            raise LabError(f"{k} must be a number")
        elif isinstance(d, int) and not float(v).is_integer():
            raise LabError(f"{k} must be a whole number")
        if v == d:
            continue
        out[k] = int(v) if isinstance(d, int) and not isinstance(d, bool) else v
    if not out:
        raise LabError("every value equals the current default: nothing to test")
    return out


# ---------------------------------------------------------------- proposals

def create(user_id: str, hypothesis: str, strategy_id: str, params: dict, origin: str = "manual") -> dict:
    change = validate_change(strategy_id, params)
    return db().insert("proposals", {"user_id": user_id, "hypothesis": hypothesis.strip()[:500],
                                     "change": {"strategy": strategy_id, "params": change, "origin": origin},
                                     "evidence": {"tested": False}, "status": "new"})[0]


def translate(user_id: str, proposal_id: str) -> dict:
    """A review's free-text hypothesis -> a concrete parameter change, using only real parameter names."""
    p = _get(user_id, proposal_id)
    classes = all_strategy_classes()
    catalogue = {sid: {"description": c.description, "parameters": {k: {"default": v, "meaning": c.param_help.get(k, "")}
                                                                    for k, v in c.defaults.items()}}
                 for sid, c in classes.items()}
    schema = strict_object({
        "testable": {"type": "boolean"},
        "strategy_id": {"type": "string", "enum": list(classes)},
        "changes": {"type": "array", "items": strict_object({
            "parameter": {"type": "string"}, "number": {"type": ["number", "null"]}, "flag": {"type": ["boolean", "null"]}})},
        "reason": {"type": "string", "description": "if not testable, why; else how the change tests the hypothesis"},
    })
    system = ("Translate a trading hypothesis into a change of EXISTING strategy parameters, using only the parameter "
              "names in the catalogue. If no parameter change can test it, set testable=false and explain. Never invent "
              "parameters. Prefer the smallest change that tests the idea.")
    try:
        out = chat_json(system, __import__("json").dumps({"hypothesis": p["hypothesis"], "suggested_change": p["change"],
                                                          "catalogue": catalogue}, ensure_ascii=False),
                        schema, name="translate", purpose="review", user_id=user_id)
    except AIError as e:
        raise LabError(f"the model could not translate it: {e}")
    if not out["testable"]:
        db().update("proposals", {"id": eq(proposal_id)}, {"status": "rejected", "evidence": {**(p.get("evidence") or {}),
                    "not_testable": out["reason"]}, "decided_at": _now().astimezone(dt.timezone.utc).isoformat()})
        raise LabError(f"not testable with the current strategies: {out['reason']}")
    params = {c["parameter"]: (c["flag"] if c["flag"] is not None else c["number"]) for c in out["changes"]}
    change = validate_change(out["strategy_id"], params)          # the code checks the model's answer
    return db().update("proposals", {"id": eq(proposal_id)},
                       {"change": {"strategy": out["strategy_id"], "params": change, "origin": "review",
                                   "translation": out["reason"], "original": p["change"]}})[0]


def _get(user_id: str, proposal_id: str) -> dict:
    rows = db().select("proposals", {"id": eq(proposal_id), "user_id": eq(user_id)})
    if not rows:
        raise LabError("no such proposal")
    return rows[0]


def tests_this_month(user_id: str) -> int:
    start = _now().date().replace(day=1)
    rows = db().select("proposals", {"user_id": eq(user_id), "test": "not.is.null"}, columns="test")
    return sum(1 for r in rows if (r["test"] or {}).get("started_at", "") >= start.isoformat())


# ---------------------------------------------------------------- the test

def _split(trades: list[dict], cut: str) -> tuple[list[float], list[float]]:
    ins = [float(t["r"]) for t in trades if t["session"] < cut]
    oos = [float(t["r"]) for t in trades if t["session"] >= cut]
    return ins, oos


def _mean(x: list[float]) -> float | None:
    return round(float(sum(x) / len(x)), 4) if x else None


def bootstrap_p(candidate: list[float], baseline: list[float], runs: int = 2000, seed: int = 11) -> float:
    """Share of resamples in which the candidate's average R beats the baseline's."""
    if not candidate or not baseline:
        return 0.0
    rng = random.Random(seed)
    wins = 0
    for _ in range(runs):
        c = sum(rng.choice(candidate) for _ in candidate) / len(candidate)
        b = sum(rng.choice(baseline) for _ in baseline) / len(baseline)
        wins += c > b
    return wins / runs


def evaluate(baseline: list[dict], candidate: list[dict], prior_tests: int, cut: str | None = None,
             min_improvement: float = MIN_IMPROVEMENT_R, unit: str = "R") -> dict:
    """The verdict, from trades only (each needs `session` and `r`). Fixed before looking at any result.
    Swing tests (app.swing.lab) pass their fixed cutoff date, and score trades in % per trade (unit "%")."""
    sessions = sorted({t["session"] for t in baseline + candidate})
    if len(sessions) < 10:
        return {"passed": False, "reasons": ["fewer than 10 sessions of data"], "checks": {}}
    cut = cut or sessions[int(len(sessions) * 0.7)]
    b_in, b_oos = _split(baseline, cut)
    c_in, c_oos = _split(candidate, cut)
    tests = prior_tests + 1
    needed = 1 - BASE_ALPHA / tests
    p = bootstrap_p(c_oos, b_oos)
    m = {"held_back_from": cut, "baseline": {"in_sample": _mean(b_in), "held_back": _mean(b_oos), "held_back_trades": len(b_oos)},
         "candidate": {"in_sample": _mean(c_in), "held_back": _mean(c_oos), "held_back_trades": len(c_oos)},
         "bootstrap_share_better": round(p, 3), "bootstrap_needed": round(needed, 3), "tests_this_month": tests}
    checks = {      # plain bools: the result is stored as JSON
        "enough_held_back_trades": bool(len(c_oos) >= MIN_OOS_TRADES),
        "positive_after_costs": bool((m["candidate"]["held_back"] or -1) > 0),
        "improves_by_enough": bool((m["candidate"]["held_back"] or -1) - (m["baseline"]["held_back"] or 0) >= min_improvement),
        "not_luck": bool(p >= needed),
    }
    reasons = []
    if not checks["enough_held_back_trades"]:
        reasons.append(f"only {len(c_oos)} held-back trades (need {MIN_OOS_TRADES})")
    if not checks["positive_after_costs"]:
        reasons.append(f"the change still loses after costs on the held-back days ({m['candidate']['held_back']}{unit})")
    if not checks["improves_by_enough"]:
        reasons.append(f"improvement under {min_improvement}{unit} on the held-back days")
    if not checks["not_luck"]:
        reasons.append(f"could be luck: better in {p:.0%} of resamples, {needed:.0%} needed after {tests} test(s) this month")
    warnings = []
    if (m["candidate"]["in_sample"] or 0) < (m["baseline"]["in_sample"] or 0):
        warnings.append("on the first 70% of days the change was worse: the gain may not last")
    return {**m, "checks": checks, "passed": all(checks.values()), "reasons": reasons, "warnings": warnings}


def _wait(job_id: str) -> dict:
    while True:
        j = runner.get(job_id)
        if j["status"] != "running":
            return j
        time.sleep(2)


def start_test(user_id: str, proposal_id: str, universe: str | None = None) -> dict:
    p = _get(user_id, proposal_id)
    ch = p["change"] or {}
    if ch.get("kind") == "swing":
        from app.swing import lab as swing_lab
        return swing_lab.start_test(user_id, proposal_id)
    if ch.get("kind") == "fno_condor":
        from app.fno_research import lab as fno_lab
        return fno_lab.start_test(user_id, proposal_id)
    if "params" not in ch:
        raise LabError("this proposal has no concrete parameter change yet: translate it first")
    sid = ch["strategy"]
    c = _cls(sid)
    universe = universe or ("INDEXOPT" if c.market == "index_options" else "NIFTY50")
    prior = tests_this_month(user_id)
    test = {"status": "running", "started_at": _now().isoformat(), "universe": universe}
    db().update("proposals", {"id": eq(proposal_id)}, {"status": "testing", "test": test})

    def run():
        try:
            base = runner.start(sid, {}, universe, None)
            cand = runner.start(sid, ch["params"], universe, None)
            jb, jc = _wait(base["id"]), _wait(cand["id"])
            if jb["status"] != "done" or jc["status"] != "done":
                raise LabError("a backtest failed: " + "; ".join(jb.get("errors", []) + jc.get("errors", []))[:300])
            verdict = evaluate(jb["trades"], jc["trades"], prior)
            result = {**test, "status": "done", "baseline_backtest": jb["id"], "candidate_backtest": jc["id"],
                      "baseline_version": jb["strategy"]["version_key"], "candidate_version": jc["strategy"]["version_key"],
                      **verdict}
            db().update("proposals", {"id": eq(proposal_id)}, {"test": result, "status": "passed" if verdict["passed"] else "failed",
                                                                "oos_backtest_id": None})
        except Exception as e:  # noqa: BLE001 - the proposal shows what went wrong
            db().update("proposals", {"id": eq(proposal_id)}, {"test": {**test, "status": "failed", "error": str(e)[:300]},
                                                                "status": "failed"})
    threading.Thread(target=run, daemon=True, name=f"lab-{proposal_id[:8]}").start()
    return {**p, "status": "testing", "test": test}


# ---------------------------------------------------------------- variants

def approve(user_id: str, proposal_id: str) -> dict:
    p = _get(user_id, proposal_id)
    if p["status"] != "passed":
        raise LabError("only a proposal that passed its test can be approved")
    if (p["change"] or {}).get("kind") == "swing":
        raise LabError("swing strategies have no paper-trading path yet: a pass is a research result, not a version")
    if (p["change"] or {}).get("kind") == "fno_condor":
        raise LabError("option-selling research has no automatic paper path: a pass is a research result, not a version")
    t = p["test"] or {}
    sid, params = p["change"]["strategy"], p["change"]["params"]
    c = _cls(sid)(**params)
    d = db()
    existing = d.select("strategy_versions", {"strategy_id": eq(sid), "version": eq(c.version_key)})
    if existing:
        sv = d.update("strategy_versions", {"id": eq(existing[0]["id"])}, {"status": "paper", "user_id": user_id})[0]
    else:
        sv = d.insert("strategy_versions", {"strategy_id": sid, "version": c.version_key, "params": c.params, "status": "paper",
                                            "user_id": user_id,
                                            "note": f"from proposal: {p['hypothesis'][:200]} (held back {t.get('candidate', {}).get('held_back')}R)"})[0]
    return d.update("proposals", {"id": eq(proposal_id)}, {"status": "approved", "strategy_version_id": sv["id"],
                                                            "decided_at": _now().astimezone(dt.timezone.utc).isoformat()})[0]


def reject(user_id: str, proposal_id: str) -> dict:
    _get(user_id, proposal_id)
    return db().update("proposals", {"id": eq(proposal_id)}, {"status": "rejected",
                                                               "decided_at": _now().astimezone(dt.timezone.utc).isoformat()})[0]


def variants(user_id: str) -> list[dict]:
    """Every version: defaults and your approved variants, with their paper record."""
    d = db()
    rows = d.select("strategy_versions", {}, order="created_at")
    rows = [r for r in rows if not r.get("user_id") or r.get("user_id") == user_id]
    trades = d.select("trades", {"user_id": eq(user_id), "mode": eq("paper"), "exit_ts": "not.is.null"},
                      columns="net_pnl,r_multiple,entry_context")
    out = []
    for r in rows:
        mine = [t for t in trades if (t["entry_context"] or {}).get("strategy") == r["strategy_id"]
                and str((t["entry_context"] or {}).get("strategy_version") or "1") == r["version"]]
        rs = [float(t["r_multiple"]) for t in mine if t["r_multiple"] is not None]
        out.append({**r, "paper_trades": len(mine), "paper_average_r": round(sum(rs) / len(rs), 3) if rs else None})
    return out


def review_variants(user_id: str) -> list[dict]:
    """Retire a variant whose paper record, after enough trades, is negative after costs."""
    retired = []
    for v in variants(user_id):
        if v.get("user_id") == user_id and v["status"] == "paper" and v["paper_trades"] >= PAPER_TRADES_TO_JUDGE \
                and (v["paper_average_r"] or 0) <= 0:
            db().update("strategy_versions", {"id": eq(v["id"])},
                        {"status": "retired", "note": f"retired: {v['paper_trades']} paper trades averaged {v['paper_average_r']}R"})
            retired.append(v)
    return retired


def params_for(strategy_id: str, version: str | None) -> dict:
    """The parameters of a stored version (for scans and backtests); {} for the default."""
    if not version or version == _cls(strategy_id).version:
        return {}
    rows = db().select("strategy_versions", {"strategy_id": eq(strategy_id), "version": eq(version)})
    if not rows:
        raise LabError(f"no version {version!r} of {strategy_id}")
    if rows[0]["status"] == "retired":
        raise LabError(f"{strategy_id} v{version} is retired")
    defaults = _cls(strategy_id).defaults
    return {k: v for k, v in (rows[0]["params"] or {}).items() if defaults.get(k) != v}
