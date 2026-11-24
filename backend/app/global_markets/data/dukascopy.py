"""Dukascopy's free historical candles: real spot bid and ask, one file per side per UTC day. An unofficial, slow source
that answers 503 when pushed, so `backfill.py` asks one file at a time and backs off.

Path: /datafeed/XAUUSD/<year>/<zero-based month>/<day>/BID_candles_min_1.bi5. A file is LZMA-compressed 24-byte records,
big-endian: seconds into the day, open, close, low, high (ints; dollars x 1000 for XAUUSD), volume (float).
"""

from __future__ import annotations

import datetime as dt
import lzma
import struct

import httpx
import numpy as np

HOST = "https://datafeed.dukascopy.com/datafeed"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tradingbot/0.1)"}
RECORD = struct.Struct(">5if")
MAX_BYTES = 1440 * RECORD.size          # a day has at most 1440 minutes: anything larger is not this format
PRICE_SCALE = {"XAUUSD": 1000.0}
COLUMNS = ("t", "bo", "bh", "bl", "bc", "ao", "ah", "al", "ac", "v")


class Busy(Exception):
    """The server is refusing or not answering: try later."""


class BadFile(Exception):
    """The file came back but cannot be read as candles."""


def day_url(symbol: str, day: dt.date, side: str) -> str:
    return f"{HOST}/{symbol}/{day.year}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"


def parse(raw: bytes, day: dt.date, scale: float) -> dict[int, tuple[float, float, float, float, float]]:
    """{bar start in unix seconds: (open, high, low, close, volume)}. Minutes with no volume are skipped: the feed fills
    closed minutes with flat bars."""
    if not raw:
        return {}
    decoder = lzma.LZMADecompressor()
    try:
        data = decoder.decompress(raw, max_length=MAX_BYTES + 1)
    except lzma.LZMAError as e:
        raise BadFile(f"cannot decompress: {e}")
    if len(data) > MAX_BYTES or len(data) % RECORD.size:
        raise BadFile(f"unexpected size: {len(data)} bytes")
    midnight = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    out = {}
    for sec, o, c, lo, h, v in RECORD.iter_unpack(data):
        if v <= 0:
            continue
        if not 0 <= sec < 86400 or min(o, c, lo, h) <= 0 or lo > min(o, c) or h < max(o, c):
            raise BadFile(f"impossible candle at second {sec}")
        out[midnight + sec] = (o / scale, h / scale, lo / scale, c / scale, float(v))
    return out


def _get(url: str) -> bytes:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=45)
    except httpx.HTTPError as e:
        raise Busy(f"network: {type(e).__name__}")
    if r.status_code == 404:
        return b""
    if r.status_code != 200:
        raise Busy(f"HTTP {r.status_code}")
    return r.content


def join_sides(bid: dict, ask: dict) -> dict[str, np.ndarray] | None:
    """Minutes present on both sides, as columns; None if there are none."""
    ts = sorted(bid.keys() & ask.keys())
    if not ts:
        return None
    b = np.array([bid[t] for t in ts])
    a = np.array([ask[t] for t in ts])
    return {"t": np.array(ts, dtype=np.int64), "bo": b[:, 0], "bh": b[:, 1], "bl": b[:, 2], "bc": b[:, 3],
            "ao": a[:, 0], "ah": a[:, 1], "al": a[:, 2], "ac": a[:, 3], "v": b[:, 4] + a[:, 4]}


def fetch_day(symbol: str, day: dt.date) -> dict[str, np.ndarray] | None:
    scale = PRICE_SCALE[symbol]
    bid = parse(_get(day_url(symbol, day, "BID")), day, scale)
    if not bid:                         # a closed day: no need to ask for the other side
        return None
    return join_sides(bid, parse(_get(day_url(symbol, day, "ASK")), day, scale))
