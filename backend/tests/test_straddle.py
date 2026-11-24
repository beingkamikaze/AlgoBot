"""The ESTIMATED-price short-straddle study: pricing, clocks, expiry calendar, stop logic, charges, discipline."""

import datetime as dt

import numpy as np
import pytest

from app.core.calendar import IST
from app.fno_research import straddle as sd
from app.options.pricing import black76


def _ts(d: dt.date, hh: int, mm: int) -> float:
    return dt.datetime.combine(d, dt.time(hh, mm), IST).timestamp()


def _weekdays(start: dt.date, end: dt.date, holidays=()) -> set[dt.date]:
    out, d = set(), start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.add(d)
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- pricing

@pytest.mark.parametrize("call", [True, False])
@pytest.mark.parametrize("strike", [21900, 22000, 22150])
def test_b76_matches_the_app_pricer(call, strike):
    fast = sd.b76(call, 22010.0, strike, 3 / 365, 0.14)
    ref = black76("CE" if call else "PE", 22010.0, strike, 3 / 365, 0.14).price
    assert fast == pytest.approx(ref, rel=1e-12)


def test_b76_at_expiry_is_intrinsic():
    assert sd.b76(True, 22010, 22000, 0.0, 0.14) == pytest.approx(10)
    assert sd.b76(False, 22010, 22000, 0.0, 0.14) == 0.0


# ---------------------------------------------------------------- calendar

def test_weekly_expiry_thursday_and_holiday_shift():
    sessions = _weekdays(dt.date(2021, 3, 1), dt.date(2021, 3, 31), holidays={dt.date(2021, 3, 11), dt.date(2021, 3, 29)})
    assert sd.weekly_expiry(dt.date(2021, 3, 1), sessions) == dt.date(2021, 3, 4)
    assert sd.weekly_expiry(dt.date(2021, 3, 4), sessions) == dt.date(2021, 3, 4)       # expiry day itself
    assert sd.weekly_expiry(dt.date(2021, 3, 5), sessions) == dt.date(2021, 3, 10)      # Thursday holiday -> Wednesday
    assert sd.weekly_expiry(dt.date(2021, 3, 10), sessions) == dt.date(2021, 3, 10)


def test_weekly_expiry_skips_a_short_special_session():
    # 2021-11-04 was a muhurat session only (not a full session): that week's expiry was Wednesday 3 Nov
    sessions = _weekdays(dt.date(2021, 11, 1), dt.date(2021, 11, 12), holidays={dt.date(2021, 11, 4), dt.date(2021, 11, 5)})
    assert sd.weekly_expiry(dt.date(2021, 11, 1), sessions) == dt.date(2021, 11, 3)
    assert sd.weekly_expiry(dt.date(2021, 11, 8), sessions) == dt.date(2021, 11, 11)


def test_weekly_expiry_moves_to_tuesday_from_september_2025():
    sessions = _weekdays(dt.date(2025, 8, 25), dt.date(2025, 9, 12))
    assert sd.weekly_expiry(dt.date(2025, 8, 25), sessions) == dt.date(2025, 8, 28)    # last Thursday weekly
    assert sd.weekly_expiry(dt.date(2025, 9, 1), sessions) == dt.date(2025, 9, 2)      # first Tuesday weekly
    assert sd.weekly_expiry(dt.date(2025, 9, 3), sessions) == dt.date(2025, 9, 9)


