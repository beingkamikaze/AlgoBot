import dataclasses
import datetime as dt

import pytest

from app.core.calendar import IST
from app.market.models import Bar
from app.paper import sim
from app.paper.service import size_for
from app.risk.engine import AccountState, Limits, OpenPosition, OrderRequest, check

NOW = dt.datetime(2026, 10, 23, 10, 30, tzinfo=IST)       # a Friday, market open, inside the window
LIM = Limits()
ST = AccountState(capital=1_000_000, cash_free=1_000_000)


def order(**kw) -> OrderRequest:
    base = dict(symbol="RELIANCE", side="buy", qty=100, limit=1200.0, stop=1190.0, target=1220.0)
    base.update(kw)
    return OrderRequest(**base)


def reasons(v) -> str:
    return " | ".join(v.reasons)


class TestRisk:
    def test_a_normal_entry_passes(self):
        v = check(order(), LIM, ST, NOW, last_price=1199.5)
        assert v.verdict == "PASS", v.reasons
        assert v.numbers["max_loss"] == pytest.approx(100 * 10 + v.numbers["charges_at_stop"])

    @pytest.mark.parametrize("kw, why", [
        (dict(stop=None), "stop is required"),
        (dict(stop=1205.0), "wrong side"),
        (dict(qty=0), "whole number"),
        (dict(limit=1200.03), "tick"),
        (dict(qty=300), "per trade"),                         # 300 x 10 = 3,000 > 2,000
    ])
    def test_format_and_loss_blocks(self, kw, why):
        v = check(order(**kw), LIM, ST, NOW, last_price=1199.5)
        assert v.verdict == "BLOCK" and why in reasons(v)

    def test_window_market_and_kill_switch(self):
        assert "entry window" in reasons(check(order(), LIM, ST, NOW.replace(hour=9, minute=20), 1199.5))
        assert "market closed" in reasons(check(order(), LIM, ST, dt.datetime(2026, 10, 24, 11, 0, tzinfo=IST), 1199.5))
        assert "Dussehra" in reasons(check(order(), LIM, ST, dt.datetime(2026, 10, 20, 11, 0, tzinfo=IST), 1199.5))
        assert "kill switch" in reasons(check(order(), LIM, dataclasses.replace(ST, kill_switch=True), NOW, 1199.5))

    def test_daily_limits(self):
        lost = dataclasses.replace(ST, realized_today=-6000)
        assert "daily limit" in reasons(check(order(), LIM, lost, NOW, 1199.5))
        near = dataclasses.replace(ST, realized_today=-5000)          # 5,000 + ~1,050 > 6,000
        assert "would be over your daily limit" in reasons(check(order(), LIM, near, NOW, 1199.5))
        assert "entries today" in reasons(check(order(), LIM, dataclasses.replace(ST, entries_today=5), NOW, 1199.5))
        assert "losses in a row" in reasons(check(order(), LIM, dataclasses.replace(ST, consecutive_losses=3), NOW, 1199.5))
        assert "monthly limit" in reasons(check(order(), LIM, dataclasses.replace(ST, realized_month=-30000), NOW, 1199.5))

    def test_positions_and_cash(self):
        pos = [OpenPosition("RELIANCE", "buy", 10, 1200, 1190)]
        assert "already have an open RELIANCE" in reasons(check(order(), LIM, dataclasses.replace(ST, open_positions=pos), NOW, 1199.5))
        poor = dataclasses.replace(ST, cash_free=50_000)
        assert "free cash" in reasons(check(order(), LIM, poor, NOW, 1199.5))
        three = [OpenPosition(s, "buy", 1, 100, 99) for s in ("A", "B", "C")]
        assert "positions open" in reasons(check(order(), LIM, dataclasses.replace(ST, open_positions=three), NOW, 1199.5))

    def test_price_sanity(self):
        assert "too far" in reasons(check(order(limit=1230.0, stop=1220.0, target=1260.0), LIM, ST, NOW, 1199.5))
        assert "beyond the stop" in reasons(check(order(), LIM, ST, NOW, 1189.0))

    def test_exits_are_never_blocked_by_limits(self):
        st = dataclasses.replace(ST, kill_switch=True, realized_today=-99999, entries_today=99,
                                 open_positions=[OpenPosition("RELIANCE", "buy", 100, 1200, 1190)])
        v = check(order(side="sell", leg="exit", stop=None, target=None), LIM, st, NOW.replace(hour=15, minute=20), 1180)
        assert v.verdict == "PASS", v.reasons
        too_many = check(order(side="sell", leg="exit", qty=200, stop=None, target=None), LIM, st, NOW, 1180)
        assert "more than the 100 held" in reasons(too_many)

    def test_sizing_fits_max_loss_with_charges(self):
        qty = size_for(1200.0, 1190.0, LIM, ST)
        v = check(order(qty=qty), LIM, ST, NOW, last_price=1199.5)
        assert v.verdict == "PASS" and v.numbers["max_loss"] <= LIM.max_loss_per_trade
        assert check(order(qty=qty + 1), LIM, ST, NOW, 1199.5).verdict == "BLOCK"


