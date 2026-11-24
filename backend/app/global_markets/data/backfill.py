"""Downloads spot history slowly in the background, one file at a time. It resumes where it stopped,
and backs off when the server is busy, so it can take many hours and may be stopped at any moment.

Started by the app (GLOBAL_BACKFILL_ENABLED), or by hand: python -m app.global_markets.data.backfill --days 5
Only one process downloads at a time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import logging
import threading
from pathlib import Path

from app.global_markets.clock import is_trading_day
from app.global_markets.data import dukascopy, store
from app.global_markets.research_window import HELD_BACK_FROM

log = logging.getLogger("global.backfill")
PAUSE = 3.0                 # seconds between days
FIRST_BACKOFF = 30.0
MAX_BACKOFF = 900.0
state: dict = {"running": False, "last_error": None, "backoff_seconds": 0, "current_day": None}


def pending_days(symbol: str, start: dt.date, today: dt.date, root: Path | None = None) -> list[dt.date]:
    """Days not tried yet: research days first, then held-back days, newest first within each, because research cannot start
    without research days. Today is left out (its file is not finished), and so are Saturdays (UTC): the market is shut
    all of that day. Sundays stay in, for the evening open."""
    done = store.manifest(symbol, root)
    out, d = [], today - dt.timedelta(days=1)
    while d >= start:
        if d.weekday() != 5 and d.isoformat() not in done:
            out.append(d)
        d -= dt.timedelta(days=1)
    return [d for d in out if d < HELD_BACK_FROM] + [d for d in out if d >= HELD_BACK_FROM]


def run(symbol: str, start: dt.date, stop: threading.Event, fetch=dukascopy.fetch_day, today=None, max_days: int | None = None,
        root: Path | None = None) -> int:
    """Works through the pending days until they run out or `stop` is set; returns how many days it finished."""
    finished, backoff = 0, FIRST_BACKOFF
    for day in pending_days(symbol, start, today or dt.datetime.now(dt.timezone.utc).date(), root):
        if max_days is not None and finished >= max_days:
            break
        state["current_day"] = day.isoformat()
        while not stop.is_set():
            try:
                store.save_day(symbol, day, fetch(symbol, day), root=root)
                state.update(last_error=None, backoff_seconds=0)
                backoff = FIRST_BACKOFF
                finished += 1
                stop.wait(PAUSE)
                break
            except dukascopy.Busy as e:
                state.update(last_error=f"{day}: {e}", backoff_seconds=backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except dukascopy.BadFile as e:      # will not improve by asking again: recorded, and shown as a gap
                log.warning("%s %s: %s", symbol, day, e)
                state["last_error"] = f"{day}: {e}"
                store.save_day(symbol, day, None, status="bad", root=root)
                finished += 1
                break
        if stop.is_set():
            break
    state["current_day"] = None
    return finished


def _acquire(root: Path | None = None):
    """A lock the operating system frees when the process ends, however it ends. None if another process holds it."""
    base = root or store.ROOT
    base.mkdir(parents=True, exist_ok=True)
    fh = open(base / ".backfill.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


class BackfillThread:
    """Keeps history complete while the app runs: works through what is pending, then checks once an hour for new days."""

    def __init__(self, symbol: str, start: dt.date) -> None:
        self.symbol, self.start = symbol, start
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="global-backfill", daemon=True)

    def start_thread(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        lock = _acquire()
        while lock is None:
            state["last_error"] = "another process is already downloading; checking again in 5 minutes"
            if self._stop.wait(300):
                return
            lock = _acquire()
        state.update(running=True, last_error=None)
        try:
            while not self._stop.is_set():
                try:
                    run(self.symbol, self.start, self._stop)
                except Exception:  # noqa: BLE001 - a bad day must not end the download for good
                    log.exception("backfill failed")
                    state["last_error"] = "stopped by an error, see the backend log; retrying in an hour"
                self._stop.wait(3600)
        finally:
            state["running"] = False
            lock.close()


def coverage(symbol: str, start: dt.date, today: dt.date | None = None, root: Path | None = None) -> dict:
    """How much history there really is, in plain counts. A trading day with no bars is a gap."""
    days = store.manifest(symbol, root)
    ok = sorted(d for d, s in days.items() if s == "ok")
    gaps = sorted(d for d, s in days.items() if s != "ok" and is_trading_day(dt.date.fromisoformat(d)))
    today = today or dt.datetime.now(dt.timezone.utc).date()
    left = len(pending_days(symbol, start, today, root))
    trading_ok = sum(is_trading_day(dt.date.fromisoformat(d)) for d in ok)
    return {"days_with_bars": len(ok), "trading_days_with_bars": trading_ok, "first_day": ok[0] if ok else None,
            "last_day": ok[-1] if ok else None, "gap_days": len(gaps), "gap_examples": gaps[-5:], "days_left": left,
            "start_target": start.isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Dukascopy history for the global setup")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--start", default="2021-01-01", type=dt.date.fromisoformat)
    parser.add_argument("--days", type=int, default=None, help="stop after this many days")
    args = parser.parse_args()
    lock = _acquire()
    if lock is None:
        raise SystemExit("another process is already downloading")
    n = run(args.symbol, args.start, threading.Event(), max_days=args.days)
    print(f"finished {n} days")
    print(coverage(args.symbol, args.start))
    lock.close()


if __name__ == "__main__":
    main()
