import datetime as dt

import numpy as np

from app.core.calendar import IST
from app.monitor.analysis import Bars, Setup, analyze, r_of, support_resistance

DAY = "2026-11-02"
T0 = int(dt.datetime(2026, 11, 2, 9, 15, tzinfo=IST).timestamp())


def bars(closes, step=60, day=DAY, start=T0, spread=1.0, vol=0.0):
    c = np.array(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    t = np.array([start + i * step for i in range(len(c))])
    return Bars(t=t, o=o, h=np.maximum(o, c) + spread, l=np.minimum(o, c) - spread, c=c, v=np.full(len(c), vol),
                session=np.array([day] * len(c)), minute=np.array([i * step // 60 for i in range(len(c))]))


def with_prev(b: Bars, prev_close=100.0, prev_high=103.0, prev_low=97.0):
    """Prepend a previous session of 5-minute bars."""
    n = 75
    c = np.linspace(prev_low, prev_high, n)
    c[-1] = prev_close
    p = bars(c, step=300, day="2026-10-30", start=T0 - 3 * 86400)
    return Bars(*(np.r_[getattr(p, f), getattr(b, f)] for f in ("t", "o", "h", "l", "c", "v", "session", "minute")))


def test_r_and_progress():
    s = Setup("X", 1, 100.0, 98.0, 104.0, T0)
    assert r_of(101.0, s) == 0.5 and r_of(97.0, s) == -1.5
    assert r_of(99.0, Setup("X", -1, 100.0, 102.0, 96.0, T0)) == 0.5


def test_trade_going_well_is_on_track():
    up1 = bars(np.linspace(100, 103, 90))
    up5 = with_prev(bars(np.linspace(100, 103, 18), step=300))
    s = Setup("X", 1, 100.5, 99.0, 104.0, T0 + 30 * 60, strategy=None)
    a = analyze(s, up1, up5, dt.datetime(2026, 11, 2, 10, 45, tzinfo=IST))
    assert a["health"] == "on track", [c for c in a["checks"] if not c["ok"]]
    assert a["progress"]["r"] > 1 and a["progress"]["mae_r"] <= a["progress"]["r"]


def test_failed_orb_breakout_is_thesis_broken():
    # opening range 100-102 in the first 15 min, breakout to 104, then back inside the range below entry
    c5 = [101, 100.5, 102, 103.5, 104, 103, 101.5, 101, 100.8]
    b5 = with_prev(bars(c5, step=300))
    b1 = bars(np.r_[np.linspace(101, 104, 25), np.linspace(104, 100.8, 20)])
    s = Setup("X", 1, 103.0, 100.0, 109.0, T0 + 20 * 60, strategy="orb")
    a = analyze(s, b1, b5, dt.datetime(2026, 11, 2, 10, 0, tzinfo=IST))
    thesis = next(c for c in a["checks"] if c["key"] == "thesis")
    assert not thesis["ok"] and "back inside the range" in thesis["detail"]
    assert a["health"] == "thesis broken"


def test_sudden_bar_against_is_flagged():
    c = list(np.linspace(100, 101, 60)) + [101.0, 101.0]
    b1 = bars(c, spread=0.05)
    b1.o[-1], b1.c[-1], b1.h[-1], b1.l[-1] = 101.0, 99.0, 101.0, 98.9      # a 2-point red bar after quiet 0.1-point bars
    b5 = with_prev(bars(np.linspace(100, 101, 12), step=300))
    s = Setup("X", 1, 100.5, 98.0, 106.0, T0 + 10 * 60)
    a = analyze(s, b1, b5, dt.datetime(2026, 11, 2, 10, 17, tzinfo=IST))
    shock = next(c for c in a["checks"] if c["key"] == "shock")
    assert not shock["ok"] and "against the trade" in shock["detail"] and a["health"] in ("at risk", "thesis broken")


def test_behind_the_usual_path():
    b1 = bars(np.full(120, 100.2))
    b5 = with_prev(bars(np.full(24, 100.2), step=300))
    s = Setup("X", 1, 100.0, 99.0, 102.0, T0 + 5 * 60)
    a = analyze(s, b1, b5, dt.datetime(2026, 11, 2, 11, 15, tzinfo=IST), {"median_minutes_to_target": 40})
    path = next(c for c in a["checks"] if c["key"] == "path")
    assert not path["ok"] and "usually reach target in about 40 min" in path["detail"]


def test_support_resistance_from_yesterday_and_today():
    b5 = with_prev(bars([100, 101, 102, 101, 100.5, 101.5], step=300), prev_close=100.0, prev_high=103.0, prev_low=97.0)
    sr = support_resistance(b5, 101.2, DAY)
    labels = " ".join(x["label"] for x in sr)
    assert any(x["kind"] == "resistance" and x["price"] >= 101.2 for x in sr) and any(x["kind"] == "support" for x in sr)
    assert "prev day high" in labels or "R1" in labels


def test_the_analysis_is_plain_json():
    import json
    b1 = bars(np.linspace(100, 103, 90))
    b5 = with_prev(bars(np.linspace(100, 103, 18), step=300))
    a = analyze(Setup("X", 1, 100.5, 99.0, 104.0, T0 + 30 * 60, strategy="orb"), b1, b5,
                dt.datetime(2026, 11, 2, 10, 45, tzinfo=IST), {"median_minutes_to_target": 30})
    json.dumps(a)                                    # numpy values would raise here (the API cannot send them)
    assert all(type(c["ok"]) is bool for c in a["checks"])


def test_a_trade_is_matched_to_the_signal_with_its_exact_levels(monkeypatch):
    import app.signals.scan as scanmod
    from app.monitor import service
    def fake_scan(sid, params, symbols, at):
        sigs = {"supertrend": [{"strategy_id": "supertrend", "strategy_version": "1", "side": "buy", "created_at": "2026-11-02T09:30:00+05:30",
                                "explanation": {"plan": {"stop": 54567.18, "target": 55752.23}, "summary": "ST flip"}}],
                "orb": [{"strategy_id": "orb", "strategy_version": "1", "side": "sell", "created_at": "2026-11-02T10:20:00+05:30",
                         "explanation": {"plan": {"stop": 54948.18, "target": 54496.0}, "summary": "ORB"}}]}
        return {"signals": sigs.get(sid, [])}
    monkeypatch.setattr(scanmod, "scan", fake_scan)
    s = Setup("BANKNIFTY", 1, 54781.2, 54567.18, 55752.23, T0)
    m = service.match_strategy({}, s)
    assert [x["strategy"] for x in m] == ["supertrend"]
    assert service.match_strategy({}, Setup("BANKNIFTY", -1, 54781.2, 54567.18, 55752.23, T0)) == []   # wrong direction
