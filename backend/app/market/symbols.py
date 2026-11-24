"""Our symbols -> provider symbols. NSE equities are plain NSE symbols (RELIANCE, M&M,
BAJAJ-AUTO); indices have short names. Kite tokens replace this map in Phase 3."""

from __future__ import annotations

INDICES: dict[str, tuple[str, str]] = {   # our name -> (Yahoo symbol, display name)
    "NIFTY": ("^NSEI", "Nifty 50"),
    "BANKNIFTY": ("^NSEBANK", "Nifty Bank"),
    "INDIAVIX": ("^INDIAVIX", "India VIX"),
    "FINNIFTY": ("NIFTY_FIN_SERVICE.NS", "Nifty Financial Services"),
    "NIFTYIT": ("^CNXIT", "Nifty IT"),
}
_REVERSE = {y: k for k, (y, _) in INDICES.items()}


def normalize(symbol: str) -> str:
    s = symbol.strip().upper().replace(" ", "")
    if s in ("NIFTY50", "^NSEI"):
        return "NIFTY"
    if s in ("NIFTYBANK", "^NSEBANK"):
        return "BANKNIFTY"
    if s.endswith(".NS"):
        s = s[:-3]
    return _REVERSE.get(s, s)


def to_yahoo(symbol: str) -> str:
    s = normalize(symbol)
    if s in INDICES:
        return INDICES[s][0]
    return s if s.endswith(".BO") else s + ".NS"


def is_index(symbol: str) -> bool:
    return normalize(symbol) in INDICES
