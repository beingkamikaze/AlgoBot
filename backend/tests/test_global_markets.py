"""The global setup's clock and instruments (XAUUSD plan, phase X1)."""

import datetime as dt
import subprocess
import sys

import pytest

from app.global_markets import clock
from app.global_markets.clock import IST, LONDON, NEW_YORK, TradingWindow, can_enter, day_window, market_status, session_name, trading_day
from app.global_markets.instruments import INSTRUMENTS, resolve

UTC = dt.timezone.utc


def ny(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=NEW_YORK)


def utc(y, m, d, hh=0, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=UTC)


# ---------------------------------------------------------------- the trading week

def test_week_opens_sunday_18_00_new_york():
    assert market_status(ny(2026, 11, 8, 17, 59)).is_open is False
    assert market_status(ny(2026, 11, 8, 17, 59)).phase == "weekend"
    opened = market_status(ny(2026, 11, 8, 18, 0))
    assert opened.is_open and opened.trading_day == dt.date(2026, 11, 9)


def test_week_closes_friday_17_00_new_york():
    assert market_status(ny(2026, 11, 6, 16, 59)).is_open
    closed = market_status(ny(2026, 11, 6, 17, 0))
    assert not closed.is_open and closed.phase == "weekend"
    assert closed.next_open == ny(2026, 11, 8, 18, 0)


def test_closed_all_saturday():
    s = market_status(ny(2026, 11, 7, 12, 0))
    assert not s.is_open and s.phase == "weekend" and s.session is None
    assert s.trading_day == dt.date(2026, 11, 9)


def test_friday_evening_is_not_a_daily_break():
    assert market_status(ny(2026, 11, 6, 17, 30)).phase == "weekend"


def test_sunday_before_the_open_is_weekend_not_a_daily_break():
    assert market_status(ny(2026, 11, 8, 17, 30)).phase == "weekend"


# ---------------------------------------------------------------- the daily break

@pytest.mark.parametrize("hh,mm,is_open", [(16, 59, True), (17, 0, False), (17, 59, False), (18, 0, True)])
def test_daily_break_edges(hh, mm, is_open):
    assert market_status(ny(2026, 11, 11, hh, mm)).is_open is is_open


def test_daily_break_says_so_and_when_it_reopens():
    s = market_status(ny(2026, 11, 11, 17, 30))
    assert s.phase == "daily-break" and "break" in s.reason
    assert s.next_open == ny(2026, 11, 11, 18, 0)
    assert s.next_close == ny(2026, 11, 12, 17, 0)


def test_trading_day_rolls_at_17_00():
    assert trading_day(ny(2026, 11, 11, 16, 59)) == dt.date(2026, 11, 11)
    assert trading_day(ny(2026, 11, 11, 17, 0)) == dt.date(2026, 11, 12)
    assert trading_day(ny(2026, 11, 6, 17, 0)) == dt.date(2026, 11, 9)      # Friday's close counts towards Monday


def test_open_status_reports_this_days_close():
    s = market_status(ny(2026, 11, 11, 9, 0))
    assert s.next_close == ny(2026, 11, 11, 17, 0)
    assert s.next_open == ny(2026, 11, 11, 18, 0)


# ---------------------------------------------------------------- daylight saving

def test_daily_break_moves_in_utc_and_ist_with_new_york_daylight_saving():
    assert market_status(utc(2026, 7, 15, 20, 59)).is_open                  # summer: 17:00 New York is 21:00 UTC
    assert not market_status(utc(2026, 7, 15, 21, 0)).is_open
    assert market_status(utc(2026, 11, 11, 21, 59)).is_open                 # winter: 22:00 UTC
    assert not market_status(utc(2026, 11, 11, 22, 0)).is_open
    assert ny(2026, 11, 11, 17, 0).astimezone(IST).strftime("%a %H:%M") == "Thu 03:30"
    assert ny(2026, 7, 15, 17, 0).astimezone(IST).strftime("%a %H:%M") == "Thu 02:30"


