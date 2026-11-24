"""Option chains from Kite Connect: real-time bid, ask, last price, open interest, volume and five levels of depth.

Contracts come from Kite's NFO instruments dump (downloaded once a day, app.market.kite). Only strikes within
BAND (±10%) of spot are quoted, in batched `quote` calls (500 instruments a call, 1 call a second). IV and Greeks
are ours: Black-76 at the bid-ask mid, off the forward read from put-call parity (app.options.pricing).

The result is the same `Chain` that the NSE scrape returns (source "kite"), so every caller works unchanged.
Kite does not give the change in open interest since yesterday: `chg_oi` is None on Kite chains.
"""

from __future__ import annotations

import datetime as dt

from app.core.calendar import IST
from app.market import kite as K
from app.market import ticks
from app.options import pricing

BAND = 0.10


class KiteChainError(Exception):
    pass


def _contracts(kd: K.KiteData, underlying: str, today: dt.date) -> list[dict]:
    return [r for r in kd.instruments("NFO", today)
            if r.get("name") == underlying and r.get("instrument_type") in ("CE", "PE") and r.get("expiry")]


def expiries(kd: K.KiteData, underlying: str, today: dt.date | None = None) -> list[dt.date]:
    today = today or dt.datetime.now(IST).date()
    out = sorted({r["expiry"] for r in _contracts(kd, underlying, today) if r["expiry"] >= today})
    if not out:
        raise KiteChainError(f"Kite lists no {underlying} options")
    return out


def _spot_hint(kd: K.KiteData, underlying: str) -> float:
    live = ticks.builder.last_price(underlying) if ticks.builder.fresh(underlying) else None
    if live:
        return live[0]
    got = kd.ohlc([K.key_for(underlying)]).get(K.key_for(underlying)) or {}
    if not got.get("last_price"):
        raise KiteChainError(f"Kite has no price for {underlying}")
    return float(got["last_price"])


def _quote(kind: str, strike: float, q: dict):
    from app.options.chain import Quote
    depth = q.get("depth") or {}
    buy = [lv for lv in depth.get("buy") or [] if lv.get("price")]
    sell = [lv for lv in depth.get("sell") or [] if lv.get("price")]
    return Quote(kind=kind, strike=strike, ltp=float(q["last_price"]) if q.get("last_price") else None,
                 bid=float(buy[0]["price"]) if buy else None, ask=float(sell[0]["price"]) if sell else None,
                 iv=None, oi=int(q["oi"]) if q.get("oi") is not None else None, chg_oi=None,
                 volume=int(q["volume"]) if q.get("volume") is not None else None,
                 depth={"buy": [{k: lv.get(k) for k in ("price", "quantity", "orders")} for lv in depth.get("buy") or []],
                        "sell": [{k: lv.get(k) for k in ("price", "quantity", "orders")} for lv in depth.get("sell") or []]})


def build(kd: K.KiteData, underlying: str, expiry: dt.date, all_expiries: list[dt.date], now: dt.datetime | None = None):
    from app.options.chain import Chain
    now = (now or dt.datetime.now(IST)).astimezone(IST)
    rows = [r for r in _contracts(kd, underlying, now.date()) if r["expiry"] == expiry]
    if not rows:
        raise KiteChainError(f"Kite lists no {underlying} options expiring {expiry}")
    hint = _spot_hint(kd, underlying)
    lo, hi = hint * (1 - BAND), hint * (1 + BAND)
    band = [r for r in rows if lo <= float(r["strike"]) <= hi]
    spot_key = K.key_for(underlying)
    keys = [spot_key] + [f"NFO:{r['tradingsymbol']}" for r in band]
    got = kd.quote(keys)
    sq = got.get(spot_key) or {}
    spot = float(sq.get("last_price") or hint)
    stamp = sq.get("timestamp") or now
    if isinstance(stamp, dt.datetime) and stamp.tzinfo is not None:
        stamp = stamp.astimezone(IST)
    c = Chain(underlying=underlying, expiry=expiry, spot=spot,
              as_of=stamp.strftime("%d-%b-%Y %H:%M:%S") if isinstance(stamp, dt.datetime) else str(stamp),
              source="kite", expiries=all_expiries, lot=int(rows[0].get("lot_size") or 0) or None)
    for r in band:
        q = got.get(f"NFO:{r['tradingsymbol']}")
        if not q:
            continue
        k = float(r["strike"])
        c.rows.setdefault(k, {})[r["instrument_type"]] = _quote(r["instrument_type"], k, q)
    if not c.rows:
        raise KiteChainError(f"Kite returned no quotes for {underlying} {expiry}")
    t, f = c.years_left(now), c.forward(now)
    for k, pair in c.rows.items():
        for kind, q in pair.items():
            if q.mid:
                v = pricing.implied_vol(kind, q.mid, spot, k, t, fwd=f)
                q.iv = round(v * 100, 2) if v else None
    return c
