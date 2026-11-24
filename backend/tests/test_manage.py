"""Trade management (app/core/manage.py): breakeven, trailing, partial exits and the time stop, in the
backtester, the paper simulator, the paper service (fake database), option structures and the monitor."""

import dataclasses
import datetime as dt

import pytest

from app.backtest.engine import run_symbol
from app.core import manage
from app.core.calendar import IST
from app.core.costs import equity_intraday_charges
from app.monitor import service as monitor
from app.monitor.analysis import Setup
from app.paper import options as paper_options
from app.paper import service as paper
from app.paper import sim
from app.strategies.library import make

from .fakes import FakeDB
from .test_backtest import CFG, Fixed, frame
from .test_paper import bars

FLAT = (100, 100.3, 99.8, 100.1)          # a quiet bar: never near the stop (99) or the target (102)


def run(rows, **params):
    """Signal at bar 3, entry at bar 4's open (100), stop 99: risk 1.0, target 2R = 102, qty 1000."""
    return run_symbol(Fixed(at=3, stop=99.0, **params), frame([FLAT] * 4 + rows + [FLAT] * 3), CFG)[0]


class TestSettings:
    def test_every_strategy_has_the_rules_off_by_default(self):
        s = make("orb")
        assert {k: s.params[k] for k in manage.EXIT_DEFAULTS} == manage.EXIT_DEFAULTS
        assert manage.rules_from(s.params) is None and s.version_key == "1"
        assert make("orb", {"breakeven_at_r": 1.0}).version_key == "1+breakeven_at_r=1.0"

    def test_off_means_identical_results(self):
        rows = [(100, 101.5, 99.8, 101.2), (101.2, 101.3, 99.5, 100), (100, 102.5, 99.9, 102)]
        a, b = run(rows), run(rows, partial_pct=30)              # partial_pct alone switches nothing on
        assert a.management is None and b.management is None
        assert dataclasses.replace(a, rule="") == dataclasses.replace(b, rule="")


class TestBacktest:
    def test_breakeven_moves_the_stop_from_the_next_bar(self):
        rows = [(100, 101.2, 99.95, 101), (101, 101.1, 99.9, 100)]
        t = run(rows, breakeven_at_r=1.0)
        assert t.exit_reason == "stop" and t.exit == 100.0 and t.stop == 99.0        # stop recorded as the first one
        m = t.management
        assert m["final_stop"] == 100.0 and len(m["stop_moves"]) == 1 and m["stop_moves"][0]["to"] == 100.0
        assert run(rows).exit_reason != "stop"                   # unmanaged: 99.9 never reaches 99

    def test_the_bar_that_reaches_breakeven_does_not_use_it(self):
        t = run([(100, 101.2, 99.95, 101), (101, 101.5, 100.5, 101)], breakeven_at_r=1.0)
        assert t.exit_t > t.entry_t + 300                        # bar 4 dipped under 100 but its stop was still 99
        assert t.exit_reason == "stop" and t.exit == 100.0       # the later quiet bars (low 99.8) do hit the moved stop

    def test_moved_stop_and_target_in_one_bar_counts_as_the_stop(self):
        t = run([(100, 101.2, 99.8, 101), (101, 102.5, 99.9, 102)], breakeven_at_r=1.0)
        assert t.exit_reason == "stop" and t.exit == 100.0

    def test_trailing_after_breakeven(self):
        # 16 bars with a true range of 1.0, so ATR(14) at the signal bar is 1.0
        quiet = [(100, 100.5, 99.5, 100)] * 16
        rows = quiet + [(100, 101.5, 99.8, 101.4), (101.4, 101.6, 100.9, 101.2)] + [FLAT] * 3
        t = run_symbol(Fixed(at=15, stop=99.0, breakeven_at_r=1.0, trail_atr=0.5), frame(rows), CFG)[0]
        moves = t.management["stop_moves"]
        assert [mv["to"] for mv in moves] == [100.0, 101.0]      # breakeven, then 0.5 x ATR behind the best 101.5
        assert t.exit_reason == "stop" and t.exit == 101.0 and t.r > 0

    def test_partial_then_target_with_charges_per_exit(self):
        t = run([(100, 101.5, 99.8, 101.2), (101.2, 102.5, 101, 102.4)], partial_at_r=1.0, partial_pct=50)
        assert t.exit_reason == "target" and t.management["partial"]["qty"] == 500
        assert t.management["partial"]["price"] == 101.0
        assert t.gross == pytest.approx(500 * 1.0 + 500 * 2.0)
        costs = equity_intraday_charges(100 * 500, 101 * 500).total + equity_intraday_charges(100 * 500, 102 * 500).total
        assert t.costs == pytest.approx(costs) and t.r == pytest.approx((t.gross - costs) / 1000, abs=1e-4)

    def test_stop_wins_over_the_partial_in_one_bar(self):
        t = run([(100, 101.5, 98.9, 99)], partial_at_r=1.0)
        assert t.exit_reason == "stop" and t.management["partial"] is None and t.gross == pytest.approx(-1000)

    def test_time_stop(self):
        t = run([FLAT] * 7, time_stop_minutes=30)
        entry_local = dt.datetime.fromtimestamp(t.entry_t, IST)
        exit_local = dt.datetime.fromtimestamp(t.exit_t, IST)
        assert t.exit_reason == "time-stop" and (exit_local - entry_local).seconds == 25 * 60   # the bar ending 30 min in
        assert t.exit == pytest.approx(100.1)
        working = run([FLAT] * 5 + [(100.1, 100.6, 100, 100.5)] + [FLAT], time_stop_minutes=30)
        assert working.exit_reason != "time-stop"                # +0.5R at 30 minutes: it stays


