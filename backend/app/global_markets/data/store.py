"""Local spot history: backend/data/global/dukascopy/<SYMBOL>/<YYYY-MM>.npz (1-minute bid and ask) plus manifest.json, which
says what happened to each day so a download can resume. Only spot goes here: futures never mix with it."""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path

import numpy as np

from app.global_markets.data.bars import Ohlcv
from app.global_markets.data.dukascopy import COLUMNS

ROOT = Path(__file__).resolve().parents[3] / "data" / "global"
_lock = threading.Lock()


def _dir(symbol: str, root: Path | None = None) -> Path:
    return (root or ROOT) / "dukascopy" / symbol


def _replace(path: Path, write) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        write(fh)
    tmp.replace(path)


def manifest(symbol: str, root: Path | None = None) -> dict[str, str]:
    """{"2026-10-01": "ok" | "empty" | "bad"}"""
    p = _dir(symbol, root) / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_day(symbol: str, day: dt.date, columns: dict[str, np.ndarray] | None, status: str = "ok",
             root: Path | None = None) -> None:
    """Keeps one day: its bars (if any) replace any earlier copy, then the manifest records it."""
    d = _dir(symbol, root)
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        if columns is not None:
            month = d / f"{day:%Y-%m}.npz"
            start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
            merged = columns
            if month.exists():
                with np.load(month) as old:
                    keep = (old["t"] < start) | (old["t"] >= start + 86400)
                    merged = {k: np.concatenate([old[k][keep], columns[k]]) for k in COLUMNS}
            order = np.argsort(merged["t"])
            sorted_cols = {k: merged[k][order] for k in COLUMNS}
            _replace(month, lambda fh: np.savez_compressed(fh, **sorted_cols))
        days = manifest(symbol, root)
        days[day.isoformat()] = status if columns is not None or status != "ok" else "empty"
        _replace(d / "manifest.json", lambda fh: fh.write(json.dumps(days, sort_keys=True).encode()))


def read_columns(symbol: str, since: int = 0, root: Path | None = None) -> dict[str, np.ndarray] | None:
    """All stored 1-minute columns from unix time `since` on, or None if there are none."""
    d = _dir(symbol, root)
    if not d.exists():
        return None
    since_month = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m")
    parts = []
    for p in sorted(d.glob("*.npz")):
        if p.stem >= since_month:
            with np.load(p) as f:
                keep = f["t"] >= since
                parts.append({k: f[k][keep] for k in COLUMNS})
    parts = [p for p in parts if len(p["t"])]
    if not parts:
        return None
    return {k: np.concatenate([p[k] for p in parts]) for k in COLUMNS}


def mid(columns: dict[str, np.ndarray]) -> Ohlcv:
    """Halfway between bid and ask: what the chart shows. The backtester fills on the real bid and ask instead."""
    return Ohlcv(t=columns["t"], o=(columns["bo"] + columns["ao"]) / 2, h=(columns["bh"] + columns["ah"]) / 2,
                 l=(columns["bl"] + columns["al"]) / 2, c=(columns["bc"] + columns["ac"]) / 2, v=columns["v"])
