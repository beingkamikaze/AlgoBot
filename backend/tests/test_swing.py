import datetime as dt

import numpy as np
import pytest

from app.core.costs import equity_delivery_charges, equity_intraday_charges
from app.market.models import Bar
from app.swing import data
from app.swing.data import Daily, adjust, frame_from_bars
from app.swing.engine import Entry, SwingConfig, SwingStrategy, charges, run, summary
from app.swing.strategies import GRIDS, STRATEGIES, CrossSectionalMomentum, HighBreakout, RSI2Pullback, month_ends, rank_universe

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CFG = SwingConfig(slippage_bps=0, capital_per_trade=100000)


def days(n: int, start: str = "2026-01-05") -> list[str]:
    d0 = dt.date.fromisoformat(start)
    return [(d0 + dt.timedelta(days=k)).isoformat() for k in range(n)]


def daily(rows, blocked=(), start="2026-01-05") -> Daily:
    ds = days(len(rows), start)
    bars = [Bar(t=int(dt.datetime.fromisoformat(d).replace(tzinfo=IST).timestamp()), o=o, h=h, l=lo, c=c, v=1000, complete=True)
            for d, (o, h, lo, c) in zip(ds, rows)]
    return frame_from_bars("TEST", bars, {ds[k] for k in blocked})


class Fixed(SwingStrategy):
    """Signals once, on a chosen day, with a chosen stop/target/hold; optional close-rule exit day."""
    defaults = {"at": 0, "stop": 95.0, "target": None, "max_hold": 10, "exit_day": None, "risk": None}

    def entry(self, f, ind, i):
        p = self.params
        if i != p["at"]:
            return None
        return Entry(stop=p["stop"], risk=p["risk"] or 5.0, target=p["target"], max_hold=p["max_hold"], rule="test")

    def exit_on_close(self, f, ind, i, pos):
        return i == self.params["exit_day"]


FLAT = (100, 101, 99, 100)


def test_enters_at_next_open_plus_slippage_and_skips_gap_below_stop():
    f = daily([FLAT, (100, 102, 99, 101), FLAT, FLAT])
    t = run(Fixed(at=0, max_hold=2), f, SwingConfig(slippage_bps=10))[0]
    assert t.entry_date == f.dates[1] and t.entry == pytest.approx(100.1)       # 100 * 1.001, on the tick
    assert run(Fixed(at=0, stop=100.5), f, CFG) == []                          # day 1 opens at/below the stop


def test_stop_wins_when_a_day_hits_both_and_gap_fills_at_open():
    f = daily([FLAT, FLAT, (100, 120, 90, 100), FLAT])
    t = run(Fixed(at=0, target=110.0), f, CFG)[0]
    assert t.exit_reason == "stop" and t.exit == 95.0 and t.exit_date == f.dates[2]
    g = daily([FLAT, FLAT, (90, 92, 88, 91), FLAT])
    t = run(Fixed(at=0), g, CFG)[0]
    assert t.exit_reason == "stop" and t.exit == 90.0                           # gap through the stop: the open


def test_target_needs_trade_through_and_gap_up_fills_at_open():
    f = daily([FLAT, FLAT, (100, 110, 99, 109), (100, 110.5, 99, 110), FLAT])
    t = run(Fixed(at=0, target=110.0), f, CFG)[0]
    assert t.exit_reason == "target" and t.exit == 110.0 and t.exit_date == f.dates[3]   # day 2 only touched it
    g = daily([FLAT, FLAT, (115, 116, 114, 115), FLAT])
    assert run(Fixed(at=0, target=110.0), g, CFG)[0].exit == 115.0


def test_close_rule_sells_next_open_and_max_hold_sells_at_close():
    f = daily([FLAT, FLAT, FLAT, (103, 104, 102, 103), FLAT, FLAT])
    t = run(Fixed(at=0, exit_day=2), f, CFG)[0]
    assert t.exit_reason == "rule" and t.exit == 103 and t.exit_date == f.dates[3] and t.days == 2
    t = run(Fixed(at=0, max_hold=3), f, CFG)[0]
    assert t.exit_reason == "time" and t.exit_date == f.dates[3] and t.exit == 103 and t.days == 3