def bars(start: str, rows) -> list[Bar]:
    t0 = int(dt.datetime.fromisoformat(start).replace(tzinfo=IST).timestamp())
    return [Bar(t=t0 + 60 * i, o=o, h=h, l=lo, c=c, v=100, complete=True) for i, (o, h, lo, c) in enumerate(rows)]


class TestSim:
    def test_entry_waits_for_the_next_bar_and_fills_at_the_limit(self):
        b = bars("2026-10-23 10:00", [(100, 101, 99, 100.5), (100.5, 100.8, 100.1, 100.6), (100.4, 100.6, 99.9, 100)])
        placed = b[0].t + 30                                     # mid-way through the first bar
        f = sim.entry_fill("buy", 100.0, placed, b)
        assert f.t == b[2].t and f.price == 100.0                # the 10:00 bar is ignored; 10:01 never reached 100

    def test_entry_at_the_open_when_it_gaps_in_your_favour(self):
        b = bars("2026-10-23 10:00", [(99.5, 99.8, 99.2, 99.6)])
        f = sim.entry_fill("buy", 100.0, b[0].t, b)
        assert f.price == pytest.approx(99.5 * 1.0002, abs=0.01)

    def test_expiry(self):
        placed = int(dt.datetime(2026, 10, 23, 10, 0, tzinfo=IST).timestamp())
        assert not sim.entry_expired(placed, dt.datetime(2026, 10, 23, 10, 5, tzinfo=IST), 10, dt.time(14, 45))
        assert sim.entry_expired(placed, dt.datetime(2026, 10, 23, 10, 11, tzinfo=IST), 10, dt.time(14, 45))

    def test_stop_before_target_and_gap(self):
        b = bars("2026-10-23 10:00", [(100, 103, 98, 101)])
        e = sim.exit_check("buy", 100, 99, 102, 0, b, dt.time(15, 15))
        assert e.fill.reason == "stop" and e.fill.price == pytest.approx(99 * 0.9998, abs=0.01)
        gap = bars("2026-10-23 10:00", [(97, 97.5, 96, 97)])
        assert sim.exit_check("buy", 100, 99, 102, 0, gap, dt.time(15, 15)).fill.price == pytest.approx(97 * 0.9998, abs=0.01)

    def test_target_must_trade_through_and_short_side(self):
        touch = bars("2026-10-23 10:00", [(100, 102, 99.5, 101)])
        assert sim.exit_check("buy", 100, 99, 102, 0, touch, dt.time(15, 15)).fill is None
        short = bars("2026-10-23 10:00", [(100, 100.5, 97.9, 98)])
        e = sim.exit_check("sell", 100, 101, 98, 0, short, dt.time(15, 15))
        assert e.fill.reason == "target" and e.fill.price == 98 and e.mfe == pytest.approx(2.1)

    def test_flat_by_and_resuming_after_last_check(self):
        b = bars("2026-10-23 15:13", [(100, 100.2, 99.9, 100.1), (100.1, 100.3, 100, 100.2), (100.2, 100.4, 100.1, 100.3)])
        e = sim.exit_check("buy", 100, 99, 105, 0, b, dt.time(15, 15))
        assert e.fill.reason == "time" and e.fill.t == b[2].t
        e2 = sim.exit_check("buy", 100, 99, 105, b[2].t, b, dt.time(15, 15))
        assert e2.fill is None and e2.last_t == b[2].t               # already-seen bars are not re-checked