def test_clock_change_weekends_keep_new_york_hours():
    assert market_status(ny(2026, 3, 8, 18, 0)).is_open                     # US clocks went forward that morning
    assert market_status(ny(2026, 11, 1, 18, 0)).is_open                    # US clocks went back that morning
    assert not market_status(ny(2026, 3, 8, 3, 0)).is_open
    assert not market_status(ny(2026, 11, 1, 1, 30)).is_open


def test_window_in_ist_winter_matches_the_plan():
    w = day_window(dt.date(2026, 11, 11))
    assert [x.astimezone(IST).strftime("%H:%M") for x in (w.entry_start, w.entry_end, w.flat_by)] == ["13:30", "21:30", "23:30"]


def test_window_in_ist_summer_is_an_hour_earlier():
    w = day_window(dt.date(2026, 7, 15))
    assert [x.astimezone(IST).strftime("%H:%M") for x in (w.entry_start, w.entry_end, w.flat_by)] == ["12:30", "20:30", "22:30"]


def test_window_when_us_has_changed_clocks_and_uk_has_not():
    w = day_window(dt.date(2026, 3, 16))                                    # US on summer time, UK not yet
    assert w.entry_start == dt.datetime(2026, 3, 16, 8, 0, tzinfo=UTC)
    assert w.entry_end == dt.datetime(2026, 3, 16, 15, 0, tzinfo=UTC)


def test_window_when_uk_has_changed_clocks_back_and_us_has_not():
    w = day_window(dt.date(2026, 10, 27))
    assert w.entry_start == dt.datetime(2026, 10, 27, 8, 0, tzinfo=UTC)
    assert w.entry_end == dt.datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


# ---------------------------------------------------------------- sessions

@pytest.mark.parametrize("when,name", [
    (ny(2026, 11, 10, 18, 0), "asia"),
    (dt.datetime(2026, 11, 11, 7, 59, tzinfo=LONDON), "asia"),
    (dt.datetime(2026, 11, 11, 8, 0, tzinfo=LONDON), "london"),
    (ny(2026, 11, 11, 7, 59), "london"),
    (ny(2026, 11, 11, 8, 0), "new_york"),
    (ny(2026, 11, 11, 16, 59), "new_york"),
    (ny(2026, 11, 11, 17, 30), None),
    (ny(2026, 11, 7, 12, 0), None),
])
def test_sessions(when, name):
    assert session_name(when) == name


def test_sunday_evening_is_mondays_asian_session():
    assert session_name(ny(2026, 11, 8, 20, 0)) == "asia"


# ---------------------------------------------------------------- holidays

def test_good_friday_closed_from_thursday_to_sunday():
    assert clock.holidays(2026)[0][dt.date(2026, 4, 3)] == "Good Friday"
    assert clock.holidays(2025)[0][dt.date(2025, 4, 18)] == "Good Friday"
    assert clock.holidays(2027)[0][dt.date(2027, 3, 26)] == "Good Friday"
    assert market_status(ny(2026, 4, 2, 16, 0)).is_open
    s = market_status(ny(2026, 4, 3, 10, 0))
    assert not s.is_open and s.phase == "holiday" and "Good Friday" in s.reason
    assert market_status(ny(2026, 4, 2, 18, 0)).phase == "holiday"
    assert s.next_open == ny(2026, 4, 5, 18, 0)


def test_christmas_on_a_friday_2026():
    assert market_status(ny(2026, 12, 24, 12, 59)).is_open                  # Christmas Eve closes early
    eve = market_status(ny(2026, 12, 24, 13, 0))
    assert not eve.is_open and eve.phase == "holiday" and "Christmas" in eve.reason
    assert eve.next_open == ny(2026, 12, 27, 18, 0)


def test_christmas_midweek_reopens_that_evening():
    assert not market_status(ny(2025, 12, 25, 12, 0)).is_open
    assert market_status(ny(2025, 12, 25, 18, 0)).is_open


