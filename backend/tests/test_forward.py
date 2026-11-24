"""Forward results: aggregation, the override and Lab-version separation, and the small-sample verdict."""

from __future__ import annotations

import datetime as dt

import pytest

from app.review import forward
from tests.fakes import FakeDB

USER = "u1"


def _trade(fdb, *, net, r, day=3, minute=0, strategy="orb", version="1", kind="equity", override=False, user=USER):
    exit_ts = dt.datetime(2026, 11, day, 9, minute, tzinfo=dt.timezone.utc).isoformat()
    ctx = {"kind": kind, "risk_amount": 1000}
    if strategy:
        ctx.update(strategy=strategy, strategy_version=version)
    if override:
        ctx["override"] = {"reasons": ["daily loss limit"], "note": "test"}
    fdb.insert("trades", {"user_id": user, "mode": "paper", "symbol": "RELIANCE", "side": "buy",
                          "entry_ts": dt.datetime(2026, 11, day, 4, 0, tzinfo=dt.timezone.utc).isoformat(),
                          "exit_ts": exit_ts, "net_pnl": net, "r_multiple": r, "rules_followed": not override,
                          "entry_context": ctx})


@pytest.fixture
def fdb(monkeypatch):
    f = FakeDB()
    monkeypatch.setattr(forward, "db", lambda: f)
    seen = []

    def latest_for(sid, version, futures=False):
        seen.append((sid, version, futures))
        rs = [1.0, -1.0, -1.0, 0.5] * 50           # mean -0.125
        return {"id": "bt1", "universe": "NIFTY50", "symbols": ["A"] * 50, "period": {"from": "2026-01-01", "to": "2026-06-30"},
                "report": {"summary": {"trades": len(rs), "expectancy_r": -0.125, "win_rate": 0.5},
                           "out_of_sample": {"expectancy_r": -0.2}},
                "trades": [{"r": r} for r in rs]}
    monkeypatch.setattr(forward.runner, "latest_for", latest_for)
    f.seen = seen
    return f


def test_stats_aggregates_r_pnl_streak_and_drawdown():
    rows = [{"net": 100, "r": 1.0, "exit_ts": "2026-11-03T09:00:00+00:00"},
            {"net": -50, "r": -0.5, "exit_ts": "2026-11-03T09:05:00+00:00"},
            {"net": -100, "r": -1.0, "exit_ts": "2026-11-03T09:10:00+00:00"},
            {"net": 20, "r": None, "exit_ts": "2026-11-03T09:15:00+00:00"},
            {"net": -10, "r": -0.1, "exit_ts": "2026-11-03T09:20:00+00:00"}]
    s = forward.stats(list(reversed(rows)))          # order of the input does not matter
    assert s["trades"] == 5 and s["trades_with_r"] == 4 and s["wins"] == 2
    assert s["win_rate"] == 0.4
    assert s["average_r"] == pytest.approx(-0.15)
    assert s["net_pnl"] == -40
    assert s["worst_losing_streak"] == 2
    assert s["max_drawdown_r"] == pytest.approx(1.6)   # peak +1.0, low -0.6
    assert [p["equity"] for p in s["curve"]] == [1.0, 0.5, -0.5, -0.6]


def test_overrides_never_mix_into_a_strategys_numbers(fdb):
    _trade(fdb, net=500, r=0.5)
    _trade(fdb, net=-1000, r=-1.0, minute=5)
    _trade(fdb, net=5000, r=5.0, minute=10, override=True)       # a big overridden win
    _trade(fdb, net=-300, r=-0.3, minute=15, strategy=None)       # no strategy linked
    _trade(fdb, net=900, r=0.9, minute=20, user="someone else")   # another user's trade
    out = forward.nse(USER, today=dt.date(2026, 11, 13))
    assert [g["key"] for g in out["strategies"]] == ["orb|1|equity"]
    orb = out["strategies"][0]["forward"]
    assert orb["trades"] == 2 and orb["average_r"] == pytest.approx(-0.25) and orb["net_pnl"] == -500
    assert out["overridden"]["trades"] == 1 and out["overridden"]["net_pnl"] == 5000
    assert out["overridden"]["by_strategy"] == [{"strategy": "orb", "trades": 1, "average_r": 5.0, "net_pnl": 5000.0}]
    assert out["unlinked"]["trades"] == 1 and out["unlinked"]["net_pnl"] == -300
    assert out["since"] == "2026-11-03"


