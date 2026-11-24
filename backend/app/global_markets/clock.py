"""The global market clock: XAUUSD hours, sessions and the entry window, in New York and London time so daylight
saving is handled by the time zones. Separate from `app.core.calendar`, which is NSE's clock.

A trading day is named by the date it ends on. Monday's runs from Sunday 18:00 to Monday 17:00 New York, then a
one-hour break. Seen in `GC=F` hourly bars, Nov 2024 to Nov 2026: Sunday's first bar is 18:00, Friday's last is
before 17:00, and Saturday has none.

Holidays follow the COMEX pattern in those bars, and are NOT a spot broker's schedule (a broker's replaces it in
plan phase X8):
  - closed all day: Good Friday, Christmas, New Year's Day;
  - closes early: the US holidays below, the day after Thanksgiving, the day before Christmas. Yahoo's bars show the
    last trade between about 13:00 and 14:30 on these days, so we use 13:00, the cautious end.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
IST = ZoneInfo("Asia/Kolkata")

DAY_START = dt.time(18, 0)              # New York, on the calendar day before the trading day
DAY_END = dt.time(17, 0)                # New York
EARLY_CLOSE = dt.time(13, 0)            # New York
LONDON_SESSION_START = dt.time(8, 0)    # London
NEW_YORK_SESSION_START = dt.time(8, 0)  # New York
FLAT_BEFORE_CLOSE = dt.timedelta(minutes=15)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> dt.date:
    """Gregorian Easter (the anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 19 * m) // 433
    month = (h + m - 7 * n + 90) // 25
    return dt.date(year, month, (h + m - 7 * n + 33 * month + 19) % 32)


def _observed(d: dt.date) -> dt.date:
    """A holiday on Saturday is kept on Friday, one on Sunday on Monday."""
    return d + dt.timedelta(days={5: -1, 6: 1}.get(d.weekday(), 0))


