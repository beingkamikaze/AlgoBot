"""US releases that move gold: CPI, the jobs report and Fed decisions. A fixed list from the official schedules, so nothing
is scraped and nothing changes without an edit here.

Sources, checked 10 Nov 2026:
  - CPI: https://www.bls.gov/schedule/news_release/cpi.htm (08:30 New York)
  - Employment Situation (jobs, "NFP"): https://www.bls.gov/schedule/news_release/empsit.htm (08:30 New York)
  - FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (statement 14:00 New York on the second day,
    press conference 14:30). The Fed says each date is tentative until the meeting before it confirms it.
Add the next year's dates when the agencies publish them; `coverage_ends()` says when the list runs out.

Used for warnings on tickets and for why cards and post-mortems. It is not a trading rule: a no-entry window for strategies
would make a new strategy version (plan section 4) and is not applied here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.global_markets.clock import IST, NEW_YORK

WARN_BEFORE = dt.timedelta(minutes=30)
WARN_AFTER = dt.timedelta(minutes=15)

_CPI = ["2026-11-10", "2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12", "2027-06-10",
        "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"]
_JOBS = ["2026-11-06", "2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02", "2027-05-07", "2027-06-04",
         "2027-07-02", "2027-08-06", "2027-09-03", "2027-10-08", "2027-11-05", "2027-12-03"]
# the second (decision) day of each meeting
_FOMC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
         "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"]


@dataclass(frozen=True)
class Event:
    name: str
    at: dt.datetime                  # aware
    note: str

    def to_dict(self, now: dt.datetime | None = None) -> dict:
        d = {"name": self.name, "at": self.at.astimezone(dt.timezone.utc).isoformat(),
             "ist": self.at.astimezone(IST).strftime("%a %d %b %H:%M IST"),
             "new_york": self.at.astimezone(NEW_YORK).strftime("%H:%M New York"), "note": self.note}
        if now is not None:
            d["minutes_away"] = round((self.at - now).total_seconds() / 60)
        return d


def _at(day: str, h: int, m: int) -> dt.datetime:
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(h, m), NEW_YORK)


EVENTS: list[Event] = sorted(
    [Event("US CPI (inflation)", _at(d, 8, 30), "inflation figures often move gold sharply in the first minutes") for d in _CPI]
    + [Event("US jobs report (NFP)", _at(d, 8, 30), "the monthly jobs report often moves gold sharply in the first minutes")
       for d in _JOBS]
    + [Event("Fed rate decision (FOMC)", _at(d, 14, 0), "statement at 14:00 and press conference at 14:30 New York; "
             "after this app's flat-by time, but it can move the next session's open") for d in _FOMC],
    key=lambda e: e.at)


def coverage_ends() -> dt.date:
    return min(dt.date.fromisoformat(x[-1]) for x in (_CPI, _JOBS))


def between(start: dt.datetime, end: dt.datetime) -> list[Event]:
    return [e for e in EVENTS if start <= e.at <= end]


def upcoming(now: dt.datetime, days: int = 14) -> list[Event]:
    return between(now - WARN_AFTER, now + dt.timedelta(days=days))


def near(now: dt.datetime) -> list[Event]:
    """Releases from 30 minutes ahead to 15 minutes ago: a ticket now trades into (or right after) the jump."""
    return between(now - WARN_AFTER, now + WARN_BEFORE)


def warnings_for(now: dt.datetime, until: dt.datetime | None = None) -> list[str]:
    """Plain warnings for a ticket made at `now` that may stay open until `until` (its flat-by time)."""
    out = []
    for e in near(now):
        mins = round((e.at - now).total_seconds() / 60)
        when = f"in {mins} minutes" if mins > 0 else ("now" if mins == 0 else f"{-mins} minutes ago")
        out.append(f"{e.name} at {e.at.astimezone(IST):%H:%M} IST ({when}): price can jump past your stop")
    if until is not None:
        for e in between(now + WARN_BEFORE, until):
            out.append(f"{e.name} at {e.at.astimezone(IST):%H:%M} IST comes while this trade can still be open")
    if now.date() > coverage_ends():
        out.append("the US release calendar in this app has run out: add the new dates in global_markets/macro.py")
    return out