def test_market_years_counts_open_time_and_weighted_gaps():
    sessions = _weekdays(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    mon, thu = dt.date(2024, 1, 8), dt.date(2024, 1, 11)
    assert sd.market_years(_ts(mon, 9, 15), thu, sessions) == pytest.approx(4 / 252)
    assert sd.market_years(_ts(mon, 12, 22) + 30, thu, sessions) == pytest.approx(3.5 / 252)
    assert sd.market_years(_ts(mon, 9, 15), thu, sessions, night=0.4) == pytest.approx((4 + 3 * 0.4) / 252)
    fri, tue = dt.date(2024, 1, 5), dt.date(2024, 1, 9)
    assert sd.market_years(_ts(fri, 15, 30), tue, sessions, night=0.4, long_gap=0.9) == pytest.approx((2 + 0.9 + 0.4) / 252)
    assert sd.market_years(_ts(thu, 15, 30), thu, sessions) == 0.0
    assert sd.market_years(_ts(dt.date(2024, 1, 12), 10, 0), thu, sessions) == 0.0


def _day(d: dt.date, closes, vix=15.0) -> sd.Day:
    n = len(closes)
    t = np.array([_ts(d, 9, 15) + 300 * i for i in range(n)])
    c = np.array(closes, dtype=float)
    return sd.Day(d, t, c, c + 5, c - 5, np.full(n, vix))


@pytest.mark.parametrize("clk", ["trading", "mixed", "calendar"])
def test_every_clock_starts_from_the_calendar_price(clk):
    sessions = _weekdays(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    day = _day(dt.date(2024, 1, 8), [21500.0] * sd.BARS_PER_DAY)
    expiry = dt.date(2024, 1, 11)
    tt, scale = sd.clock(day, expiry, sessions, sd.Scenario(0.0, clk), {"night": 0.4, "long_gap": 0.9})
    t0 = float(day.t[0])
    ref = sd.b76(True, 21500, 21500, sd.calendar_years(t0, expiry), 0.15)
    assert sd.b76(True, 21500, 21500, tt(t0), 0.15 * scale, sd.calendar_years(t0, expiry)) == pytest.approx(ref, rel=1e-9)


def test_trading_clock_decays_fastest_intraday():
    sessions = _weekdays(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    day = _day(dt.date(2024, 1, 8), [21500.0] * sd.BARS_PER_DAY)
    decay = {}
    for clk in ("trading", "mixed", "calendar"):
        p = sd.premium_path(day, sessions, sd.Scenario(0.0, clk), {"night": 0.4, "long_gap": 0.9})
        assert p.strike == 21500 and p.dte == 3
        decay[clk] = (p.ce[0] + p.pe[0]) - (p.ce[-1] + p.pe[-1])
    assert decay["trading"] > decay["mixed"] > decay["calendar"] > 0


def test_iv_shift_raises_premium():
    sessions = _weekdays(dt.date(2024, 1, 1), dt.date(2024, 1, 31))
    day = _day(dt.date(2024, 1, 8), [21500.0] * sd.BARS_PER_DAY)
    lo = sd.premium_path(day, sessions, sd.Scenario(-2.0, "calendar"))
    hi = sd.premium_path(day, sessions, sd.Scenario(2.0, "calendar"))
    assert hi.ce[0] > lo.ce[0] and hi.iv0 == 17.0 and lo.iv0 == 13.0


def test_calibrate_mixed_reads_the_variance_split():
    days, d = [], dt.date(2024, 1, 1)
    rng = np.random.default_rng(1)
    level = 20000.0
    while len(days) < 200:
        if d.weekday() < 5:
            gap = rng.normal(0, 0.003 if d.weekday() else 0.006)    # Mondays gap twice as much
            open_ = level * np.exp(gap)
            close = open_ * np.exp(rng.normal(0, 0.006))
            closes = np.linspace(open_, close, sd.BARS_PER_DAY)
            days.append(_day(d, closes))
            level = close
        d += dt.timedelta(days=1)
    w = sd.calibrate_mixed(days)
    assert 0.1 < w["night"] < 0.45 and w["long_gap"] > w["night"]


# ---------------------------------------------------------------- stop logic

def _path(ce, pe, dte=2, ce_hi=None, pe_hi=None) -> sd.Path:
    ce, pe = np.array(ce, float), np.array(pe, float)
    return sd.Path(22000, dt.date(2024, 1, 11), dte, ce, pe,
                   np.array(ce_hi if ce_hi is not None else ce, float), np.array(pe_hi if pe_hi is not None else pe, float),
                   22000.0, 15.0)


NOSLIP = sd.Fills(0.0, 0.0)
DAY = _day(dt.date(2024, 1, 9), [22000.0] * 5)


def test_no_stop_exits_at_1515():
    t = sd.simulate(DAY, _path([100, 95, 90, 85, 80], [100, 98, 96, 94, 92]), sd.Setting(0.25), NOSLIP, qty=1)
    assert (t.exit_ce_reason, t.exit_pe_reason) == ("time", "time")
    assert (t.exit_ce, t.exit_pe) == (80, 92)
    assert t.gross == pytest.approx(28)
    assert t.stops == 0


def test_stop_triggers_on_a_close_at_or_above_entry_times_one_plus_s():
    # call: 124 is below the 125 stop, then 126 closes above it; slippage is paid on the stop fill
    fills = sd.Fills(0.0, 2.0)
    t = sd.simulate(DAY, _path([100, 124, 126, 140, 150], [100, 90, 85, 80, 75]), sd.Setting(0.25), fills, qty=1)
    assert t.exit_ce_reason == "stop" and t.exit_ce == pytest.approx(126 * 1.02)
    assert t.exit_pe_reason == "time" and t.exit_pe == 75
    assert t.stops == 1


def test_a_high_above_the_stop_does_not_count_at_close_trigger_but_does_intrabar():
    p = _path([100, 110, 105, 100, 95], [100, 99, 98, 97, 96], ce_hi=[100, 130, 105, 100, 95])
    assert sd.simulate(DAY, p, sd.Setting(0.25), NOSLIP, qty=1).exit_ce_reason == "time"
    t = sd.simulate(DAY, p, sd.Setting(0.25), sd.Fills(0.0, 0.0, "intrabar"), qty=1)
    assert t.exit_ce_reason == "stop" and t.exit_ce == pytest.approx(125)


def test_move_to_cost_after_the_first_stop():
    # call stops at bar 1; the put then rises back to its entry price (100) and is stopped at cost
    p = _path([100, 130, 140, 150, 160], [100, 80, 95, 101, 70])
    off = sd.simulate(DAY, p, sd.Setting(0.25, move_to_cost=False), NOSLIP, qty=1)
    on = sd.simulate(DAY, p, sd.Setting(0.25, move_to_cost=True), NOSLIP, qty=1)
    assert off.exit_pe_reason == "time" and off.exit_pe == 70
    assert on.exit_pe_reason == "cost" and on.exit_pe == 101
    assert on.stops == 2


def test_the_moved_stop_is_checked_from_the_next_bar():
    # the put already sits above its entry on the bar the call stops: not stopped on that same bar
    p = _path([100, 130, 140, 150, 160], [100, 105, 90, 80, 70])
    on = sd.simulate(DAY, p, sd.Setting(0.25, move_to_cost=True), NOSLIP, qty=1)
    assert on.exit_pe_reason == "time"


def test_both_legs_can_stop():
    p = _path([100, 130, 130, 130, 130], [100, 90, 140, 140, 140])
    t = sd.simulate(DAY, p, sd.Setting(0.30), NOSLIP, qty=1)
    assert (t.exit_ce_reason, t.exit_pe_reason) == ("stop", "stop") and t.stops == 2


def test_expiry_day_is_skipped_unless_allowed():
    p = _path([100, 90, 80, 70, 60], [100, 90, 80, 70, 60], dte=0)
    assert sd.simulate(DAY, p, sd.Setting(0.25, expiry_day=False), NOSLIP) is None
    assert sd.simulate(DAY, p, sd.Setting(0.25, expiry_day=True), NOSLIP) is not None


def test_spread_is_paid_on_entry_and_exit():
    fills = sd.Fills(0.5, 0.0)
    t = sd.simulate(DAY, _path([100] * 5, [100] * 5), sd.Setting(0.25), fills, qty=1)
    assert t.entry_ce == pytest.approx(99.5) and t.exit_ce == pytest.approx(100.5)
    assert t.gross == pytest.approx(-2.0)


# ---------------------------------------------------------------- charges and margin

def test_full_zerodha_option_charges_on_four_orders():
    t = sd.simulate(DAY, _path([100] * 5, [100] * 5), sd.Setting(0.25), NOSLIP, qty=65)
    sell, buy = 200 * 65, 200 * 65
    stt = 0.0015 * sell                               # STT on the sell side only
    exchange = 0.0003553 * (sell + buy)
    sebi = (sell + buy) * 10 / 1e7
    stamp = 0.00003 * buy
    gst = 0.18 * (80 + exchange + sebi)               # brokerage: 4 orders x Rs 20
    assert t.charges == pytest.approx(80 + stt + exchange + sebi + stamp + gst, abs=0.05)
    assert t.net == pytest.approx(t.gross - t.charges)


def test_margin_estimate_is_span_plus_exposure():
    d = dt.date(2024, 6, 1)
    m = sd.margin_estimate(22000, 22000, 3 / 365, 0.14, 65, False, d)
    exposure = 0.02 * 22000 * 65 * 2
    assert exposure < m < exposure + 0.06 * 22000 * 65 * 1.1        # a 6 % move can cost at most ~6 % of notional
    late = dt.date(2024, 12, 5)
    assert sd.margin_estimate(22000, 22000, 0.5 / 365, 0.14, 65, True, late) > \
        sd.margin_estimate(22000, 22000, 0.5 / 365, 0.14, 65, True, d) + 0.019 * 22000 * 65 * 2


def test_summary_and_years():
    trades = [sd.simulate(DAY, _path([100, 90, 80, 70, 60], [100] * 5), sd.Setting(0.25), NOSLIP, qty=65, margin=100000.0)]
    s = sd.summarize(trades)
    assert s["trades"] == 1 and s["win_rate"] == 1.0 and s["avg_margin"] == 100000
    assert list(sd.by_year(trades)) == ["2024"]


# ---------------------------------------------------------------- discipline

def test_cutoff_is_the_pinned_nse_one():
    assert sd.held_back_cutoff() == dt.date(2025, 2, 5)


def test_run_refuses_held_back_days():
    sessions = _weekdays(dt.date(2025, 2, 3), dt.date(2025, 2, 28))
    days = [_day(dt.date(2025, 2, 4), [22000.0] * sd.BARS_PER_DAY), _day(dt.date(2025, 2, 5), [22000.0] * sd.BARS_PER_DAY)]
    with pytest.raises(AssertionError):
        sd.run(days, sessions, dt.date(2025, 2, 5))


def test_load_never_returns_a_day_on_or_after_the_cutoff(monkeypatch):
    from app.market import store
    from app.market.models import Bar

    def bars(day):
        return [Bar(t=int(_ts(day, 9, 15) + 300 * i), o=1, h=1, l=1, c=22000.0, v=0, complete=True) for i in range(75)]
    fake = {s: [b for d in (dt.date(2025, 2, 4), dt.date(2025, 2, 5), dt.date(2025, 2, 6)) for b in bars(d)]
            for s in ("NIFTY", "INDIAVIX")}
    monkeypatch.setattr(store, "read", lambda sym, tf: fake[sym])
    days, sessions, cut, _ = sd.load()
    assert [d.date for d in days] == [dt.date(2025, 2, 4)]
    assert all(d.date < cut for d in days)


def test_the_grid_is_the_fixed_eight():
    assert len(sd.GRID) == 8
    assert {s.stop_pct for s in sd.GRID} == {0.25, 0.30}


def test_straddle_iv_inverts_the_price():
    from app.fno_research.straddle_check import straddle_iv
    p = sd.b76(True, 22000, 22000, 3 / 365, 0.13) + sd.b76(False, 22000, 22000, 3 / 365, 0.13)
    assert straddle_iv(22000, 22000, 3 / 365, p) == pytest.approx(0.13, abs=1e-6)
