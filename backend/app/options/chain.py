"""Option chains: from Kite Connect when you are logged in to Kite today (real-time, source "kite",
app.options.kite_chain), else from NSE's public API (delayed, source "nse"): Nifty, Bank Nifty and every NSE F&O stock.

  expiries:  https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY   (or TITAN)
  chain:     https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=27-Oct-2026
             https://www.nseindia.com/api/option-chain-v3?type=Equity&symbol=TITAN&expiry=23-Nov-2026

Stock options are monthly, physically settled if held to expiry (this app only trades them intraday), and
often illiquid away from the money: check the spread before trading. There is no modeled fallback for
stocks: if NSE does not answer, the app says so instead of inventing prices.

The chain carries last price, best bid and ask, NSE's implied volatility, open
interest and volume per strike. It is delayed and unofficial: the fallback when there is no Kite login.
If neither answers, a MODELED chain is priced with Black-76 from India VIX (source "modeled"); paper fills
then say so.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field

import httpx

from app.core.calendar import CLOSE, IST
from app.core.instruments import INDEX_FUTURES, MONTHS
from app.market import yahoo
from app.market.nse import NSE_HEADERS
from app.options import pricing

INFO = "https://www.nseindia.com/api/option-chain-contract-info?symbol={u}"
CHAIN = "https://www.nseindia.com/api/option-chain-v3?type={t}&symbol={u}&expiry={e}"
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
WEEKLY_MONTH = {10: "O", 11: "N", 12: "D"}
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


class ChainError(Exception):
    pass


@dataclass
class Quote:
    kind: str                 # "CE" or "PE"
    strike: float
    ltp: float | None
    bid: float | None
    ask: float | None
    iv: float | None          # NSE's IV (or ours from the mid, on Kite chains), in percent
    oi: int | None = None
    chg_oi: int | None = None
    volume: int | None = None
    depth: dict | None = None # Kite chains only: {"buy": [{price, quantity, orders} x5], "sell": [...]}

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2, 2)
        return self.ltp

    @property
    def spread_pct(self) -> float | None:
        m = self.mid
        return round((self.ask - self.bid) / m * 100, 2) if self.bid and self.ask and m else None


@dataclass
class Chain:
    underlying: str
    expiry: dt.date
    spot: float
    as_of: str                # NSE's timestamp, IST
    source: str               # "kite" (real-time), "nse" (delayed) or "modeled"
    rows: dict[float, dict[str, Quote]] = field(default_factory=dict)
    expiries: list[dt.date] = field(default_factory=list)
    lot: int | None = None            # stocks: from NSE's F&O lot file; indices: INDEX_FUTURES

    @property
    def lot_size(self) -> int:
        return self.lot or INDEX_FUTURES[self.underlying]["lot_size"]

    @property
    def is_index(self) -> bool:
        return self.underlying in INDEX_FUTURES

    def strikes(self) -> list[float]:
        return sorted(self.rows)

    def atm(self) -> float:
        return min(self.rows, key=lambda k: abs(k - self.spot))

    def quote(self, strike: float, kind: str) -> Quote | None:
        return self.rows.get(float(strike), {}).get(kind)

    def pcr(self) -> float | None:
        ce = sum((r["CE"].oi or 0) for r in self.rows.values() if "CE" in r)
        pe = sum((r["PE"].oi or 0) for r in self.rows.values() if "PE" in r)
        return round(pe / ce, 2) if ce else None

    def max_pain(self) -> float | None:
        """The expiry price at which option buyers in total would lose the most (writers pay the least)."""
        if not any((q.oi or 0) for r in self.rows.values() for q in r.values()):
            return None
        best, best_pain = None, None
        for x in self.rows:
            pain = sum(max(x - k, 0) * (r["CE"].oi or 0) if "CE" in r else 0 for k, r in self.rows.items())
            pain += sum(max(k - x, 0) * (r["PE"].oi or 0) if "PE" in r else 0 for k, r in self.rows.items())
            if best_pain is None or pain < best_pain:
                best, best_pain = x, pain
        return best

    def atm_iv(self) -> float | None:
        r = self.rows.get(self.atm(), {})
        ivs = [q.iv for q in r.values() if q.iv]
        return round(sum(ivs) / len(ivs), 2) if ivs else None

    def is_monthly(self) -> bool:
        later_same_month = [e for e in self.expiries if e > self.expiry and (e.year, e.month) == (self.expiry.year, self.expiry.month)]
        return not later_same_month

    def tradingsymbol(self, strike: float, kind: str) -> str:
        """Kite's name: monthly NIFTY26OCT22600CE, weekly NIFTY26N0322600CE (month 1-9, O, N, D)."""
        e, u, k = self.expiry, self.underlying, int(strike) if float(strike).is_integer() else strike
        if self.is_monthly() or not self.is_index:
            return f"{u}{e:%y}{MONTHS[e.month - 1]}{k}{kind}"
        return f"{u}{e:%y}{WEEKLY_MONTH.get(e.month, str(e.month))}{e:%d}{k}{kind}"

    def forward(self, now: dt.datetime | None = None) -> float:
        """The market's forward for this expiry, from put-call parity at the ATM strike (spot if it cannot be read)."""
        r = self.rows.get(self.atm(), {})
        c, p = r.get("CE"), r.get("PE")
        if self.source == "modeled" or not (c and p and c.mid and p.mid):
            return pricing.forward(self.spot, self.years_left(now))
        return round(pricing.parity_forward(c.mid, p.mid, self.atm(), self.years_left(now)), 2)

    def greeks(self, strike: float, kind: str, now: dt.datetime | None = None) -> pricing.Greeks | None:
        """Greeks from our own IV at the mid, priced off the chain's forward."""
        q = self.quote(strike, kind)
        if not q or not q.mid:
            return None
        t, f = self.years_left(now), self.forward(now)
        iv = pricing.implied_vol(kind, q.mid, self.spot, strike, t, fwd=f) or ((q.iv or 0) / 100) or None
        return pricing.black76(kind, self.spot, strike, t, iv, fwd=f) if iv else None

    def years_left(self, now: dt.datetime | None = None) -> float:
        now = now or dt.datetime.now(IST)
        close = dt.datetime.combine(self.expiry, CLOSE, IST)
        return pricing.years_to_expiry(now.timestamp(), close.timestamp())


