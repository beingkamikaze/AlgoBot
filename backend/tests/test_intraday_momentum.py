import datetime as dt

import pytest

from app.backtest.engine import BacktestConfig, run_symbol
from app.core.calendar import IST
from app.market.models import Bar, Candles
from app.strategies.base import Frame
from app.strategies.library import STRATEGIES, describe, make


def ts(day: str, hhmm: str) -> int:
    return int(dt.datetime.fromisoformat(f"{day} {hhmm}").replace(tzinfo=IST).timestamp())


def day_bars(day: str, rows) -> list[Bar]:
    t0 = ts(day, "09:15")
    return [Bar(t=t0 + 300 * k, o=o, h=h, l=lo, c=c, v=1000, complete=True) for k, (o, h, lo, c) in enumerate(rows)]


def flat(p: float, n: int = 75):
    return [(p, p + 0.5, p - 0.5, p)] * n          # 75 five-minute bars = 9:15 to 15:30


def frame(today) -> Frame:
    bars = day_bars("2026-10-22", flat(100.0)) + day_bars("2026-10-23", today)
    return Frame(Candles("TEST", "TEST.NS", None, "5m", bars))


def first_half_hour_to(p: float):
    """Today: opens at 100, the 9:40 bar (closes 9:45) closes at p, flat at p afterwards."""
    return flat(100.0, 5) + [(100.0, max(100, p) + 0.5, min(100, p) - 0.5, p)] + flat(p, 69)


SIGNAL_BAR = 75 + 65                                   # today's 14:40 bar, which closes at 14:45
CFG = BacktestConfig(slippage_bps=0, risk_per_trade=1000, capital_per_trade=1e9)


def signals(s, f):
    return {i: c for i in range(len(f)) if (c := s.check(f, i)) is not None}


class TestIntradayMomentum:
    def test_registered(self):
        assert "intraday_momentum" in STRATEGIES and any(d["id"] == "intraday_momentum" for d in describe())
        assert set(STRATEGIES["intraday_momentum"].param_help) == set(STRATEGIES["intraday_momentum"].defaults)

    def test_rising_first_half_hour_buys_at_1445_once(self):
        sig = signals(make("intraday_momentum"), frame(first_half_hour_to(100.4)))
        assert list(sig) == [SIGNAL_BAR]                # only at 14:45, and never on the first day (no prior close)
        c = sig[SIGNAL_BAR]
        assert c.side == "buy" and c.features["early_move_pct"] == pytest.approx(0.4)
        assert c.stop < c.reference and "+0.40%" in c.rule

    def test_falling_first_half_hour_sells_unless_shorts_are_off(self):
        f = frame(first_half_hour_to(99.6))
        assert make("intraday_momentum").check(f, SIGNAL_BAR).side == "sell"
        assert make("intraday_momentum", {"allow_short": False}).check(f, SIGNAL_BAR) is None

    def test_minimum_move_filter(self):
        f = frame(first_half_hour_to(100.2))
        assert make("intraday_momentum", {"min_move_pct": 0.25}).check(f, SIGNAL_BAR) is None
        assert make("intraday_momentum", {"min_move_pct": 0.15}).check(f, SIGNAL_BAR) is not None

    def test_day_signal_uses_the_move_up_to_the_signal_bar(self):
        today = first_half_hour_to(100.4)[:60] + flat(99.5, 15)     # up early, below yesterday's close by 14:15
        f = frame(today)
        assert make("intraday_momentum").check(f, SIGNAL_BAR).side == "buy"
        assert make("intraday_momentum", {"signal": "day"}).check(f, SIGNAL_BAR).side == "sell"

    def test_entry_minute_moves_the_signal(self):
        sig = signals(make("intraday_momentum", {"entry_minute": 300}), frame(first_half_hour_to(100.4)))
        assert list(sig) == [75 + 59]                   # the 14:10 bar closes at 14:15

    def test_backtest_exits_at_the_flat_by_time(self):
        today = first_half_hour_to(100.4)[:66] + [(100.4, 101, 100.3, 100.9)] * 9
        t = run_symbol(make("intraday_momentum"), frame(today), CFG)[0]
        assert t.side == "buy" and t.entry == 100.4 and t.exit_reason == "time"
        assert t.exit == pytest.approx(100.9)
        assert dt.datetime.fromtimestamp(t.exit_t, IST).strftime("%H:%M") == "15:10"   # the bar ending 15:15
