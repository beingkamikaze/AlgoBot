"""Live Nifty option-chain recorder: real intraday option prices for later research.

Kite keeps no history of expired options and the bhavcopy is end-of-day only, so this builds our own: every
5 minutes while NSE is open (app.core.calendar), one snapshot of the NIFTY chain for the nearest two expiries,
every strike within 10% of spot, with bid, ask, last price, NSE's IV, OI and volume, plus spot, the forward
read from put-call parity and the near-month future (only when Kite is connected; NSE's public API has none).

Each day is one file, backend/data/chain_snapshots/YYYY-MM-DD.jsonl.gz, one JSON line per snapshot (appended as
separate gzip members, which every gzip reader joins). Nothing goes to Supabase, so no migration is needed.

Only real chains are stored (NSE's public chain, or Kite's when you are logged in today; each expiry records its
`source`, and `nse_ts` then holds that source's quote time): the app's modeled fallback is never recorded. A failure is logged and the slot is
skipped; when NSE refuses (HTTP 401/403/429) the recorder waits longer before asking again, up to 30 minutes.
Turn it off with CHAIN_RECORDER_ENABLED=false in backend/.env.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import threading
from pathlib import Path

from app.core.calendar import IST, market_status

log = logging.getLogger("fno.recorder")

ROOT = Path(__file__).resolve().parents[2] / "data" / "chain_snapshots"
UNDERLYING = "NIFTY"
EVERY = 300                   # seconds between snapshots
BAND = 0.10                   # strikes within ±10% of spot
EXPIRIES = 2
MAX_BACKOFF = 1800

state: dict = {"running": False, "last_snapshot": None, "last_error": None, "backoff": 0}


def should_snapshot(now: dt.datetime) -> bool:
    """Only during the NSE session (9:15 to 15:30 IST on trading days)."""
    return market_status(now).is_open


def next_slot(now: dt.datetime, every: int = EVERY) -> dt.datetime:
    """The next wall-clock multiple of `every` seconds (9:15, 9:20, ...), strictly after now."""
    now = now.astimezone(IST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    secs = (now - midnight).total_seconds()
    return midnight + dt.timedelta(seconds=(int(secs // every) + 1) * every)


def day_path(d: dt.date, root: Path | None = None) -> Path:
    return (root or ROOT) / f"{d.isoformat()}.jsonl.gz"


def _future_price() -> float | None:
    """The near-month NIFTY future from Kite when you are logged in today; None otherwise."""
    try:
        from app.core.instruments import resolve
        from app.live import session
        b = session.any_broker()
        if b is None:
            return None
        key = f"NFO:{resolve('NIFTY-FUT').tradingsymbol}"
        return b.ltp([key]).get(key)
    except Exception:  # noqa: BLE001 - the future is a nice-to-have
        return None


def snapshot(now: dt.datetime, fetch_expiries=None, fetch_chain=None, future=_future_price) -> dict:
    """One snapshot as a plain dict. Raises chain.ChainError when NSE does not answer with a real chain."""
    from app.options import chain as ch
    fetch_expiries = fetch_expiries or ch.expiries
    fetch_chain = fetch_chain or (lambda u, e: ch.chain(u, e, allow_modeled=False))
    today = now.astimezone(IST).date()
    exps = [e for e in fetch_expiries(UNDERLYING) if e >= today][:EXPIRIES]
    if not exps:
        raise ch.ChainError("NSE listed no current expiries")
    out = {"ts": now.astimezone(IST).isoformat(timespec="seconds"), "underlying": UNDERLYING, "spot": None,
           "future": future(), "expiries": []}
    for e in exps:
        c = fetch_chain(UNDERLYING, e)
        if c.source == "modeled":
            raise ch.ChainError("no real chain (only a modeled one): not recorded")
        lo, hi = c.spot * (1 - BAND), c.spot * (1 + BAND)
        rows = []
        for k in c.strikes():
            if not lo <= k <= hi:
                continue
            for kind, q in sorted(c.rows[k].items()):
                rows.append({"k": k, "t": kind, "bid": q.bid, "ask": q.ask, "ltp": q.ltp, "iv": q.iv, "oi": q.oi,
                             "vol": q.volume})
        out["spot"] = out["spot"] or c.spot
        out["expiries"].append({"expiry": e.isoformat(), "source": c.source, "nse_ts": c.as_of, "spot": c.spot,
                                "forward": c.forward(now), "rows": rows})
    return out


def append(snap: dict, root: Path | None = None) -> Path:
    d = dt.datetime.fromisoformat(snap["ts"]).date()
    p = day_path(d, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "at") as fh:
        fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
    return p


def read_day(d: dt.date, root: Path | None = None) -> list[dict]:
    p = day_path(d, root)
    if not p.exists():
        return []
    out = []
    with gzip.open(p, "rt") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue            # a line cut short by a crash: skip it
    return out


def status(now: dt.datetime | None = None, root: Path | None = None) -> dict:
    """Snapshots stored today and the last one's time (for the status line)."""
    from app.config import get_settings
    now = (now or dt.datetime.now(IST)).astimezone(IST)
    snaps = read_day(now.date(), root)
    days = sorted(p.name[:10] for p in (root or ROOT).glob("*.jsonl.gz")) if (root or ROOT).exists() else []
    return {"enabled": get_settings().chain_recorder_enabled, "running": state["running"],
            "today": len(snaps), "last_snapshot": snaps[-1]["ts"] if snaps else state["last_snapshot"],
            "days_recorded": len(days), "first_day": days[0] if days else None,
            "market_open": should_snapshot(now), "last_error": state["last_error"]}


def refused(e: Exception) -> bool:
    text = str(e)
    return any(f"HTTP {c}" in text for c in (401, 403, 429))


def tick(now: dt.datetime, root: Path | None = None, snap=snapshot) -> str:
    """One slot: 'closed', 'stored' or 'failed'. Never raises."""
    if not should_snapshot(now):
        return "closed"
    try:
        s = snap(now)
        append(s, root)
        state.update(last_snapshot=s["ts"], last_error=None, backoff=0)
        return "stored"
    except Exception as e:  # noqa: BLE001 - a failed slot is skipped, the server goes on
        log.warning("chain snapshot failed: %s", e)
        state["last_error"] = f"{now.astimezone(IST):%H:%M}: {str(e)[:200]}"
        if refused(e):
            state["backoff"] = min(max(state["backoff"] * 2, EVERY * 2), MAX_BACKOFF)
        return "failed"


class Recorder:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="chain-recorder", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        state["running"] = True
        try:
            while not self._stop.is_set():
                now = dt.datetime.now(IST)
                if not should_snapshot(now):
                    st = market_status(now)
                    wait = (st.next_open - now).total_seconds() if st.next_open > now else EVERY
                    self._stop.wait(min(max(wait, 5), 3600))
                    continue
                tick(now)
                extra = state["backoff"]
                wait = (next_slot(dt.datetime.now(IST)) - dt.datetime.now(IST)).total_seconds() + extra
                self._stop.wait(max(wait, 1))
        finally:
            state["running"] = False
