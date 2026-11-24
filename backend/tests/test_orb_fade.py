import datetime as dt

import pytest

from app.backtest.engine import BacktestConfig, run_symbol
from app.core.calendar import IST
from app.market.models import Bar, Candles
from app.strategies.base import Frame
from app.strategies.library import STRATEGIES, describe, make


def ts(day: str, hhmm: str) -> int:
    return int(dt.datetime.fromisoformat(f"{day} {hhmm}").replace(tzinfo=IST).timestamp())


def frame(rows) -> Frame:
    t0 = ts("2026-10-23", "09:15")
    return Frame(Candles("TEST", "TEST.NS", None, "5m",
                         [Bar(t=t0 + 300 * k, o=o, h=h, l=lo, c=c, v=1000, complete=True) for k, (o, h, lo, c) in enumerate(rows)]))


OR = [(100, 101, 99, 100.5), (100.5, 101, 99.5, 100), (100, 100.8, 99.2, 100.2)]   # 9:15-9:30: high 101, low 99
CFG = BacktestConfig(slippage_bps=0, risk_per_trade=1000, capital_per_trade=1e9)


def fade(**p):
    return make("orb_fade", {"stop_buffer_atr": 0.0, "min_target_r": 0.0, **p})


def signals(s, f):
    return {i: c for i in range(len(f)) if (c := s.check(f, i)) is not None}


class TestOrbFade:
    def test_registered(self):
        assert "orb_fade" in STRATEGIES and any(d["id"] == "orb_fade" for d in describe())
        assert set(STRATEGIES["orb_fade"].param_help) == set(STRATEGIES["orb_fade"].defaults)

    def test_failed_upside_break_sells_toward_the_midpoint(self):
        rows = OR + [(100.5, 101.6, 100.4, 101.4),     # 3: closes above the range high 101
                     (101.4, 101.8, 100.6, 100.7),     # 4: closes back inside: the signal
                     (100.7, 100.8, 100.6, 100.7)]
        sig = signals(fade(), frame(rows))
        assert list(sig) == [4]
        c = sig[4]
        assert c.side == "sell" and c.stop == pytest.approx(101.8)            # the failed break's highest high
        assert c.target_r == pytest.approx((100.7 - 100.0) / (101.8 - 100.7), abs=1e-3)   # midpoint 100

    def test_failed_downside_break_buys_and_targets_the_opposite_edge(self):
        rows = OR + [(99.5, 99.6, 98.5, 98.7), (98.7, 99.4, 98.2, 99.3), (99.3, 99.4, 99.2, 99.3)]
        c = signals(fade(target_frac=1.0), frame(rows))[4]
        assert c.side == "buy" and c.stop == pytest.approx(98.2)
        assert c.target_r == pytest.approx((101 - 99.3) / (99.3 - 98.2), abs=1e-3)

    def test_needs_a_close_outside_unless_wicks_count(self):
        rows = OR + [(100.5, 101.5, 100.4, 100.8), (100.8, 100.9, 100.6, 100.7)]   # wick above 101, close inside
        assert signals(fade(), frame(rows)) == {}
        sig = signals(fade(break_by_close=False), frame(rows))
        assert list(sig) == [3] and sig[3].stop == pytest.approx(101.5)

    def test_only_the_first_failure_per_side(self):
        rows = OR + [(100.5, 101.6, 100.4, 101.4), (101.4, 101.5, 100.6, 100.7),
                     (100.7, 101.7, 100.6, 101.5), (101.5, 101.6, 100.5, 100.6)]       # breaks and fails again
        assert list(signals(fade(), frame(rows))) == [4]

    def test_buffer_and_reward_filter(self):
        rows = OR + [(100.5, 101.6, 100.4, 101.4), (101.4, 101.8, 100.6, 100.7), (100.7, 100.8, 100.6, 100.7)]
        f = frame(rows)
        assert fade().check(f, 4).target_r < 1                                  # 0.7 reward for 1.1 risk
        assert fade(min_target_r=1.0).check(f, 4) is None
        assert fade(stop_buffer_atr=1.0, atr_n=2).check(f, 4).stop > 101.8

    def test_backtest_takes_the_fade_to_target(self):
        rows = OR + [(100.5, 101.6, 100.4, 101.4), (101.4, 101.8, 100.6, 100.7),
                     (100.7, 100.8, 99.8, 99.9)] + [(99.9, 100, 99.8, 99.9)] * 3
        t = run_symbol(fade(), frame(rows), CFG)[0]
        assert t.side == "sell" and t.entry == 100.7 and t.exit_reason == "target"
        assert t.exit == pytest.approx(100.0, abs=0.01)
