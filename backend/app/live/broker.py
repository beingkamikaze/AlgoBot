"""The one place that talks to Zerodha. Everything else uses this small interface, so the tests
can swap in a fake broker and nothing in the app imports kiteconnect directly.

Order rules this follows (Kite Connect + SEBI's 2026 retail-algo framework):
  - no market orders: entries are LIMIT, stops are SL (stop-limit), exits are marketable LIMITs;
  - every order carries our tag (tb + 8 characters of our order id) so it can be found again;
  - at most ORDERS_PER_SECOND orders a second (SEBI's threshold is 10);
  - intraday product MIS; NSE for stocks, NFO for index futures.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

ORDERS_PER_SECOND = 4


class BrokerError(Exception):
    """The broker refused or failed. `ambiguous` = we do not know whether the order reached the exchange."""

    def __init__(self, message: str, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


@dataclass
class BrokerOrder:
    order_id: str
    status: str                  # OPEN, COMPLETE, CANCELLED, REJECTED, TRIGGER PENDING, ... (Kite's words)
    tag: str | None
    tradingsymbol: str
    side: str                    # buy / sell
    qty: int
    filled: int
    average_price: float
    price: float | None
    trigger_price: float | None
    order_type: str
    message: str | None = None

    @property
    def done(self) -> bool:
        return self.status in ("COMPLETE", "CANCELLED", "REJECTED")

    @property
    def is_open(self) -> bool:
        return not self.done


class Broker(Protocol):
    def profile(self) -> dict: ...
    def funds(self) -> dict: ...                                      # {"net": ..., "available": ...} equity segment
    def ltp(self, keys: list[str]) -> dict[str, float]: ...           # "NSE:RELIANCE" -> price
    def instruments(self, exchange: str) -> list[dict]: ...
    def orders(self) -> list[BrokerOrder]: ...
    def positions(self) -> list[dict]: ...                            # net MIS positions
    def order_margin(self, exchange: str, tradingsymbol: str, side: str, qty: int, price: float) -> float: ...
    def place(self, *, exchange: str, tradingsymbol: str, side: str, qty: int, order_type: str,
              price: float | None, trigger: float | None, tag: str) -> str: ...
    def modify(self, order_id: str, *, price: float | None = None, trigger: float | None = None,
               order_type: str | None = None, qty: int | None = None) -> None: ...
    def cancel(self, order_id: str) -> None: ...


def tag_for(order_id: str) -> str:
    return "tb" + order_id.replace("-", "")[:8]


class _Bucket:
    def __init__(self, per_second: int):
        self.per, self.times, self.lock = per_second, [], threading.Lock()

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            self.times = [t for t in self.times if now - t < 1.0]
            if len(self.times) >= self.per:
                time.sleep(1.0 - (now - self.times[0]) + 0.01)
            self.times.append(time.monotonic())


_bucket = _Bucket(ORDERS_PER_SECOND)


class KiteBroker:
    """Wraps the official kiteconnect SDK for one day's session."""

    def __init__(self, api_key: str, access_token: str, algo_id: str = ""):
        from kiteconnect import KiteConnect
        self.k = KiteConnect(api_key=api_key, timeout=7)
        self.k.set_access_token(access_token)
        self.algo_id = algo_id or None

    @staticmethod
    def _err(e: Exception, placing: bool = False) -> BrokerError:
        from kiteconnect import exceptions as kx
        # a network or gateway failure while placing: the order may or may not exist at the exchange
        ambiguous = placing and (isinstance(e, (kx.NetworkException, kx.GeneralException))
                                 or e.__class__.__name__ in ("ReadTimeout", "ConnectTimeout", "ConnectionError", "Timeout"))
        return BrokerError(f"Kite: {e}", ambiguous=ambiguous)

    def _call(self, fn, *a, placing: bool = False, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 - every SDK failure becomes a BrokerError
            raise self._err(e, placing) from e

    def profile(self) -> dict:
        return self._call(self.k.profile)

    def funds(self) -> dict:
        m = self._call(self.k.margins, "equity")
        return {"net": float(m.get("net") or 0), "available": float((m.get("available") or {}).get("live_balance") or 0)}

    def ltp(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        out = self._call(self.k.ltp, *keys)
        return {k: float(v["last_price"]) for k, v in out.items()}

    def instruments(self, exchange: str) -> list[dict]:
        return self._call(self.k.instruments, exchange)

    def orders(self) -> list[BrokerOrder]:
        return [BrokerOrder(order_id=str(o["order_id"]), status=str(o["status"]), tag=o.get("tag"),
                            tradingsymbol=o["tradingsymbol"], side=o["transaction_type"].lower(), qty=int(o["quantity"]),
                            filled=int(o.get("filled_quantity") or 0), average_price=float(o.get("average_price") or 0),
                            price=float(o["price"]) if o.get("price") else None,
                            trigger_price=float(o["trigger_price"]) if o.get("trigger_price") else None,
                            order_type=o["order_type"], message=o.get("status_message"))
                for o in self._call(self.k.orders)]

    def positions(self) -> list[dict]:
        net = self._call(self.k.positions).get("net", [])
        return [{"tradingsymbol": p["tradingsymbol"], "exchange": p["exchange"], "qty": int(p["quantity"])}
                for p in net if p.get("product") == "MIS"]

    def order_margin(self, exchange: str, tradingsymbol: str, side: str, qty: int, price: float) -> float:
        r = self._call(self.k.order_margins, [{"exchange": exchange, "tradingsymbol": tradingsymbol,
                                               "transaction_type": side.upper(), "variety": "regular", "product": "MIS",
                                               "order_type": "LIMIT", "quantity": qty, "price": price}])
        return float(r[0]["total"]) if r else 0.0

    def place(self, *, exchange: str, tradingsymbol: str, side: str, qty: int, order_type: str,
              price: float | None, trigger: float | None, tag: str) -> str:
        if order_type not in ("LIMIT", "SL"):
            raise BrokerError("only LIMIT and SL orders are sent (API market orders need market protection)")
        _bucket.wait()
        return str(self._call(self.k.place_order, placing=True, variety="regular", exchange=exchange,
                              tradingsymbol=tradingsymbol, transaction_type=side.upper(), quantity=qty, product="MIS",
                              order_type=order_type, price=price, trigger_price=trigger, validity="DAY", tag=tag,
                              algo_id=self.algo_id))

    def modify(self, order_id: str, *, price: float | None = None, trigger: float | None = None,
               order_type: str | None = None, qty: int | None = None) -> None:
        if order_type not in (None, "LIMIT", "SL"):
            raise BrokerError("only LIMIT and SL orders are sent")
        _bucket.wait()
        self._call(self.k.modify_order, variety="regular", order_id=order_id, price=price, trigger_price=trigger,
                   order_type=order_type, quantity=qty)

    def cancel(self, order_id: str) -> None:
        _bucket.wait()
        self._call(self.k.cancel_order, variety="regular", order_id=order_id)