def test_new_year_on_a_friday_2027():
    assert market_status(ny(2026, 12, 31, 16, 0)).is_open
    s = market_status(ny(2027, 1, 1, 12, 0))
    assert not s.is_open and "New Year" in s.reason
    assert s.next_open == ny(2027, 1, 3, 18, 0)


def test_christmas_on_a_saturday_is_kept_on_friday():
    assert dt.date(2027, 12, 24) in clock.holidays(2027)[0]
    assert dt.date(2027, 12, 23) in clock.holidays(2027)[1]


def test_new_year_on_a_saturday_leaves_friday_open():
    assert dt.date(2027, 12, 31).weekday() == 4 and dt.date(2028, 1, 1).weekday() == 5
    assert clock.is_trading_day(dt.date(2027, 12, 31))


@pytest.mark.parametrize("day,name", [
    (dt.date(2026, 1, 19), "Martin Luther King Jr. Day"),
    (dt.date(2026, 2, 16), "Presidents' Day"),
    (dt.date(2026, 5, 25), "Memorial Day"),
    (dt.date(2026, 6, 19), "Juneteenth"),
    (dt.date(2026, 7, 3), "Independence Day"),      # the 4th is a Saturday
    (dt.date(2026, 9, 7), "Labor Day"),
    (dt.date(2026, 11, 26), "Thanksgiving"),
    (dt.date(2026, 11, 27), "the day after Thanksgiving"),
    (dt.date(2026, 12, 24), "Christmas Eve"),
])
def test_early_closes(day, name):
    assert clock.holidays(day.year)[1][day] == name
    assert clock.session_bounds(day)[1] == dt.datetime.combine(day, dt.time(13, 0), NEW_YORK)


def test_juneteenth_only_counts_from_2022():
    assert dt.date(2021, 6, 18) not in clock.holidays(2021)[1]
    assert dt.date(2022, 6, 20) in clock.holidays(2022)[1]                  # the 19th was a Sunday


def test_early_close_status_and_evening_reopen():
    s = market_status(ny(2026, 11, 26, 14, 0))
    assert not s.is_open and s.phase == "early-close" and "Thanksgiving" in s.reason
    assert s.next_open == ny(2026, 11, 26, 18, 0)
    assert market_status(ny(2026, 11, 26, 18, 0)).is_open


# ---------------------------------------------------------------- the entry window

def test_entry_window_edges():
    assert can_enter(ny(2026, 11, 11, 2, 59))[0] is False                   # 07:59 London
    assert can_enter(ny(2026, 11, 11, 3, 0))[0] is True                     # 08:00 London
    assert can_enter(ny(2026, 11, 11, 11, 0))[0] is True
    assert can_enter(ny(2026, 11, 11, 11, 1))[0] is False


def test_entry_refusals_say_why_in_ist():
    assert can_enter(ny(2026, 11, 11, 1, 0))[1] == "entries start at 13:30 IST"
    assert can_enter(ny(2026, 11, 11, 12, 0))[1] == "entries ended at 21:30 IST"
    assert "break" in can_enter(ny(2026, 11, 11, 17, 30))[1]
    assert "weekend" in can_enter(ny(2026, 11, 7, 12, 0))[1]


def test_short_day_exits_before_the_early_close():
    w = day_window(dt.date(2026, 11, 26))
    assert w.flat_by == ny(2026, 11, 26, 12, 45)
    assert w.entry_end == ny(2026, 11, 26, 11, 0)


def test_short_day_with_a_late_window_never_enters_after_the_exit():
    w = day_window(dt.date(2026, 11, 26), TradingWindow(entry_end=dt.time(12, 50), flat_by=dt.time(15, 0)))
    assert w.entry_end == w.flat_by == ny(2026, 11, 26, 12, 45)


def test_no_window_on_a_closed_day():
    assert day_window(dt.date(2026, 11, 7)) is None
    assert day_window(dt.date(2026, 12, 25)) is None


