"""Which NSE stocks have futures and options, and their lot sizes, from NSE's daily lot-size file.

  https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv
  UNDERLYING, SYMBOL, NOV-26, DEC-26, JAN-27, ...   (lot size per contract month; blank = no contract)

Cached on disk for a day. Lot sizes change every few months (NSE reviews them), so the
app never hard-codes a stock's lot.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import threading

from app.market.nse import DATA, _fetch, _fresh

URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
PATH = DATA / "fo_mktlots.csv"
INDEX_ROWS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
_lock = threading.Lock()
_parsed: tuple[float, dict] | None = None


class FnoError(Exception):
    pass


def parse(text: str) -> dict[str, dict[str, int]]:
    """{symbol: {"NOV-26": 175, "DEC-26": 175, ...}} for stocks (index rows are left out)."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    head = [h.strip().upper() for h in rows[0]]
    try:
        sym_col = head.index("SYMBOL")
    except ValueError:
        raise FnoError("NSE's lot-size file has no SYMBOL column")
    months = [(i, h) for i, h in enumerate(head) if len(h) == 6 and h[3] == "-"]
    out: dict[str, dict[str, int]] = {}
    for r in rows[1:]:
        if len(r) <= sym_col:
            continue
        sym = r[sym_col].strip().upper()
        if not sym or sym == "SYMBOL" or sym in INDEX_ROWS:
            continue
        lots = {}
        for i, h in months:
            v = r[i].strip() if i < len(r) else ""
            if v.isdigit():
                lots[h] = int(v)
        if lots:
            out[sym] = lots
    return out


def table(refresh: bool = False) -> dict[str, dict[str, int]]:
    global _parsed
    with _lock:
        if _parsed and not refresh and _fresh(PATH, 86400):
            return _parsed[1]
        if refresh or not _fresh(PATH, 86400):
            try:
                body = _fetch(URL)
                DATA.mkdir(exist_ok=True)
                PATH.write_bytes(body)
            except Exception as e:  # noqa: BLE001 - use yesterday's file if NSE is down
                if not PATH.exists():
                    raise FnoError(f"cannot load NSE's F&O lot sizes: {e}")
        _parsed = (PATH.stat().st_mtime, parse(PATH.read_text(errors="replace")))
        return _parsed[1]


def month_key(d: dt.date) -> str:
    return f"{d:%b}-{d:%y}".upper()


def is_fno(symbol: str) -> bool:
    try:
        return symbol.upper() in table()
    except FnoError:
        return False


def lot_size(symbol: str, expiry: dt.date) -> int:
    lots = table().get(symbol.upper())
    if not lots:
        raise FnoError(f"{symbol} has no futures or options on NSE")
    if month_key(expiry) in lots:
        return lots[month_key(expiry)]
    return next(iter(lots.values()))           # the nearest month's lot when the month is not listed yet
