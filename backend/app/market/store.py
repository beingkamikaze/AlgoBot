"""Local bar store: every completed bar we download or build from Kite ticks is kept on disk, so history grows
beyond any provider's limits. `history()` tops it up from the best source right now (app.market.feed: Kite live
ticks, else Kite's historical API, else Yahoo) and says which one it used.

backend/data/bars/<TF>/<SYMBOL>.json.gz = {"t": [...], "o": [...], ...}, sorted by time.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import threading
from pathlib import Path

from app.market.models import TIMEFRAMES, Bar, Candles
from app.market.symbols import normalize

ROOT = Path(__file__).resolve().parents[2] / "data" / "bars"
_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def _lock(key: str) -> threading.RLock:
    with _guard:
        return _locks.setdefault(key, threading.RLock())


@contextlib.contextmanager
def locked(symbol: str, tf: str):
    """Hold while reading, merging and writing one file, so two writers never lose each other's bars."""
    with _lock(f"{normalize(symbol)}|{tf}"):
        yield


def path(symbol: str, tf: str) -> Path:
    return _path(symbol, tf)


def _path(symbol: str, tf: str) -> Path:
    safe = normalize(symbol).replace("&", "_AND_").replace("/", "_")
    return ROOT / tf / f"{safe}.json.gz"


def read(symbol: str, tf: str) -> list[Bar]:
    p = _path(symbol, tf)
    if not p.exists():
        return []
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    return [Bar(t=t, o=o, h=h, l=lo, c=c, v=v, complete=True)
            for t, o, h, lo, c, v in zip(d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])]


def write(symbol: str, tf: str, bars: list[Bar]) -> None:
    p = _path(symbol, tf)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {k: [getattr(b, k) for b in bars] for k in ("t", "o", "h", "l", "c", "v")}
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(d, fh, separators=(",", ":"))
    tmp.replace(p)


def merge(old: list[Bar], new: list[Bar]) -> list[Bar]:
    by_t = {b.t: b for b in old}
    by_t.update({b.t: b for b in new if b.complete})
    return [by_t[t] for t in sorted(by_t)]


def history(symbol: str, tf: str, refresh: bool = True) -> Candles:
    """All stored completed bars, topped up from the best source's longest range (`source` says which)."""
    from app.market import feed
    key = f"{normalize(symbol)}|{tf}"
    with _lock(key):
        stored = read(symbol, tf)
        fresh: Candles | None = None
        if refresh:
            fresh = feed.candles(symbol, tf, TIMEFRAMES[tf][3])
            stored = merge(stored, fresh.bars)
            write(symbol, tf, stored)
        base = fresh or Candles(symbol=normalize(symbol), provider_symbol="", name=None, tf=tf)
        return Candles(symbol=base.symbol, provider_symbol=base.provider_symbol, name=base.name, tf=tf,
                       bars=stored, prev_close=base.prev_close, last_price=base.last_price, as_of=base.as_of,
                       source=base.source if fresh else "store", delayed=base.delayed if fresh else True)
