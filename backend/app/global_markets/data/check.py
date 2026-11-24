"""Do two sources tell the same story? Compares stored spot with `GC=F` on 5-minute bars. Futures sit at a different price,
so the test is that they move together and that the gap between them stays steady. A pass means the history is not
garbled; it says nothing about whether a strategy works."""

from __future__ import annotations

import time

import numpy as np

from app.market.models import Bar

MIN_COMMON_BARS = 500
MIN_CORRELATION = 0.9
MAX_GAP_SWING = 40.0        # dollars, between the 5th and 95th percentile of the gap, over about 60 days


def compare(spot: list[Bar], other: list[Bar], seconds: int = 300) -> dict:
    a = {b.t: b.c for b in spot if b.complete}
    b = {x.t: x.c for x in other if x.complete}
    common = sorted(a.keys() & b.keys())
    if len(common) < MIN_COMMON_BARS:
        return {"ok": None, "common_bars": len(common), "detail": f"too few bars in common to judge (need {MIN_COMMON_BARS})"}
    t = np.array(common)
    x, y = np.array([a[k] for k in common]), np.array([b[k] for k in common])
    next_bar = t[1:] - t[:-1] == seconds    # returns only across neighbouring bars: gaps are not moves
    rx, ry = np.diff(x)[next_bar], np.diff(y)[next_bar]
    corr = float(np.corrcoef(rx, ry)[0, 1])
    gap = y - x
    p5, p50, p95 = (float(v) for v in np.percentile(gap, [5, 50, 95]))
    ok = corr >= MIN_CORRELATION and p95 - p5 <= MAX_GAP_SWING
    return {"ok": ok, "common_bars": len(common), "first": int(t[0]), "last": int(t[-1]),
            "move_correlation": round(corr, 3), "median_gap": round(p50, 2), "gap_swing": round(p95 - p5, 2),
            "detail": "the two sources move together" if ok else "the two sources disagree: do not trust this history yet"}


def compare_now(symbol: str) -> dict:
    from app.global_markets.data import bars as bars_mod, store, yahoo_futures
    now = time.time()
    columns = store.read_columns(symbol, int(now) - 60 * 86400)
    spot = bars_mod.to_bars(bars_mod.resample(store.mid(columns), "5m"), "5m", now) if columns else []
    return compare(spot, yahoo_futures.fetch(symbol, "5m"))