class TestSim:
    def state(self, **rules):
        return manage.start({**manage.EXIT_DEFAULTS, **rules}, long=True, entry=100.0, stop=99.0, qty=100,
                            entry_t=bars("2026-10-23 10:00", [FLAT])[0].t, target_r=2.0, tick=0.01)

    def test_breakeven_and_stop(self):
        b = bars("2026-10-23 10:00", [FLAT, (100.1, 101.2, 100, 101), (101, 101.1, 99.9, 100)])
        m = self.state(breakeven_at_r=1.0)
        e = sim.exit_check("buy", 100, 99, 102, 0, b, dt.time(15, 15), m=m)
        assert e.fill.reason == "stop" and e.fill.price == pytest.approx(100 * 0.9998, abs=0.01)
        assert [mv["to"] for mv in e.moves] == [100.0] and m["stop"] == 100.0

    def test_fill_bar_never_moves_the_stop(self):
        b = bars("2026-10-23 10:00", [(100, 101.5, 99.9, 101)])         # the high may have come before the fill
        m = self.state(breakeven_at_r=1.0)
        assert sim.exit_check("buy", 100, 99, 102, 0, b, dt.time(15, 15), m=m).moves == []

    def test_partial_then_resume(self):
        b = bars("2026-10-23 10:00", [FLAT, (100.1, 101.5, 100, 101.2)])
        m = self.state(partial_at_r=1.0, partial_pct=50)
        e = sim.exit_check("buy", 100, 99, 102, 0, b, dt.time(15, 15), m=m)
        assert e.fill is None and e.partial == {"t": b[1].t, "qty": 50, "price": 101.0, "at_r": 1.0}
        later = bars("2026-10-23 10:02", [(101.2, 101.8, 101.1, 101.5)])
        assert sim.exit_check("buy", 100, 99, 102, e.last_t, later, dt.time(15, 15), m=m).partial is None   # once only

    def test_time_stop(self):
        b = bars("2026-10-23 10:00", [FLAT] * 12)
        e = sim.exit_check("buy", 100, 99, 102, 0, b, dt.time(15, 15), m=self.state(time_stop_minutes=10))
        assert e.fill.reason == "time-stop" and e.fill.t == b[9].t      # the 1-minute bar that closes 10 min in


NOW = dt.datetime(2026, 10, 23, 10, 10, tzinfo=IST)
UID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def fdb(monkeypatch):
    f = FakeDB()
    for mod in (paper, paper_options, monitor):
        monkeypatch.setattr(mod, "db", lambda: f)
    monkeypatch.setattr(paper, "_now", lambda: NOW)
    monkeypatch.setattr(paper, "expire_stale_proposals", lambda now: 0)
    from app.journal import postmortem
    from app.webhooks import tradingview
    monkeypatch.setattr(postmortem, "schedule", lambda *a, **k: None)
    monkeypatch.setattr(tradingview, "expire_stale_tickets", lambda now: 0)
    monkeypatch.setattr(monitor, "_state", {})
    return f


class TestPaperService:
    def test_tick_partial_breakeven_and_close(self, fdb, monkeypatch):
        b = bars("2026-10-23 10:00", [FLAT, (100.1, 101.5, 100, 101.2), (101.2, 101.3, 99.9, 100)])
        monkeypatch.setattr(paper, "_bars_1m", lambda symbol, cache: b)
        rules = {**manage.EXIT_DEFAULTS, "breakeven_at_r": 1.0, "partial_at_r": 1.0, "partial_pct": 50}
        fdb.insert("orders", {"user_id": UID, "mode": "paper", "state": "working", "params": {
            "symbol": "TEST", "app_symbol": "TEST", "data_symbol": "TEST.NS", "kind": "equity", "lot_size": 1,
            "margin_pct": 100.0, "side": "buy", "qty": 100, "type": "LIMIT", "limit": 100.0, "stop": 99.0,
            "target": 102.0, "leg": "entry", "placed_t": b[0].t, "manage": {"rules": rules, "atr": None}}})
        paper.tick(NOW)
        t = fdb.select("trades")[0]
        ctx = t["entry_context"]
        assert t["exit_reason"] == "stop" and ctx["initial_stop"] == 99.0 and ctx["stop"] == 100.0
        assert ctx["manage"]["partial"]["qty"] == 50 and [mv["to"] for mv in ctx["manage"]["moves"]] == [100.0]
        stop_px = 99.98
        assert t["gross_pnl"] == pytest.approx(50 * 1.0 + 50 * (stop_px - 100))
        costs = equity_intraday_charges(5000, 101 * 50).total + equity_intraday_charges(5000, stop_px * 50).total
        assert t["costs"] == pytest.approx(costs)
        actions = [a["action"] for a in fdb.select("audit_log")]
        assert "partial_filled" in actions and "stop_moved" in actions
        assert any((f.get("costs") or {}).get("partial_exit") for f in fdb.select("fills"))

    def test_open_qty_after_a_partial(self):
        t = {"qty": 100, "entry_context": {"manage": {"partial": {"qty": 40}}}}
        assert paper.open_qty(t) == 60 and paper.open_qty({"qty": 100, "entry_context": {}}) == 100


