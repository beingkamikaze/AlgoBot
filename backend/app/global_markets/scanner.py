"""Live signals for gold: the three strategies, at the settings fixed on 7 Nov 2026, run on live closed bars.

You chose this on 10 Nov 2026 knowing the research rule (no strategy on prices after the held-back cutoff outside the one Lab
test). To keep the Lab test honest: settings are never changed at scan time, and live signals and the paper trades taken
from them are never used to choose settings. They are only for you to act on, by hand.

A signal never trades. It shows on the Signals page with its why card; "Make ticket" turns it into an ordinary paper ticket
that still needs the risk check and your confirmation.

Timing follows the backtest where it can: only closed bars, signal bars inside the entry window (08:00 London to 11:00 New
York), at most `max_trades_per_day` signals per strategy a day. The backtest entered within 5 minutes of the signal bar's
close; `GC=F` arrives about 10 minutes late, so a futures signal is already older than that when it is seen. Each signal says
how old it was when seen, and gives you 5 minutes from then to decide.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import numpy as np

from app.db import db, eq
from app.domain.signal import Evidence, grade_confidence
from app.global_markets import research, signals
from app.global_markets.backtest import BacktestConfig
from app.global_markets.clock import TradingWindow, day_window, market_status, trading_day
from app.global_markets.data import feed
from app.global_markets.data.bars import TF_SECONDS, Ohlcv
from app.global_markets.instruments import resolve
from app.global_markets.risk import Limits, max_lots
from app.global_markets.strategies.base import Frame
from app.global_markets.strategies.library import STRATEGIES, make

log = logging.getLogger("global.scanner")
CFG = BacktestConfig()
DECIDE_SECONDS = CFG.max_entry_delay        # how long you have after a signal is seen
LOOKBACK_BARS = 3                           # closed bars checked each pass, in case a pass was missed
SYMBOLS = ("XAUUSD",)


def frame_from(symbol: str, tf: str, bars: list) -> Frame:
    done = [b for b in bars if b.complete]
    arr = lambda k: np.array([getattr(b, k) for b in done], dtype=float)  # noqa: E731
    return Frame(symbol, tf, Ohlcv(np.array([b.t for b in done], dtype=np.int64), arr("o"), arr("h"), arr("l"), arr("c"),
                                   arr("v")))


def evidence_for(strategy_id: str) -> tuple[list[Evidence], dict | None]:
    """The latest research-days report (never the held-back days): the confidence rule's input."""
    r = research.latest().get(strategy_id)
    if not r:
        return [], None
    s = r.get("summary") or {}
    ev = [Evidence(source="backtest", trades=int(s.get("trades") or 0), expectancy_r=s.get("expectancy_r"),
                   profit_factor=s.get("profit_factor"), win_rate=s.get("win_rate"))]
    return ev, r


def _confidence_text(grade: str, ev: list[Evidence], report: dict | None) -> str:
    if not ev:
        return "No backtest yet: no track record."
    e = ev[0]
    verdict = ((report or {}).get("verdict") or {}).get("text") or ""
    exp = "–" if e.expectancy_r is None else f"{e.expectancy_r:+.2f}R"
    pf = "" if e.profit_factor is None else f", profit factor {e.profit_factor:.2f}"
    return (f"Fixed rule on the research-days backtest: {e.trades} trades, {exp} a trade after costs{pf}. {verdict}").strip()


def _signals_today(d, strategy_id: str, symbol: str, day: dt.date) -> int:
    rows = d.select("global_signals", {"strategy_id": eq(strategy_id), "symbol": eq(symbol)}, order="signal_ts.desc", limit=10)
    return sum(1 for r in rows if trading_day(dt.datetime.fromisoformat(r["signal_ts"])) == day)


