"""The app's side of MT5: a client for the Windows bridge (`bridge.py`) and the gold broker built on it.

Nothing here is wired to place orders by itself. It is the adapter a future live gold desk would use, built now against a
fake MT5 so it is ready for a demo account. The rules it keeps, on top of the bridge's own:

  - DEMO only: `connect()` refuses a real or contest account unless `allow_real=True`, which nothing in the app passes.
    Live gold trading waits for your lawyer's answer;
  - sizes come from the app's risk check in the app's lots, and are converted to the broker's lot (some brokers' gold lot
    is not 100 oz), always rounded DOWN, so the broker never risks more than the check passed;
  - every order carries a tag (ab + 8 characters of the app's id) in MT5's comment, so it can be found again. When the
    bridge times out while placing, nobody knows whether the order went through: the broker looks for the tag before
    anyone tries again, so an order is never sent twice;
  - broker refusals come back in plain words.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import httpx

from app.global_markets.instruments import GlobalInstrument, resolve

RETCODES = {
    10004: "requote: the price moved before the order was taken",
    10006: "the broker rejected the order",
    10013: "the broker said the request was invalid",
    10014: "the broker does not allow this volume",
    10015: "the broker does not allow this price",
    10016: "the stop or target is too close to the price or on the wrong side",
    10017: "trading is disabled for this account",
    10018: "the market is closed",
    10019: "not enough free margin",
    10021: "the broker has no prices right now",
    10022: "the broker does not allow this expiry time",
    10024: "too many requests: wait a moment",
    10027: "algo trading is switched off in the MT5 terminal: press its Algo Trading button",
    10030: "the broker does not allow this fill type",
    10031: "the MT5 terminal has no connection to the broker",
    10033: "too many pending orders on the account",
}


class Mt5Error(Exception):
    """The bridge or the broker refused or failed. `ambiguous` = we do not know whether an order reached the broker."""

    def __init__(self, message: str, ambiguous: bool = False, retcode: int | None = None):
        super().__init__(message)
        self.ambiguous, self.retcode = ambiguous, retcode


def words(retcode: int | None, comment: str = "") -> str:
    base = RETCODES.get(retcode or 0, f"the broker refused (code {retcode})")
    return f"{base} ({comment})" if comment and comment not in base else base


class BridgeClient:
    """HTTP calls to the bridge. The token travels in a header and is never put in an error."""

    def __init__(self, url: str, token: str, http: httpx.Client | None = None, timeout: float = 8.0):
        if not url or not token:
            raise Mt5Error("MT5 is not set up: put MT5_BRIDGE_URL and MT5_BRIDGE_TOKEN in backend/.env")
        self.url, self._token = url.rstrip("/"), token
        self._http = http or httpx.Client(timeout=timeout)
        self._timeout = timeout

    def _req(self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
             placing: bool = False) -> Any:
        try:
            r = self._http.request(method, f"{self.url}{path}", json=body, params=params,
                                   headers={"X-Bridge-Token": self._token}, timeout=self._timeout)
        except httpx.ConnectError as e:              # never reached the bridge: nothing was sent
            raise Mt5Error(f"cannot reach the MT5 bridge at {self.url}: {type(e).__name__}") from None
        except httpx.HTTPError as e:                 # sent, then lost: an order may or may not exist now
            raise Mt5Error(f"the MT5 bridge did not answer ({type(e).__name__})", ambiguous=placing) from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            raise Mt5Error(f"MT5 bridge: {detail or r.status_code}")
        return r.json()

    def health(self) -> dict:
        return self._req("GET", "/health")

    def symbol(self) -> dict:
        return self._req("GET", "/symbol")

    def quote(self) -> dict:
        return self._req("GET", "/quote")

    def positions(self) -> list[dict]:
        return self._req("GET", "/positions")

    def orders(self) -> list[dict]:
        return self._req("GET", "/orders")

    def deals(self, since: int) -> list[dict]:
        return self._req("GET", "/deals", params={"since": since})

    def place(self, entry: dict) -> dict:
        return self._req("POST", "/orders", body=entry, placing=True)

    def cancel(self, ticket: int) -> dict:
        return self._req("POST", f"/orders/{ticket}/cancel")

    def stops(self, ticket: int, sl: float | None, tp: float | None) -> dict:
        return self._req("POST", f"/positions/{ticket}/stops", body={"sl": sl, "tp": tp})

    def close(self, ticket: int, deviation: int, comment: str) -> dict:
        return self._req("POST", f"/positions/{ticket}/close", body={"deviation": deviation, "comment": comment},
                         placing=True)


def tag_for(ref: str) -> str:
    return "ab" + ref.replace("-", "")[:8]


class Mt5Broker:
    """Gold through MT5, in the app's terms: lots of 100 oz, prices in dollars, plain-language refusals."""

    def __init__(self, client: BridgeClient, *, allow_real: bool = False, inst: GlobalInstrument | None = None):
        self.client, self.allow_real = client, allow_real
        self.inst = inst or resolve("XAUUSD")
        self._spec: dict | None = None
        self.account: dict | None = None

    # ------------------------------------------------------------ connecting

    def connect(self) -> dict:
        h = self.client.health()
        mode = h["account"]["trade_mode"]
        if mode != "demo" and not self.allow_real:
            raise Mt5Error(f"the MT5 account is a {mode} account: the app connects to demo accounts only until live gold "
                           "trading is cleared")
        self.account, self._spec = h["account"], self.client.symbol()
        return h

    @property
    def spec(self) -> dict:
        if self._spec is None:
            self.connect()
        return self._spec  # type: ignore[return-value]

    def volume(self, lots: float) -> float:
        """The app's lots -> the broker's lots, rounded down to the broker's step."""
        s = self.spec
        raw = lots * self.inst.contract_size / float(s["contract_size"])
        step = float(s["volume_step"])
        vol = round(math.floor(raw / step + 1e-9) * step, 8)
        if vol < float(s["volume_min"]) - 1e-9:
            raise Mt5Error(f"{lots:g} lots is {raw:g} of this broker's lots, below its minimum of {s['volume_min']:g}")
        if vol > float(s["volume_max"]) + 1e-9:
            raise Mt5Error(f"{vol:g} lots is above this broker's maximum of {s['volume_max']:g} per order")
        return vol

    def price(self, p: float) -> float:
        return round(p, int(self.spec["digits"]))

    def quote(self) -> dict:
        q = self.client.quote()
        return {**q, "spread": round(q["ask"] - q["bid"], int(self.spec["digits"]))}

    # ------------------------------------------------------------ orders

    def place_entry(self, ref: str, side: str, lots: float, limit: float, stop: float, target: float,
                    expires_at: dt.datetime | None = None) -> dict:
        """One entry with its stop and target attached at the broker. Returns the ticket and whether it filled now."""
        if side not in ("buy", "sell"):
            raise Mt5Error("side must be buy or sell")
        long = side == "buy"
        limit, stop, target = self.price(limit), self.price(stop), self.price(target)
        if not (stop < limit < target if long else target < limit < stop):
            raise Mt5Error(f"for a {side} the stop must be {'below' if long else 'above'} the limit and the target "
                           f"{'above' if long else 'below'} it")
        gap = float(self.spec["stops_level"]) * float(self.spec["point"])
        if gap and (abs(limit - stop) < gap or abs(target - limit) < gap):
            raise Mt5Error(f"this broker needs the stop and target at least {gap:g} from the price")
        tag = tag_for(ref)
        entry = {"side": side, "volume": self.volume(lots), "price": limit, "sl": stop, "tp": target, "comment": tag,
                 "expires_at": int(expires_at.timestamp()) if expires_at else None}
        try:
            r = self.client.place(entry)
        except Mt5Error as e:
            if not e.ambiguous:
                raise
            found = self.find(tag)
            if found:
                return {**found, "tag": tag, "found_after_timeout": True}
            raise Mt5Error("the bridge timed out and the order is not at the broker (yet). Do not send it again: "
                           "check again in a moment", ambiguous=True) from None
        if not r["ok"]:
            raise Mt5Error(words(r["retcode"], r.get("message", "")), retcode=r["retcode"])
        return {"ticket": r["order"], "state": "filled" if r.get("deal") else "pending", "deal": r.get("deal"),
                "price": r.get("price"), "volume": r.get("volume") or entry["volume"], "tag": tag,
                "found_after_timeout": False}

    def find(self, tag: str, since: int | None = None) -> dict | None:
        """The order with this tag, wherever it is now: pending, an open position, or already closed."""
        for o in self.client.orders():
            if o["comment"] == tag:
                return {"ticket": o["ticket"], "state": "pending", "price": o["price"], "volume": o["volume"]}
        for p in self.client.positions():
            if p["comment"] == tag:
                return {"ticket": p["ticket"], "state": "filled", "price": p["price_open"], "volume": p["volume"]}
        since = since or int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).timestamp())
        for d in self.client.deals(since):
            if d["comment"] == tag and d["entry"] == "in":
                return {"ticket": d["position"], "state": "filled", "price": d["price"], "volume": d["volume"]}
        return None

    def _ok(self, r: dict) -> dict:
        if not r["ok"]:
            raise Mt5Error(words(r["retcode"], r.get("message", "")), retcode=r["retcode"])
        return r

    def cancel(self, ticket: int) -> dict:
        return self._ok(self.client.cancel(ticket))

    def move_stops(self, ticket: int, stop: float | None = None, target: float | None = None) -> dict:
        return self._ok(self.client.stops(ticket, self.price(stop) if stop is not None else None,
                                          self.price(target) if target is not None else None))

    def close(self, ticket: int, max_slippage: float, reason: str = "close") -> dict:
        """Out at the market, no worse than `max_slippage` dollars from the current price. Never blocked by a limit."""
        points = int(round(max_slippage / float(self.spec["point"])))
        return self._ok(self.client.close(ticket, points, reason[:31]))

    # ------------------------------------------------------------ checking the app against the broker

    def reconcile(self, expected: list[dict]) -> list[str]:
        """`expected`: the app's open trades as {"tag", "side", "lots"}. Each difference, in plain words; empty = agreed."""
        held = {p["comment"]: p for p in self.client.positions()}
        out = []
        for e in expected:
            p = held.pop(e["tag"], None)
            want = self.volume(e["lots"])
            if p is None:
                out.append(f"the app has an open {e['side']} ({e['tag']}) that MT5 does not have")
            elif p["side"] != e["side"] or abs(p["volume"] - want) > 1e-9:
                out.append(f"{e['tag']}: the app has {e['side']} {want:g} lots, MT5 has {p['side']} {p['volume']:g}")
        out += [f"MT5 has an open {p['side']} {p['volume']:g} lots ({tag or 'no tag'}) that the app does not know about"
                for tag, p in held.items()]
        return out