def test_lab_versions_and_instruments_are_separate_groups(fdb):
    _trade(fdb, net=100, r=0.1)
    _trade(fdb, net=200, r=0.2, version="1+trail_atr=1.0", minute=5)
    _trade(fdb, net=300, r=0.3, kind="future", minute=10)
    out = forward.nse(USER, today=dt.date(2026, 11, 13))
    by = {g["key"]: g for g in out["strategies"]}
    assert set(by) == {"orb|1|equity", "orb|1+trail_atr=1.0|equity", "orb|1|future"}
    assert by["orb|1|equity"]["version_kind"] == "default"
    assert by["orb|1+trail_atr=1.0|equity"]["version_kind"] == "lab"
    assert ("orb", "1+trail_atr=1.0", False) in fdb.seen and ("orb", "1", True) in fdb.seen   # the matching backtest


def test_small_sample_says_too_few_and_nothing_more(fdb):
    for i in range(29):
        _trade(fdb, net=-1000, r=-1.0, minute=i)
    g = forward.nse(USER, today=dt.date(2026, 11, 13))["strategies"][0]
    assert g["verdict"] == {"kind": "too_few", "text": "Too few trades to judge (29 of 30)."}
    assert g["expected"]["range"] is None
    assert g["expected"]["average_r"] == -0.125 and g["gap"] is None   # the expectation shows, no gap is drawn


def test_verdict_inside_and_below_the_backtest_range(fdb):
    for i in range(40):
        _trade(fdb, net=-100, r=[1.0, -1.0, -1.0, 0.5][i % 4], minute=i)
    g = forward.nse(USER, today=dt.date(2026, 11, 13))["strategies"][0]
    lo, hi = g["expected"]["range"]
    assert lo < -0.125 < hi
    assert g["verdict"]["kind"] == "inside" and "In line with the backtest" in g["verdict"]["text"]

    fdb.tables["trades"].clear()
    for i in range(40):
        _trade(fdb, net=-1000, r=-1.0, minute=i)
    g = forward.nse(USER, today=dt.date(2026, 11, 13))["strategies"][0]
    assert g["verdict"]["kind"] == "below" and g["verdict"]["text"].startswith("Worse than the backtest expected")
    assert g["gap"]["average_r"] == pytest.approx(-0.875) and g["gap"]["win_rate"] == pytest.approx(-0.5)


def test_since_filters_on_the_exit_date(fdb):
    _trade(fdb, net=100, r=0.1, day=3)
    _trade(fdb, net=200, r=0.2, day=10)
    out = forward.nse(USER, since=dt.date(2026, 11, 5), today=dt.date(2026, 11, 13))
    assert out["since"] == "2026-11-05" and out["strategies"][0]["forward"]["trades"] == 1


def test_options_on_a_signal_strategy_compare_with_its_futures_backtest(fdb):
    _trade(fdb, net=100, r=0.1, kind="option")
    g = forward.nse(USER, today=dt.date(2026, 11, 13))["strategies"][0]
    assert ("orb", "1", True) in fdb.seen
    assert any("not like for like" in n for n in g["notes"])


def test_xauusd_section_groups_by_strategy_and_keeps_overrides_apart(monkeypatch):
    f = FakeDB()
    monkeypatch.setattr(forward, "db", lambda: f)
    from app.global_markets import research
    from app.global_markets.strategies.library import STRATEGIES
    sid, cls = next(iter(STRATEGIES.items()))
    monkeypatch.setattr(research, "latest", lambda root=None: {sid: {
        "summary": {"trades": 600, "expectancy_r": -0.04, "win_rate": 0.36, "avg_win_r": 1.6, "avg_loss_r": -0.98},
        "first_session": "2022-01-03", "last_session": "2025-02-04"}})
    for i in range(3):
        f.insert("global_trades", {"user_id": USER, "mode": "paper", "symbol": "XAUUSD", "entry_ts": "2026-11-10T08:00:00+00:00",
                                   "exit_ts": f"2026-11-10T09:0{i}:00+00:00", "net_pnl": -10, "r_multiple": -0.5,
                                   "rules_followed": i != 2, "entry_context": {"card": {"source": cls.name},
                                                                               "override": {"x": 1} if i == 2 else None}})
    out = forward.xauusd(USER, today=dt.date(2026, 11, 13))
    assert [g["strategy"] for g in out["strategies"]] == [sid]
    assert out["strategies"][0]["forward"]["trades"] == 2 and out["overridden"]["trades"] == 1
    assert out["strategies"][0]["verdict"]["kind"] == "too_few"
    assert out["strategies"][0]["expected"]["trades_per_month"] > 0