def _previous_weekday(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def holidays(year: int) -> tuple[dict[dt.date, str], dict[dt.date, str]]:
    """(closed all day, closes early) for one calendar year."""
    closed = {_easter(year) - dt.timedelta(days=2): "Good Friday"}
    christmas = _observed(dt.date(year, 12, 25))
    closed[christmas] = "Christmas"
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:         # on a Saturday it falls in the weekend, and the Friday before stays open
        closed[_observed(new_year)] = "New Year's Day"

    thanksgiving = _nth_weekday(year, 11, 3, 4)
    early = {
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Presidents' Day",
        _last_weekday(year, 5, 0): "Memorial Day",
        _observed(dt.date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        thanksgiving: "Thanksgiving",
        thanksgiving + dt.timedelta(days=1): "the day after Thanksgiving",
        _previous_weekday(christmas): "Christmas Eve",
    }
    if year >= 2022:                    # first kept as a market holiday in 2022
        early[_observed(dt.date(year, 6, 19))] = "Juneteenth"
    return closed, {d: name for d, name in early.items() if d not in closed}


def _closed_name(d: dt.date) -> str | None:
    return holidays(d.year)[0].get(d)


def _early_name(d: dt.date) -> str | None:
    return holidays(d.year)[1].get(d)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and _closed_name(d) is None


def next_trading_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def _ny(when: dt.datetime) -> dt.datetime:
    if when.tzinfo is None:
        raise ValueError("the global clock needs a time with a time zone")
    return when.astimezone(NEW_YORK)


def session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """When trading day `day` starts and ends, or None if there is no trading that day."""
    if not is_trading_day(day):
        return None
    end = EARLY_CLOSE if _early_name(day) else DAY_END
    return (dt.datetime.combine(day - dt.timedelta(days=1), DAY_START, NEW_YORK),
            dt.datetime.combine(day, end, NEW_YORK))


def trading_day(when: dt.datetime) -> dt.date:
    """The trading day a moment counts towards: it rolls at 17:00 New York, and a closed period counts towards the
    next day that trades. Daily loss limits reset on this."""
    now = _ny(when)
    d = now.date() + dt.timedelta(days=now.time() >= DAY_END)
    return d if is_trading_day(d) else next_trading_day(d)


def session_name(when: dt.datetime) -> str | None:
    """"asia", "london" or "new_york"; None while the market is closed."""
    now = _ny(when)
    day = now.date() + dt.timedelta(days=now.time() >= DAY_START)
    bounds = session_bounds(day)
    if bounds is None or not bounds[0] <= now < bounds[1]:
        return None
    if now >= dt.datetime.combine(day, NEW_YORK_SESSION_START, NEW_YORK):
        return "new_york"
    if now >= dt.datetime.combine(day, LONDON_SESSION_START, LONDON):
        return "london"
    return "asia"


@dataclass(frozen=True)
class MarketStatus:
    now: dt.datetime
    is_open: bool
    phase: str                  # "open", "daily-break", "early-close", "weekend", "holiday"
    session: str | None         # "asia", "london", "new_york"
    reason: str | None
    trading_day: dt.date
    next_open: dt.datetime      # when it next opens; while open, the open after this day's close
    next_close: dt.datetime     # when the current day (or, while closed, the next one) ends


def _next_session(now: dt.datetime) -> tuple[dt.date, tuple[dt.datetime, dt.datetime]]:
    """The first trading day whose start is still ahead of `now`."""
    d = now.date()
    while True:
        bounds = session_bounds(d)
        if bounds and bounds[0] > now:
            return d, bounds
        d += dt.timedelta(days=1)


def market_status(when: dt.datetime) -> MarketStatus:
    now = _ny(when)
    today = now.date()
    next_day, next_bounds = _next_session(now)

    current_day = today + dt.timedelta(days=now.time() >= DAY_START)
    current = session_bounds(current_day)
    if current and current[0] <= now < current[1]:
        return MarketStatus(now=now, is_open=True, phase="open", session=session_name(now), reason=None,
                            trading_day=current_day, next_open=next_bounds[0], next_close=current[1])

    skipped = (today + dt.timedelta(days=i) for i in range((next_day - today).days))
    closed_day = next((d for d in skipped if _closed_name(d)), None)
    today_bounds = session_bounds(today)
    if closed_day:
        phase, reason = "holiday", f"closed for {_closed_name(closed_day)}"
    elif today_bounds and _early_name(today) and now >= today_bounds[1]:
        phase, reason = "early-close", f"closed early for {_early_name(today)}"
    elif today.weekday() < 5 and next_bounds[0].date() == today:
        phase, reason = "daily-break", "daily break (17:00 to 18:00 New York)"
    else:
        phase, reason = "weekend", "weekend (opens Sunday 18:00 New York)"
    return MarketStatus(now=now, is_open=False, phase=phase, session=None, reason=reason,
                        trading_day=trading_day(now), next_open=next_bounds[0], next_close=next_bounds[1])


@dataclass(frozen=True)
class TradingWindow:
    """When new trades may start and when everything must be flat. Set from volume and time of day, not from results;
    the rules are in London and New York time so a backtest means the same thing all year."""
    entry_start: dt.time = dt.time(8, 0)    # London
    entry_end: dt.time = dt.time(11, 0)     # New York
    flat_by: dt.time = dt.time(13, 0)       # New York


@dataclass(frozen=True)
class DayWindow:
    entry_start: dt.datetime
    entry_end: dt.datetime
    flat_by: dt.datetime


def day_window(day: dt.date, window: TradingWindow = TradingWindow()) -> DayWindow | None:
    """One trading day's window as real times, or None if the day does not trade. On a short day the exit moves
    earlier so it is never left to the close."""
    bounds = session_bounds(day)
    if bounds is None:
        return None
    flat_by = min(dt.datetime.combine(day, window.flat_by, NEW_YORK), bounds[1] - FLAT_BEFORE_CLOSE)
    return DayWindow(entry_start=dt.datetime.combine(day, window.entry_start, LONDON),
                     entry_end=min(dt.datetime.combine(day, window.entry_end, NEW_YORK), flat_by),
                     flat_by=flat_by)


def can_enter(when: dt.datetime, window: TradingWindow = TradingWindow()) -> tuple[bool, str | None]:
    """May a new trade start now? If not, the plain reason."""
    status = market_status(when)
    if not status.is_open:
        return False, status.reason
    w = day_window(status.trading_day, window)
    if status.now < w.entry_start:
        return False, f"entries start at {w.entry_start.astimezone(IST):%H:%M} IST"
    if status.now > w.entry_end:
        return False, f"entries ended at {w.entry_end.astimezone(IST):%H:%M} IST"
    return True, None