def test_blocked_day_is_never_held_across_or_entered():
    f = daily([FLAT, FLAT, FLAT, (50, 51, 49, 50), FLAT], blocked=[3])
    t = run(Fixed(at=0), f, CFG)[0]
    assert t.exit_reason == "blocked" and t.exit_date == f.dates[2] and t.exit == 100
    assert run(Fixed(at=2), f, CFG) == []                                       # entry day is blocked


def test_last_day_marks_to_close():
    f = daily([FLAT, FLAT, (100, 101, 99, 100.5)])
    t = run(Fixed(at=0), f, CFG)[0]
    assert t.exit_reason == "end" and t.exit == 100.5


def test_delivery_charges_match_zerodha_rates():
    c = equity_delivery_charges(100000, 110000)
    assert c.brokerage == 0
    assert c.stt == pytest.approx(210.0)                  # 0.1 % both sides
    assert c.stamp == pytest.approx(15.0)                 # 0.015 % buy
    assert c.exchange == pytest.approx(6.45, abs=0.01)    # 0.00307 % of 2.1 lakh
    assert c.sebi == pytest.approx(0.21, abs=0.01)
    assert c.gst == pytest.approx(0.18 * (6.447 + 0.21), abs=0.01)
    assert c.dp == pytest.approx(15.34)
    assert c.total == pytest.approx(210 + 15 + 6.45 + 0.21 + 1.2 + 15.34, abs=0.03)
    assert equity_delivery_charges(100000, 0).dp == 0     # nothing sold, no DP charge


def test_trade_costs_overnight_delivery_and_same_day_intraday():
    f = daily([FLAT, FLAT, FLAT, FLAT])
    t = run(Fixed(at=0, max_hold=2), f, CFG)[0]
    assert t.costs == pytest.approx(equity_delivery_charges(100 * 1000, 100 * 1000).total)
    assert t.net == pytest.approx(-t.costs) and t.r == pytest.approx(-t.costs / (5 * 1000), abs=1e-4)
    assert t.pct == pytest.approx(-t.costs / 100000 * 100, abs=1e-4) and t.cost_r == pytest.approx(t.costs / 5000, abs=1e-4)
    g = daily([FLAT, (100, 101, 94, 96), FLAT])
    s = run(Fixed(at=0), g, CFG)[0]                                             # stopped on the entry day
    assert s.days == 1 and s.costs == pytest.approx(equity_intraday_charges(100000, 95000).total)
    assert charges(100, 95, 1000, True) < charges(100, 95, 1000, False)


def test_no_stop_uses_notional_risk_for_r():
    f = daily([FLAT, FLAT, (100, 101, 50, 60), FLAT])
    t = run(Fixed(at=0, stop=None, risk=10.0, max_hold=2), f, CFG)[0]
    assert t.exit_reason == "time" and t.exit == 60 and t.r == pytest.approx((-40000 - t.costs) / 10000, abs=1e-4)


def test_summary_counts():
    f = daily([FLAT, FLAT, (100, 111, 99, 110), FLAT, FLAT])
    tr = run(Fixed(at=0, target=110.0), f, CFG) + run(Fixed(at=0, max_hold=1), f, CFG)
    s = summary(tr, years=1.0)
    assert s["trades"] == 2 and s["win_rate"] == 0.5 and s["exit_reasons"]["target"] == 1 and s["trades_per_year"] == 2


# ---------------------------------------------------------------- data adjustment

def bars_from(rows, start="2026-01-05"):
    return list(daily(rows, start=start).__dict__["dates"]), [
        Bar(t=int(dt.datetime.fromisoformat(d).replace(tzinfo=IST).timestamp()), o=o, h=h, l=lo, c=c, v=100, complete=True)
        for d, (o, h, lo, c) in zip(days(len(rows), start), rows)]