def test_a_time_without_a_zone_is_refused():
    with pytest.raises(ValueError, match="time zone"):
        market_status(dt.datetime(2026, 11, 11, 9, 0))


def test_every_day_for_years_is_consistent():
    """Whatever the holiday rules do: a day's session has a start before its end, and the window sits inside it."""
    d = dt.date(2015, 1, 1)
    while d < dt.date(2032, 1, 1):
        bounds = clock.session_bounds(d)
        if bounds is None:
            assert day_window(d) is None
        else:
            w = day_window(d)
            assert bounds[0] < w.entry_start <= w.entry_end <= w.flat_by < bounds[1], d
            assert market_status(w.entry_start).is_open and market_status(w.flat_by).trading_day == d
        d += dt.timedelta(days=1)


# ---------------------------------------------------------------- instruments

def test_xauusd_terms():
    x = resolve("XAUUSD")
    assert (x.contract_size, x.min_lot, x.lot_step, x.tick, x.quote_currency) == (100.0, 0.01, 0.01, 0.01, "USD")


@pytest.mark.parametrize("name", ["xauusd", " XAU/USD ", "XAU_USD", "xau usd"])
def test_xauusd_spellings(name):
    assert resolve(name) is INSTRUMENTS["XAUUSD"]


def test_unknown_symbol_is_refused():
    with pytest.raises(ValueError, match="not in the global setup"):
        resolve("RELIANCE")


def test_units_and_profit_by_side():
    x = resolve("XAUUSD")
    assert x.units(0.01) == 1.0 and x.units(0.02) == 2.0 and x.units(1) == 100.0
    assert x.gross_pnl("buy", 4000.0, 4010.0, 0.02) == 20.0
    assert x.gross_pnl("buy", 4000.0, 3990.0, 0.02) == -20.0
    assert x.gross_pnl("sell", 4000.0, 3990.0, 0.02) == 20.0
    assert x.gross_pnl("sell", 4000.0, 4010.0, 0.02) == -20.0


def test_gross_pnl_refuses_a_bad_side():
    with pytest.raises(ValueError, match="side"):
        resolve("XAUUSD").gross_pnl("long", 4000.0, 4010.0, 0.01)


def test_sizing_matches_the_plan():
    x = resolve("XAUUSD")
    assert x.lots_for_risk(20, 7.5) == 0.02
    assert x.lots_for_risk(20, 14) == 0.01


def test_sizing_counts_costs_and_rounds_down():
    x = resolve("XAUUSD")
    assert x.lots_for_risk(20, 10) == 0.02
    assert x.lots_for_risk(20, 10, cost_per_unit=0.6) == 0.01


def test_sizing_refuses_rather_than_rounding_up():
    assert resolve("XAUUSD").lots_for_risk(20, 25) == 0.0


def test_sizing_refuses_bad_input():
    x = resolve("XAUUSD")
    with pytest.raises(ValueError):
        x.lots_for_risk(20, 0)
    with pytest.raises(ValueError):
        x.lots_for_risk(0, 5)


def test_lot_and_tick_checks():
    x = resolve("XAUUSD")
    assert x.is_valid_lots(0.01) and x.is_valid_lots(0.07) and x.is_valid_lots(1.0)
    assert not x.is_valid_lots(0.005) and not x.is_valid_lots(0.015) and not x.is_valid_lots(0)
    assert x.is_on_tick(4074.91) and not x.is_on_tick(4074.915)
    assert x.round_price(4074.913, up=True) == 4074.92
    assert x.round_price(4074.913, up=False) == 4074.91
    assert x.round_price(4074.91, up=True) == 4074.91


# ---------------------------------------------------------------- kept apart from NSE

def test_the_global_setup_does_not_load_nse_code():
    code = ("import sys; import app.global_markets.clock, app.global_markets.instruments; "
            "print([m for m in sys.modules if m.startswith('app.') and not m.startswith('app.global_markets')])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
