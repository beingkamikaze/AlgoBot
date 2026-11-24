import datetime as dt

import numpy as np
import pytest

from app.lab import loop
from app.swing import lab as swing_lab
from app.swing import universe as uni
from app.swing.engine import SwingConfig, SwingTrade, run
from app.swing.strategies import rank_universe
from tests.fakes import FakeDB
from tests.test_swing import FLAT, Fixed, daily, walk

CFG = SwingConfig(slippage_bps=0)


def test_scores_need_history_and_regular_trading():
    mdates = [f"d{k:04d}" for k in range(400)]
    me = [299, 399]
    dates = mdates[:]
    c, v = np.full(400, 10.0), np.full(400, 100.0)
    s = uni.scores(dates, c, v, mdates, me)
    assert s == {"d0299": 1000.0, "d0399": 1000.0}
    assert uni.scores(dates[200:], c[200:], v[200:], mdates, me) == {}                 # under 252 sessions of history
    sparse = [d for k, d in enumerate(mdates) if k < 180 or k % 3 == 0 or k == 299]    # few bars in the 126-day window
    assert "d0299" not in uni.scores(sparse, np.full(len(sparse), 10.0), np.full(len(sparse), 100.0), mdates, me)


def test_membership_runs_from_month_end_to_next_month_end():
    f = daily([FLAT] * 6)                                                                # 2026-01-05 .. 01-10
    members = {"2026-01-06": ["TEST"], "2026-01-08": ["OTHER"]}
    assert list(uni.membership(f, members)) == [False, True, True, False, False, False]


def test_entries_only_inside_the_universe():
    f = daily([FLAT] * 5)
    f.extra["in_universe"] = np.array([False, True, True, True, True])
    assert run(Fixed(at=0, max_hold=2), f, CFG) == []
    assert len(run(Fixed(at=1, max_hold=2), f, CFG)) == 1


def test_momentum_ranks_only_members():
    frames = {f"S{k}": walk(seed=k, drift=0.0002 * k) for k in range(6)}
    for k, f in frames.items():
        f.symbol = k
        f.extra["in_universe"] = np.full(len(f), k != "S5")                             # the strongest is never a member
    rank_universe(frames, walk(seed=99), [(126, 21, 2)])
    assert not frames["S5"].extra["top_126_21_2"].any()
    assert sum(f.extra["top_126_21_2"].sum() for f in frames.values()) > 0


def test_equal_weight_index_and_baseline():
    a = daily([(100, 100, 100, 100), (100, 110, 100, 110), (110, 121, 110, 121)])
    b = daily([(100, 100, 100, 100), (100, 100, 90, 90), (90, 90, 81, 81)])
    a.symbol, b.symbol = "A", "B"
    members = {a.dates[0]: ["A", "B"]}
    ew = uni.equal_weight_index({"A": a, "B": b}, members, a.dates)
    assert ew[a.dates[1]] == pytest.approx(1.0) and ew[a.dates[2]] == pytest.approx(1.0)  # +10% and -10% average to 0
    t = SwingTrade("A", a.dates[0], a.dates[1], a.dates[2], 100.0, 121.0, None, None, "time", 1000, 2,
                   21000, 250.0, 20750, 1.0, 20.75, 0.01, "x")
    assert uni.baseline_pct(t, ew, sorted(ew)) == pytest.approx(-0.25)                    # 0% minus the same charges
    t2 = SwingTrade(**{**t.__dict__, "exit_reason": "rule"})
    assert uni.baseline_pct(t2, ew, sorted(ew)) == pytest.approx(-0.25)


def test_evaluate_with_a_fixed_cutoff_and_percent_unit():
    base = [{"session": f"2021-{m:02d}-01", "r": 0.0} for m in range(1, 13) for _ in range(10)]
    cand = [{"session": t["session"], "r": 2.0 + (0.1 if i % 2 else -0.1)} for i, t in enumerate(base)]
    v = loop.evaluate(base, cand, 0, cut="2021-03-01", min_improvement=0.5, unit="%")
    assert v["held_back_from"] == "2021-03-01" and v["candidate"]["held_back_trades"] == 100 and v["passed"]
    v = loop.evaluate(base, [dict(t, r=0.3) for t in cand], 0, cut="2021-03-01", min_improvement=0.5, unit="%")
    assert not v["checks"]["improves_by_enough"] and "improvement under 0.5% on the held-back days" in v["reasons"]


@pytest.fixture
def fake_db(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(loop, "db", lambda: d)
    monkeypatch.setattr(swing_lab, "db", lambda: d)
    return d


def test_swing_proposal_is_tested_once_counts_toward_the_month_and_cannot_be_approved(fake_db, monkeypatch):
    seen = {}

    def fake_verdict(sid, params, prior):
        seen["prior"] = prior
        return {"passed": False, "reasons": ["x"], "checks": {}, "unit": "%"}
    monkeypatch.setattr(swing_lab, "verdict", fake_verdict)
    fake_db.tables["proposals"] = [{"id": "old", "user_id": "u", "test": {"started_at": dt.datetime.now().isoformat()},
                                    "change": {}, "status": "failed"}]
    p = swing_lab.propose("u", "swing_momentum", {}, "momentum beats holding the universe")
    assert p["change"]["kind"] == "swing" and p["status"] == "new"
    out = loop.start_test("u", p["id"])                                                  # the Lab's own entry point
    assert out["status"] == "testing"
    for th in __import__("threading").enumerate():
        if th.name.startswith("swing-lab-"):
            th.join(5)
    row = loop._get("u", p["id"])
    assert row["status"] == "failed" and row["test"]["unit"] == "%" and seen["prior"] == 1
    assert loop.tests_this_month("u") == 2
    with pytest.raises(loop.LabError, match="already tested"):
        swing_lab.start_test("u", p["id"])
    fake_db.update("proposals", {"id": f"eq.{p['id']}"}, {"status": "passed"})
    with pytest.raises(loop.LabError, match="no paper-trading path"):
        loop.approve("u", p["id"])


def test_swing_params_are_checked(fake_db):
    with pytest.raises(loop.LabError, match="no parameter"):
        swing_lab.propose("u", "swing_momentum", {"nope": 1}, "x")
    with pytest.raises(loop.LabError, match="unknown swing strategy"):
        swing_lab.propose("u", "orb", {}, "x")