def test_split_applied_only_when_kite_shows_it():
    ds, bars = bars_from([(200, 202, 198, 200), (200, 202, 198, 200), (100, 101, 99, 100), (100, 101, 99, 101)])
    out, rep = adjust(bars, [{"date": ds[2], "ratio": 2.0}])
    assert len(rep["applied"]) == 1 and out[0].c == 100 and out[0].v == 200 and out[2].c == 100
    ds, bars = bars_from([FLAT, FLAT, FLAT])                                    # already adjusted: no gap
    out, rep = adjust(bars, [{"date": ds[1], "ratio": 2.0}])
    assert rep["applied"] == [] and len(rep["rejected"]) == 1 and out[0].c == 100


def test_disagreement_with_yahoo_blocks_the_day_but_a_confirmed_crash_does_not():
    rows = [FLAT, FLAT, FLAT, (70, 71, 69, 70), (70, 71, 69, 70), (70, 71, 69, 70)]
    ds, bars = bars_from(rows)
    yahoo_same = dict(zip(ds, [100, 100, 100, 70, 70, 70]))
    _, rep = adjust(bars, [], yahoo_same)
    assert rep["blocked"] == []                                                 # a real 30% crash
    yahoo_flat = dict(zip(ds, [100] * 6))
    _, rep = adjust(bars, [], yahoo_flat)
    assert [b["date"] for b in rep["blocked"]] == [ds[3]]                       # e.g. a demerger


def test_zero_price_rows_dropped():
    _, bars = bars_from([(0, 0, 0, 0), FLAT, FLAT])
    out, _ = adjust(bars, [])
    assert len(out) == 2


def test_daily_until_cuts_everything():
    f = daily([FLAT] * 5)
    f.extra["x"] = np.arange(5)
    g = f.until(f.dates[3])
    assert len(g) == 3 and list(g.extra["x"]) == [0, 1, 2]


# ---------------------------------------------------------------- strategies

def walk(n=600, seed=1, drift=0.001) -> Daily:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
    o = np.concatenate([[100], c[:-1]]) * (1 + rng.normal(0, 0.003, n))
    rows = [(o[k], max(o[k], c[k]) * 1.005, min(o[k], c[k]) * 0.995, c[k]) for k in range(n)]
    return daily(rows, start="2024-01-01")


@pytest.mark.parametrize("cls", [RSI2Pullback, HighBreakout])
def test_signals_do_not_look_ahead(cls):
    f = walk()
    s = cls()
    full = s.prepare(f)
    hits = [i for i in range(len(f)) if s.entry(f, full, i) is not None]
    assert hits
    for i in hits[:10] + [len(f) // 2]:
        cut = f.until(f.dates[i + 1])
        assert (s.entry(cut, s.prepare(cut), i) is not None) == (i in hits)


def test_momentum_ranks_are_causal_and_month_end():
    frames = {f"S{k}": walk(seed=k, drift=0.0002 * k) for k in range(6)}
    for k, f in frames.items():
        f.symbol = k
    market = walk(seed=99)
    rank_universe(frames, market, [(126, 21, 2)])
    me = month_ends(market.dates)
    assert not me[-1] and me.sum() >= 12
    i = int(np.flatnonzero(me)[8])
    picked = {s for s, f in frames.items() if f.extra["top_126_21_2"][i]}
    cut = {s: f.until(f.dates[i + 2]) for s, f in frames.items()}
    for f in cut.values():
        f.extra = {}
    rank_universe(cut, market.until(market.dates[i + 2]), [(126, 21, 2)])
    assert {s for s, f in cut.items() if f.extra["top_126_21_2"][i]} == picked and len(picked) == 2
    tr = run(CrossSectionalMomentum(lookback=126, top=2), frames["S5"], CFG)
    assert all(frames["S5"].extra["month_end"][frames["S5"].dates.index(t.signal_date)] for t in tr)


def test_grids_are_small_and_include_the_published_default():
    for sid, grid in GRIDS.items():
        assert 1 <= len(grid) <= 8
        d = STRATEGIES[sid].defaults
        assert any(all(d[k] == v for k, v in g.items()) for g in grid)


def test_swing_strategies_stay_out_of_the_intraday_scan():
    from app.strategies.library import STRATEGIES as INTRADAY
    assert not any(k.startswith("swing") for k in INTRADAY)
    assert data.ACTIONS.name == "corporate_actions.json"
