"""NSE market clock in IST. Ported from the nse-alerts / nse-risk skills.

India has no daylight saving time, so IST is always UTC+5:30. The holiday list
is NSE's capital-market list for 2026; later years are loaded from the database
(Phase 1 syncs NSE's own holiday master) through `add_holidays`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)
PRE_OPEN = dt.time(9, 0)

NSE_HOLIDAYS: dict[str, str] = {  # capital market segment, NSE holiday master (read 2026-10-23)
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}


def add_holidays(extra: dict[str, str]) -> None:
    NSE_HOLIDAYS.update(extra)


def known_years() -> set[int]:
    return {int(d[:4]) for d in NSE_HOLIDAYS}


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def is_trading_day(d: dt.date) -> tuple[bool, str | None]:
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, f"NSE holiday: {name}"
    return True, None


def next_trading_day(d: dt.date) -> dt.date:
    n = d + dt.timedelta(days=1)
    while not is_trading_day(n)[0]:
        n += dt.timedelta(days=1)
    return n


@dataclass(frozen=True)
class MarketStatus:
    now: dt.datetime
    is_open: bool
    phase: str            # "closed", "pre-open", "open", "post-close"
    reason: str | None
    next_open: dt.datetime
    calendar_known: bool


def market_status(when: dt.datetime | None = None) -> MarketStatus:
    now = (when or now_ist()).astimezone(IST)
    d, t = now.date(), now.time()
    ok, why = is_trading_day(d)
    if ok and t < PRE_OPEN:
        phase, reason, nxt = "closed", "before the pre-open (9:00)", d
    elif ok and t < OPEN:
        phase, reason, nxt = "pre-open", "pre-open session (9:00 to 9:15)", d
    elif ok and t < CLOSE:
        phase, reason, nxt = "open", None, None
    elif ok:
        phase, reason, nxt = "post-close", "after the close (15:30)", next_trading_day(d)
    else:
        phase, reason, nxt = "closed", why, next_trading_day(d)
    next_open = dt.datetime.combine(nxt or next_trading_day(d), OPEN, IST)
    return MarketStatus(now=now, is_open=phase == "open", phase=phase, reason=reason,
                        next_open=next_open, calendar_known=d.year in known_years())
