import dataclasses
import datetime as dt

import numpy as np
import pytest

from app.backtest import metrics
from app.backtest.engine import BacktestConfig, Trade, run_symbol
from app.core.calendar import IST
from app.domain.signal import Condition
from app.market.models import Bar, Candles
from app.strategies.base import Candidate, Frame, Strategy
from app.strategies.library import STRATEGIES, make


def ts(day: str, hhmm: str) -> int:
    return int(dt.datetime.fromisoformat(f"{day} {hhmm}").replace(tzinfo=IST).timestamp())


def session(day: str, rows: list[tuple[float, float, float, float]], start="09:15", vol=1000, step=300) -> list[Bar]:
    t0 = ts(day, start)
    return [Bar(t=t0 + step * k, o=o, h=h, l=lo, c=c, v=vol, complete=True) for k, (o, h, lo, c) in enumerate(rows)]


def random_walk_frame(days: int = 6, seed: int = 3, tf: str = "5m") -> Frame:
    step = {"5m": 300, "15m": 900}[tf]
    rng = np.random.default_rng(seed)
    bars, price = [], 1000.0
    start = dt.date(2026, 10, 12)
    for d in range(days):
        day = (start + dt.timedelta(days=d)).isoformat()
        rows = []
        for _ in range(22500 // step):
            o = price
            c = o * (1 + rng.normal(0, 0.003))
            rows.append((o, max(o, c) * (1 + abs(rng.normal(0, 0.001))), min(o, c) * (1 - abs(rng.normal(0, 0.001))), c))
            price = c
        bars += session(day, rows, vol=int(rng.integers(500, 5000)), step=step)
    return Frame(Candles("TEST", "TEST.NS", None, tf, bars))


class Fixed(Strategy):
    """Fires once, at a chosen bar index, with a chosen stop: for testing fills."""
    id, version, name, description = "fixed", "1", "Fixed", "test"
    defaults = {"at": 3, "side": "buy", "stop": 99.0, "target_r": 2.0}

    def check(self, f, i):
        if i != self.params["at"]:
            return None
        return Candidate(side=self.params["side"], signal_index=i, reference=f.c[i], stop=self.params["stop"],
                         target_r=self.params["target_r"], rule="test", conditions=[Condition(name="x", met=True, detail="")],
                         invalidation="", stop_reason="test")


def frame(rows) -> Frame:
    return Frame(Candles("TEST", "TEST.NS", None, "5m", session("2026-10-23", rows)))


CFG = BacktestConfig(slippage_bps=0, risk_per_trade=1000, capital_per_trade=1e9)


class TestNoLookahead:
    @pytest.mark.parametrize("sid", sorted(STRATEGIES))
    def test_signals_do_not_change_when_the_future_is_cut_off(self, sid):
        s = make(sid)
        full = random_walk_frame(days=10, tf=s.timeframe)
        signals_full = {i: s.check(full, i) for i in range(len(full))}
        assert any(signals_full.values()), f"{sid} never fired on the fixture: the test would prove nothing"
        n = len(full)
        for cut in (n // 4, n // 2, n - 7):
            bars = [Bar(t=int(full.t[k]), o=full.o[k], h=full.h[k], l=full.l[k], c=full.c[k], v=int(full.v[k]),
                        complete=True) for k in range(cut)]
            short = Frame(Candles("TEST", "TEST.NS", None, s.timeframe, bars))
            for i in range(cut):
                a, b = signals_full[i], s.check(short, i)
                assert (a is None) == (b is None), f"{sid} bar {i} changed when data after {cut} was removed"
                if a and b:
                    assert a.stop == pytest.approx(b.stop) and a.rule == b.rule


class TestFills:
    def test_enters_next_open_and_hits_target_through(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(100.5, 101, 100.2, 100.8), (100.8, 104, 100.6, 103.5)] + [(103, 103, 103, 103)] * 3
        tr = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)
        assert len(tr) == 1
        t = tr[0]
        assert t.entry == 100.5 and t.exit_reason == "target"
        assert t.target == pytest.approx(100.5 + 2 * 1.5) and t.exit == pytest.approx(103.5)

    def test_stop_wins_when_both_hit_in_one_bar(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(100, 100.1, 99.9, 100), (100, 110, 90, 100)] + [(100, 100, 100, 100)] * 2
        t = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)[0]
        assert t.exit_reason == "stop" and t.exit == 99.0

    def test_gap_through_the_stop_fills_at_the_open(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(100, 100.1, 99.9, 100), (97, 97.5, 96, 97)] + [(97, 97, 97, 97)] * 2
        t = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)[0]
        assert t.exit_reason == "stop" and t.exit == 97.0          # worse than the stop

    def test_skips_when_the_open_is_already_past_the_stop(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(98, 98.5, 97.5, 98)] + [(98, 98, 98, 98)] * 2
        assert run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG) == []

    def test_touching_the_target_is_not_a_fill(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(100, 100.2, 99.9, 100), (100, 103.0, 99.9, 102)] + [(102, 102, 102, 102)] * 2
        t = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)[0]   # target = 102.0, high 103 > it
        assert t.exit_reason == "target"
        rows[5] = (100, 102.0, 99.9, 101.9)                            # high exactly at the target
        t = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)[0]
        assert t.exit_reason != "target"

    def test_short_side_and_costs(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(100, 100.2, 99.8, 100), (100, 100.1, 96, 96.5)] + [(96.5,) * 4] * 2
        t = run_symbol(Fixed(at=3, side="sell", stop=101.0), frame(rows), CFG)[0]
        assert t.side == "sell" and t.exit_reason == "target" and t.exit == 98.0
        assert t.gross == pytest.approx(2.0 * t.qty) and t.costs > 0 and t.net < t.gross

    def test_time_exit_at_flat_by(self):
        rows = [(100, 100.5, 99.8, 100)] * 75                       # a full, dull session
        t = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)[0]
        exit_local = dt.datetime.fromtimestamp(t.exit_t, IST)
        assert t.exit_reason == "time" and f"{exit_local:%H:%M}" == "15:10"   # the bar ending 15:15

    def test_slippage_makes_it_worse(self):
        rows = [(100, 101, 99.5, 100)] * 4 + [(100.5, 101, 100.2, 100.8), (100.8, 104, 100.6, 103.5)] + [(103,) * 4] * 3
        clean = run_symbol(Fixed(at=3, stop=99.0), frame(rows), CFG)[0]
        slipped = run_symbol(Fixed(at=3, stop=99.0), frame(rows), dataclasses.replace(CFG, slippage_bps=10))[0]
        assert slipped.entry > clean.entry