class _Q:
    def __init__(self, px):
        self.bid = self.ask = self.ltp = px


class _Chain:
    def __init__(self, spot, px=100.0):
        self.spot, self.px = spot, px

    def quote(self, strike, kind):
        return _Q(self.px)


class TestOptions:
    def trade(self, fdb, rules):
        exits = {"type": "debit", "stop_pnl": -5000.0, "target_pnl": 10000.0, "underlying_stop": 24900.0,
                 "underlying_target": 25300.0, "side_hint": "buy", "manage": {"rules": rules, "atr": None}}
        mg = paper_options.start_management(exits, 25000.0, int(NOW.timestamp()))
        return fdb.insert("trades", {"user_id": UID, "mode": "paper", "symbol": "NIFTY CE", "side": "buy", "qty": 1,
                                     "entry_ts": NOW.isoformat(), "entry": 100.0, "entry_context": {
                                         "kind": "option", "underlying": "NIFTY", "expiry": "2026-10-28", "lot_size": 75,
                                         "legs": [{"kind": "CE", "strike": 25000, "side": "buy", "lots": 1, "price": 100.0,
                                                   "tradingsymbol": "NIFTY26OCT25000CE"}],
                                         "net_premium": -100.0, "exits": exits, "risk_amount": 3750, "spot_at_entry": 25000.0,
                                         "manage": mg, "initial_underlying_stop": 24900.0}})[0]

    def test_index_breakeven_then_index_stop(self, fdb, monkeypatch):
        rules = {**manage.EXIT_DEFAULTS, "breakeven_at_r": 1.0, "partial_at_r": 1.0}
        t = self.trade(fdb, rules)
        assert "partial exits are not applied to option structures" in t["entry_context"]["manage"]["notes"]
        spot = {"v": 25120.0}
        monkeypatch.setattr(paper_options.ch, "chain", lambda *a, **k: _Chain(spot["v"]))
        assert paper_options.manage_trade(t, NOW + dt.timedelta(minutes=1), dt.time(15, 15)) is None
        t = fdb.select("trades")[0]
        assert t["entry_context"]["exits"]["underlying_stop"] == 25000.0
        spot["v"] = 24990.0                                      # above the first stop, below the moved one
        upd = paper_options.manage_trade(t, NOW + dt.timedelta(minutes=2), dt.time(15, 15))
        assert upd["exit_reason"] == "index stop"

    def test_index_time_stop(self, fdb, monkeypatch):
        t = self.trade(fdb, {**manage.EXIT_DEFAULTS, "time_stop_minutes": 15})
        monkeypatch.setattr(paper_options.ch, "chain", lambda *a, **k: _Chain(25010.0))
        assert paper_options.manage_trade(t, NOW + dt.timedelta(minutes=5), dt.time(15, 15)) is None
        upd = paper_options.manage_trade(fdb.select("trades")[0], NOW + dt.timedelta(minutes=16), dt.time(15, 15))
        assert upd["exit_reason"] == "time-stop"


class TestMonitor:
    def test_setup_uses_the_first_stop_for_r_and_shows_the_moved_one(self):
        t = {"symbol": "TEST", "side": "buy", "entry": 100.0, "entry_ts": NOW.isoformat(),
             "entry_context": {"stop": 100.0, "initial_stop": 99.0, "target": 102.0}}
        s = monitor.setup_for(t, dt.time(15, 15))
        assert s.stop == 99.0 and s.live_stop == 100.0
        assert Setup("X", 1, 100.0, 99.0, 102.0, 0).live_stop == 99.0

    def test_alert_when_the_stop_moves_or_a_partial_fills(self, fdb):
        mv = {"t": int(NOW.timestamp()), "from": 99.0, "to": 100.0, "why": "breakeven: the trade reached +1R"}
        monitor._management_alerts("trade-1", UID, "TEST", {"moves": [mv], "partial": None}, NOW)
        assert monitor._state["trade-1"]["alerts"] == []         # already there when first seen: shown, not alerted
        part = {"t": mv["t"] + 60, "qty": 50, "price": 101.0, "at_r": 1.0}
        monitor._management_alerts("trade-1", UID, "TEST", {"moves": [mv, {**mv, "from": 100.0, "to": 100.5}],
                                                             "partial": part}, NOW)
        titles = [a["title"] for a in monitor._state["trade-1"]["alerts"]]
        assert titles == ["TEST: stop moved 100.00 → 100.50", "TEST: partial exit filled"]
        assert len(fdb.select("risk_events")) == 2
