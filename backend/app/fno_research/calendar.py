"""Nifty's real expiry calendar, read from the bhavcopy instead of hard-coded, and the NSE held-back cutoff.

An expiry day is a session on which an option expiring THAT day traded. This catches every rule change by
itself: Thursday weeklies, the Tuesday switch in 2025, SEBI's one-weekly-per-exchange rule from Nov 2024,
and expiries moved a day earlier for a holiday.

    cal = load(last_date=CUTOFF - 1 day)        # research: never reads a day on or after the cutoff
    e = cal.next_expiry(d)                       # first expiry on or after d
    entry = cal.sessions_before(e, 2)            # the session 2 trading days before expiry e

The cutoff is the NSE research's own: the first held-back session is the session at 70% of the NIFTY 5-minute
bar store's sessions up to 2026-11-11 (Kite history from 2021-01-01, app.market.backfill), which is 2025-02-05.
It is pinned here, like the swing cutoff, so new data never moves it.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.fno_research import bhavcopy

CUTOFF = dt.date(2025, 2, 5)          # first held-back session for NSE research: never move it
CUTOFF_STORE_END = "2026-11-11"       # the bar-store sessions the 70% split was taken over
IN_SAMPLE_SHARE = 0.7
INDEX_FILE = "index.json"


def nse_cutoff_from_store() -> str:
    """Recompute the cutoff from the NIFTY 5-minute bar store (for the check that it has not moved)."""
    from app.core.calendar import IST
    from app.market import store
    sessions = sorted({dt.datetime.fromtimestamp(b.t, IST).date().isoformat() for b in store.read("NIFTY", "5m")})
    sessions = [s for s in sessions if s <= CUTOFF_STORE_END]
    return sessions[int(len(sessions) * IN_SAMPLE_SHARE)]


class HeldBackError(Exception):
    """Raised when research code asks for a day it must not see."""


@dataclass
class Calendar:
    trading_days: list[dt.date]
    expiries: list[dt.date]
    listed: dict[dt.date, list[dt.date]] = field(default_factory=dict)   # day -> option expiries trading that day

    @classmethod
    def from_listing(cls, listed: dict[dt.date, list[dt.date]]) -> Calendar:
        days = sorted(listed)
        exps = sorted(d for d in days if d in set(listed[d]))
        return cls(trading_days=days, expiries=exps, listed=listed)

    def _idx(self, d: dt.date) -> int:
        i = bisect.bisect_left(self.trading_days, d)
        if i >= len(self.trading_days) or self.trading_days[i] != d:
            raise KeyError(f"{d} is not a session in the data")
        return i

    def is_session(self, d: dt.date) -> bool:
        i = bisect.bisect_left(self.trading_days, d)
        return i < len(self.trading_days) and self.trading_days[i] == d

    def next_expiry(self, d: dt.date, after: bool = False) -> dt.date | None:
        """The first expiry on (or, with after=True, strictly after) d that the data has seen happen."""
        i = (bisect.bisect_right if after else bisect.bisect_left)(self.expiries, d)
        return self.expiries[i] if i < len(self.expiries) else None

    def previous_expiry(self, d: dt.date) -> dt.date | None:
        i = bisect.bisect_left(self.expiries, d)
        return self.expiries[i - 1] if i > 0 else None

    def sessions_before(self, d: dt.date, n: int) -> dt.date | None:
        """The session n trading days before session d (n=0 is d itself)."""
        i = self._idx(d) - n
        return self.trading_days[i] if i >= 0 else None

    def sessions_between(self, a: dt.date, b: dt.date) -> list[dt.date]:
        """Sessions strictly after a and strictly before b."""
        i = bisect.bisect_right(self.trading_days, a)
        j = bisect.bisect_left(self.trading_days, b)
        return self.trading_days[i:j]

    def trading_days_to(self, d: dt.date, e: dt.date) -> int:
        return self._idx(e) - self._idx(d)

    def weekday_by_year(self) -> dict[int, dict[str, int]]:
        """How many expiries fell on each weekday, per year: shows the Thursday -> Tuesday move."""
        out: dict[int, Counter] = {}
        for e in self.expiries:
            out.setdefault(e.year, Counter())[e.strftime("%a")] += 1
        return {y: dict(c) for y, c in sorted(out.items())}

    def weekday_changes(self) -> list[tuple[dt.date, str]]:
        """(first expiry, weekday) each time the usual weekday changes, ignoring single holiday shifts."""
        out: list[tuple[dt.date, str]] = []
        days = [e.strftime("%a") for e in self.expiries]
        for i, e in enumerate(self.expiries):
            run = days[i:i + 3]
            if len(run) == 3 and len(set(run)) == 1 and (not out or out[-1][1] != run[0]):
                out.append((e, run[0]))
        return out


def _index_path(root: Path | None) -> Path:
    return (root or bhavcopy.ROOT) / INDEX_FILE


def build_listing(root: Path | None = None, last_date: dt.date | None = None) -> dict[dt.date, list[dt.date]]:
    """day -> option expiries trading that day, for every stored day (cached in index.json, updated as days arrive)."""
    p = _index_path(root)
    try:
        cached = json.loads(p.read_text())
    except (OSError, ValueError):
        cached = {}
    changed = False
    for d in bhavcopy.stored_days(root):
        k = d.isoformat()
        if last_date is not None and d > last_date:
            continue                     # research never opens a held-back file, even just to list expiries
        if k not in cached:
            rows = bhavcopy.read_day(d, root) or []
            cached[k] = sorted({r.expiry.isoformat() for r in rows if r.instrument == "OPT"})
            changed = True
    if changed:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(cached.items()))))
        tmp.replace(p)
    out = {}
    for k, exps in cached.items():
        d = dt.date.fromisoformat(k)
        if last_date is not None and d > last_date:
            continue
        if not bhavcopy.day_path(d, root).exists():
            continue
        out[d] = [dt.date.fromisoformat(e) for e in exps]
    return out


def load(root: Path | None = None, last_date: dt.date | None = None) -> Calendar:
    """The calendar from stored days up to `last_date` (inclusive). Expiries after it are unknown to it."""
    return Calendar.from_listing(build_listing(root, last_date))