def trade(net: float, r: float, session="2026-10-01") -> Trade:
    return Trade(symbol="X", side="buy", session=session, signal_t=0, entry_t=ts(session, "10:00"), exit_t=0,
                 entry=100, stop=99, target=102, exit=101, exit_reason="target" if net > 0 else "stop", qty=1,
                 risk_per_share=1, gross=net, costs=0, net=net, r=r, rule="t")


class TestMetrics:
    def test_summary(self):
        ts_ = [trade(200, 2), trade(-100, -1), trade(-100, -1), trade(200, 2)]
        s = metrics.summary(ts_)
        assert s["trades"] == 4 and s["win_rate"] == 0.5 and s["expectancy_r"] == 0.5
        assert s["profit_factor"] == 2.0 and s["max_drawdown"] == 200 and s["worst_losing_streak"] == 2

    def test_verdict_needs_enough_trades(self):
        assert "Too few" in metrics.verdict(metrics.summary([trade(100, 1)] * 10))["text"]
        losing = metrics.verdict(metrics.summary([trade(-50, -0.5)] * 150))
        assert "Loses money" in losing["text"]

    def test_split_is_by_date(self):
        ts_ = [trade(1, 0.1, f"2026-10-{d:02d}") for d in range(1, 11)]
        ins, oos, cut = metrics.split_in_out(ts_)
        assert cut == "2026-10-08" and len(ins) == 7 and len(oos) == 3


class TestScanClock:
    def test_last_session_day(self):
        from app.signals.scan import last_session_day
        sat = dt.datetime(2026, 10, 24, 0, 5, tzinfo=IST)
        assert last_session_day(sat) == dt.date(2026, 10, 23)
        before_open = dt.datetime(2026, 10, 26, 8, 0, tzinfo=IST)               # Monday, pre-market
        assert last_session_day(before_open) == dt.date(2026, 10, 23)
        assert last_session_day(dt.datetime(2026, 10, 21, 8, 0, tzinfo=IST)) == dt.date(2026, 10, 19)  # after Dussehra
        assert last_session_day(dt.datetime(2026, 10, 23, 11, 0, tzinfo=IST)) == dt.date(2026, 10, 23)


class TestLatestFor:
    def _write(self, d, name, version="1", symbols=("A",), trades=1, mtime=0):
        import json, os
        from app.backtest import runner
        j = {"id": name, "status": "done", "strategy": {"id": "orb", "version_key": version}, "symbols": list(symbols),
             "report": {"summary": {"trades": trades}},
             "trades": [{"side": "buy", "entry_t": 1, "exit_t": 2, "net": 5.0, "r": 0.5, "exit_reason": "target",
                         "rule": "long text", "regime": {"vix": "low VIX (<13)", "nifty_gap": "x"}}] * trades}
        p = d / f"{name}.json"
        p.write_text(json.dumps(j))
        os.utime(p, (mtime, mtime))
        return runner

    def test_newest_matching_version_with_slim_trades_and_refresh(self, tmp_path, monkeypatch):
        from app.backtest import runner
        monkeypatch.setattr(runner, "DIR", tmp_path)
        monkeypatch.setattr(runner, "_headers", {})
        monkeypatch.setattr(runner, "_evidence_cache", {})
        self._write(tmp_path, "old", trades=1, mtime=1000)
        self._write(tmp_path, "fut", symbols=("NIFTY-FUT",), mtime=3000)
        self._write(tmp_path, "other", version="1+x=2", mtime=4000)
        j = runner.latest_for("orb", "1")
        assert j["id"] == "old" and set(j["trades"][0]) == {"side", "entry_t", "exit_t", "net", "r", "exit_reason", "regime"}
        assert j["trades"][0]["regime"] == {"vix": "low VIX (<13)"}
        assert runner.latest_for("orb", "1", futures=True)["id"] == "fut"
        assert runner.latest_for("orb", "1") is j                          # served from memory
        self._write(tmp_path, "new", trades=3, mtime=5000)
        j2 = runner.latest_for("orb", "1")
        assert j2["id"] == "new" and len(j2["trades"]) == 3
        assert "old.json" not in runner._evidence_cache                    # the superseded one is dropped