def _cached(key: str, ttl: float, fn):
    with _lock:
        hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.monotonic(), val)
    return val


def _get(url: str) -> dict:
    try:
        r = httpx.get(url, headers=NSE_HEADERS, timeout=20)
    except httpx.HTTPError as e:
        raise ChainError(f"NSE did not answer: {e}")
    if r.status_code != 200 or not r.content:
        raise ChainError(f"NSE answered HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise ChainError("NSE sent something that is not JSON")


def _kite():
    from app.market import kite
    return kite.current()


def expiries(underlying: str) -> list[dt.date]:
    u = underlying.upper()
    kd = _kite()
    if kd is not None:
        from app.market.kite import KiteDataError
        from app.options import kite_chain
        try:
            return kite_chain.expiries(kd, u)
        except (KiteDataError, kite_chain.KiteChainError):
            pass                                  # NSE below

    def load():
        d = _get(INFO.format(u=u))
        return sorted(dt.datetime.strptime(x, "%d-%b-%Y").date() for x in d.get("expiryDates", []))
    return _cached(f"exp|{u}", 3600, load)


def nearest_expiry(underlying: str, today: dt.date, min_days: int = 0) -> dt.date:
    """The first expiry at least `min_days` calendar days away (0 = today's expiry counts)."""
    for e in expiries(underlying):
        if (e - today).days >= min_days:
            return e
    raise ChainError(f"no {underlying} expiry found")


def parse(doc: dict, underlying: str, expiry: dt.date, all_expiries: list[dt.date]) -> Chain:
    rec = doc.get("records") or {}
    spot = rec.get("underlyingValue")
    data = rec.get("data") or []
    if not spot or not data:
        raise ChainError("NSE returned an empty chain")
    c = Chain(underlying=underlying, expiry=expiry, spot=float(spot), as_of=rec.get("timestamp") or "", source="nse",
              expiries=all_expiries)
    for row in data:
        k = float(row.get("strikePrice"))
        for kind in ("CE", "PE"):
            q = row.get(kind)
            if not q:
                continue
            c.rows.setdefault(k, {})[kind] = Quote(
                kind=kind, strike=k, ltp=q.get("lastPrice") or None, bid=q.get("buyPrice1") or None,
                ask=q.get("sellPrice1") or None, iv=q.get("impliedVolatility") or None, oi=q.get("openInterest"),
                chg_oi=q.get("changeinOpenInterest"), volume=q.get("totalTradedVolume"))
    return c


def modeled(underlying: str, expiry: dt.date, now: dt.datetime | None = None, width: int = 20) -> Chain:
    """A chain priced with Black-76 from India VIX: used when NSE does not answer, and in backtests."""
    now = now or dt.datetime.now(IST)
    spot = yahoo.candles(underlying, "1d", "1mo").bars[-1].c
    vix = yahoo.candles("INDIAVIX", "1d", "1mo").bars[-1].c / 100
    vol = vix * (1.2 if underlying == "BANKNIFTY" else 1.0)
    step = STRIKE_STEP[underlying]
    atm = round(spot / step) * step
    c = Chain(underlying=underlying, expiry=expiry, spot=spot, as_of=now.strftime("%d-%b-%Y %H:%M:%S"), source="modeled",
              expiries=[expiry])
    t = c.years_left(now)
    for i in range(-width, width + 1):
        k = float(atm + i * step)
        for kind in ("CE", "PE"):
            p = pricing.black76(kind, spot, k, t, vol).price
            c.rows.setdefault(k, {})[kind] = Quote(kind=kind, strike=k, ltp=round(p, 2), bid=round(p * 0.99, 2),
                                                   ask=round(p * 1.01, 2), iv=round(vol * 100, 2))
    return c


def from_kite(u: str, expiry: dt.date | None) -> Chain | None:
    """The Kite chain, or None when there is no Kite login today or Kite fails (then NSE is used)."""
    kd = _kite()
    if kd is None:
        return None
    from app.market.kite import KiteDataError
    from app.options import kite_chain
    try:
        exps = kite_chain.expiries(kd, u)
        e = expiry or exps[0]
        if e not in exps:
            raise ChainError(f"{e} is not an expiry for {u}")
        return _cached(f"kite|{u}|{e}", 5, lambda: kite_chain.build(kd, u, e, exps))
    except (KiteDataError, kite_chain.KiteChainError) as err:
        import logging
        logging.getLogger("chain").info("Kite chain for %s failed, using NSE: %s", u, err)
        return None


def chain(underlying: str, expiry: dt.date | None = None, allow_modeled: bool = True) -> Chain:
    u = underlying.upper()
    if u not in STRIKE_STEP:
        return stock_chain(u, expiry)
    k = from_kite(u, expiry)
    if k is not None:
        return k
    try:
        exps = expiries(u)
        e = expiry or nearest_expiry(u, dt.datetime.now(IST).date())
        if e not in exps:
            raise ChainError(f"{e} is not an NSE expiry for {u}")
        return _cached(f"chain|{u}|{e}", 15, lambda: parse(_get(CHAIN.format(t="Indices", u=u, e=e.strftime("%d-%b-%Y"))), u, e, exps))
    except ChainError:
        if not allow_modeled or expiry is None and not allow_modeled:
            raise
        e = expiry or dt.datetime.now(IST).date()
        return modeled(u, e)


def stock_chain(symbol: str, expiry: dt.date | None = None) -> Chain:
    """An NSE F&O stock's chain (monthly expiries only). No modeled fallback."""
    from app.market import fno
    u = symbol.upper().replace("-FUT", "")
    if not fno.is_fno(u):
        raise ChainError(f"{u} has no options on NSE (options exist for Nifty, Bank Nifty and NSE's F&O stocks)")
    k = from_kite(u, expiry)
    if k is not None:
        return k
    exps = expiries(u)
    e = expiry or nearest_expiry(u, dt.datetime.now(IST).date())
    if e not in exps:
        raise ChainError(f"{e} is not an NSE expiry for {u}")

    def load():
        c = parse(_get(CHAIN.format(t="Equity", u=u, e=e.strftime("%d-%b-%Y"))), u, e, exps)
        c.lot = fno.lot_size(u, e)
        return c
    return _cached(f"chain|{u}|{e}", 15, load)