def scan(now: dt.datetime | None = None, symbol: str = "XAUUSD", bars_by_tf: dict | None = None) -> list[dict]:
    """One pass: new signals from bars that closed inside the entry window. Returns the rows it stored."""
    now = now or dt.datetime.now(dt.timezone.utc)
    m = market_status(now)
    if not m.is_open:
        return []
    w = day_window(m.trading_day, TradingWindow())
    if w is None or now < w.entry_start:
        return []
    inst = resolve(symbol)
    source = feed.default_source()
    d = db()
    stored: list[dict] = []
    frames: dict[str, Frame] = {}
    for sid, cls in STRATEGIES.items():
        strategy = make(sid)                                    # the fixed settings: never tuned here
        tf = strategy.timeframe
        if tf not in frames:
            raw = (bars_by_tf or {}).get(tf)
            frames[tf] = frame_from(symbol, tf, raw if raw is not None else feed.series(symbol, tf, source).bars)
        f = frames[tf]
        if not len(f):
            continue
        secs = TF_SECONDS[tf]
        for i in range(max(0, len(f) - LOOKBACK_BARS), len(f)):
            close_t = int(f.t[i]) + secs
            closed = dt.datetime.fromtimestamp(close_t, dt.timezone.utc)
            if not (w.entry_start <= closed <= w.entry_end) or str(f.day[i]) != m.trading_day.isoformat():
                continue
            cand = strategy.check(f, i)
            if cand is None:
                continue
            if d.select("global_signals", {"strategy_id": eq(sid), "symbol": eq(symbol), "signal_ts": eq(closed.isoformat())}):
                continue
            if _signals_today(d, sid, symbol, m.trading_day) >= CFG.max_trades_per_day:
                continue
            ref = float(f.c[i])
            risk = abs(ref - cand.stop)
            target = round(ref + cand.target_r * risk if cand.side == "buy" else ref - cand.target_r * risk, 2)
            ev, report = evidence_for(sid)
            grade = grade_confidence(ev).value
            lots = max_lots(inst, ref, cand.stop, Limits())
            age = max(0, int(now.timestamp()) - close_t)
            warnings = []
            if age > CFG.max_entry_delay:
                warnings.append(f"seen {age // 60} minutes after the signal bar closed: the backtest entered within "
                                f"{CFG.max_entry_delay // 60} minutes, so taking it now is a later entry than was tested")
            if source == "futures":
                warnings.append("found on COMEX futures (GC=F), not spot: levels differ from XAUUSD")
            card = signals.card(symbol=symbol, side=cand.side, lots=lots, units=inst.units(lots) if lots else 0.0, limit=ref,
                                stop=cand.stop, target=target, why=cand.rule, max_loss=None, source=source,
                                bars=feed.series(symbol, "1m", source).bars if bars_by_tf is None else bars_by_tf.get("1m", []),
                                now=now, flat_by=w.flat_by, headlines=None, warnings=warnings, strategy=cls.name,
                                confidence={"grade": grade, "why": _confidence_text(grade, ev, report)})
            row = {"symbol": symbol, "strategy_id": sid, "strategy_version": cls.version, "side": cand.side, "timeframe": tf,
                   "signal_ts": closed.isoformat(), "price_source": source, "ref_price": ref, "stop": cand.stop,
                   "target_r": cand.target_r, "state": "new",
                   "expires_at": dt.datetime.fromtimestamp(int(now.timestamp()) + DECIDE_SECONDS, dt.timezone.utc).isoformat(),
                   "explanation": {"rule": cand.rule, "conditions": [c.model_dump() for c in cand.conditions],
                                   "invalidation": cand.invalidation, "stop_reason": cand.stop_reason,
                                   "target_reason": f"{cand.target_r:g}R from the entry", "features": cand.features,
                                   "evidence": [e.model_dump() for e in ev], "confidence": grade,
                                   "verdict": ((report or {}).get("verdict") or {}).get("text"),
                                   "age_when_seen_seconds": age, "card": card}}
            stored.append(d.insert("global_signals", row)[0])
            log.info("signal %s %s %s at %s", sid, cand.side, symbol, closed.isoformat())
    return stored


def expire(now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    d = db()
    old = d.select("global_signals", {"state": eq("new"), "expires_at": f"lt.{now.isoformat()}"})
    for r in old:
        d.update("global_signals", {"id": eq(r["id"])}, {"state": "expired"})
    return len(old)


def recent(symbol: str = "XAUUSD", limit: int = 30) -> list[dict]:
    return db().select("global_signals", {"symbol": eq(symbol)}, order="signal_ts.desc", limit=limit)


def get(signal_id: str) -> dict | None:
    rows = db().select("global_signals", {"id": eq(signal_id)})
    return rows[0] if rows else None


# ---------------------------------------------------------------- background loop

scan_state: dict = {}


class ScanLoop:
    """Checks for a newly closed 5-minute bar about every 30 s, only inside the entry window. Feed caching keeps it to roughly
    one price call per bar."""

    def __init__(self, every: float = 30.0):
        self.every, self._stop = every, threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="global-scan-loop")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        last_error = None
        while not self._stop.is_set():
            try:
                expired = expire()
                found = []
                for symbol in SYMBOLS:
                    found += scan(symbol=symbol)
                scan_state.clear()
                scan_state.update(at=time.time(), found=len(found), expired=expired)
                last_error = None
            except Exception as e:  # noqa: BLE001 - keep scanning; say why on the page
                msg = str(e)[:300]
                if msg != last_error:
                    log.warning("global scan failed: %s", msg)
                last_error = msg
                scan_state.update(at=time.time(), error=msg)
            self._stop.wait(self.every)
