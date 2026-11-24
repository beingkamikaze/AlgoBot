"""In-memory stand-ins for Supabase (PostgREST filters) and Zerodha (Kite's order book), for the live tests."""

from __future__ import annotations

import copy
import datetime as dt
import itertools
import uuid

from app.live.broker import BrokerError, BrokerOrder


DEFAULTS = {"trades": {"exit_ts": None, "exit": None, "net_pnl": None, "r_multiple": None, "exit_reason": None},
            "orders": {"broker_order_id": None, "signal_id": None, "approved_at": None}}


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}
        self._n = itertools.count(1)

    @staticmethod
    def _get(row: dict, key: str):
        if "->>" in key:
            col, field = key.split("->>")
            v = (row.get(col) or {}).get(field)
            return None if v is None else str(v)
        return row.get(key)

    def _match(self, row: dict, filters: dict | None) -> bool:
        for k, f in (filters or {}).items():
            v = self._get(row, k)
            if f == "is.null":
                ok = v is None
            elif f == "not.is.null":
                ok = v is not None
            elif f.startswith("eq."):
                ok = str(v).lower() == f[3:].lower() if isinstance(v, bool) else (v is not None and str(v) == f[3:])
            elif f.startswith("in.("):
                ok = str(v) in f[4:-1].split(",")
            elif f.startswith(("gte.", "gt.", "lt.", "lte.")):
                op, val = f.split(".", 1)
                if v is None:
                    ok = False
                else:
                    a, b = str(v), val
                    ok = {"gte": a >= b, "gt": a > b, "lt": a < b, "lte": a <= b}[op]
            else:
                raise AssertionError(f"filter not supported by FakeDB: {k}={f}")
            if not ok:
                return False
        return True

    def select(self, table, filters=None, order=None, limit=None, columns=None):
        rows = [copy.deepcopy(r) for r in self.tables.get(table, []) if self._match(r, filters)]
        if order:
            col, _, direction = order.partition(".")
            rows.sort(key=lambda r: str(r.get(col) or ""), reverse=direction == "desc")
        return rows[:limit] if limit else rows

    def insert(self, table, rows, upsert_on=None):
        rows = rows if isinstance(rows, list) else [rows]
        out = []
        for r in rows:
            r = {"id": str(uuid.uuid4()) if table not in ("risk_events", "audit_log", "webhook_events") else next(self._n),
                 "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), **copy.deepcopy(r)}
            for k, v in DEFAULTS.get(table, {}).items():          # columns Postgres would fill with null/defaults
                r.setdefault(k, v)
            if table == "risk_events":
                r.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
            self.tables.setdefault(table, []).append(r)
            out.append(copy.deepcopy(r))
        return out

    def update(self, table, filters, values):
        out = []
        for r in self.tables.get(table, []):
            if self._match(r, filters):
                r.update(copy.deepcopy(values))
                out.append(copy.deepcopy(r))
        return out

    def delete(self, table, filters):
        self.tables[table] = [r for r in self.tables.get(table, []) if not self._match(r, filters)]


class FakeKite:
    """Kite's order book for one day. Orders rest until the test fills, rejects or cancels them."""

    def __init__(self, prices: dict[str, float], lot_sizes: dict[str, int] | None = None, funds: float = 200000):
        self.prices = dict(prices)
        self.lot_sizes = lot_sizes or {}
        self.cash = funds
        self.book: dict[str, BrokerOrder] = {}
        self.extra_positions: dict[str, int] = {}
        self.fail_next: str | None = None          # "reject" or "ambiguous" for the next place()
        self.fail_place_type: str | None = None    # refuse every order of this type (e.g. "SL")
        self.fail_modify = False                   # refuse the next modify()
        self.calls: list[tuple] = []
        self._ids = itertools.count(1000)

    # -- the Broker interface
    def profile(self):
        return {"user_id": "AB1234"}

    def funds(self):
        return {"net": self.cash, "available": self.cash}

    def ltp(self, keys):
        return {k: self.prices[k] for k in keys if k in self.prices}

    def instruments(self, exchange):
        if exchange == "NFO":
            return [{"tradingsymbol": s, "lot_size": n} for s, n in self.lot_sizes.items()]
        return [{"tradingsymbol": k.split(":")[1], "lot_size": 1} for k in self.prices if k.startswith("NSE:")]

    def orders(self):
        return [copy.copy(o) for o in self.book.values()]

    def positions(self):
        net: dict[str, int] = dict(self.extra_positions)
        for o in self.book.values():
            if o.filled:
                net[o.tradingsymbol] = net.get(o.tradingsymbol, 0) + (o.filled if o.side == "buy" else -o.filled)
        return [{"tradingsymbol": s, "exchange": "NSE", "qty": q} for s, q in net.items()]

    def order_margin(self, exchange, tradingsymbol, side, qty, price):
        return round(qty * price * 0.15, 2)

    def place(self, *, exchange, tradingsymbol, side, qty, order_type, price, trigger, tag):
        self.calls.append(("place", exchange, tradingsymbol, side, qty, order_type, price, trigger, tag))
        if self.fail_next == "reject" or self.fail_place_type == order_type:
            self.fail_next = None
            raise BrokerError("Kite: RMS: order rejected")
        oid = str(next(self._ids))
        self.book[oid] = BrokerOrder(order_id=oid, status="TRIGGER PENDING" if order_type == "SL" else "OPEN", tag=tag,
                                     tradingsymbol=tradingsymbol, side=side, qty=qty, filled=0, average_price=0.0,
                                     price=price, trigger_price=trigger, order_type=order_type)
        if self.fail_next == "ambiguous":            # it reached the exchange, but the answer was lost
            self.fail_next = None
            raise BrokerError("Kite: read timed out", ambiguous=True)
        return oid

    def modify(self, order_id, *, price=None, trigger=None, order_type=None, qty=None):
        self.calls.append(("modify", order_id, price, trigger, order_type) + ((qty,) if qty is not None else ()))
        o = self.book[order_id]
        if o.done:
            raise BrokerError("Kite: order is already complete")
        if self.fail_modify:
            self.fail_modify = False
            raise BrokerError("Kite: modify refused")
        if order_type:
            o.order_type = order_type
            o.status = "OPEN" if order_type == "LIMIT" else o.status
        if price is not None:
            o.price = price
        if trigger is not None:
            o.trigger_price = trigger
        if qty is not None:
            o.qty = qty

    def cancel(self, order_id):
        self.calls.append(("cancel", order_id))
        o = self.book[order_id]
        if o.done:
            raise BrokerError("Kite: order is already complete")
        o.status = "CANCELLED"

    # -- test controls
    def fill(self, order_id, price, qty=None):
        o = self.book[order_id]
        o.filled = qty if qty is not None else o.qty
        o.average_price = price
        o.status = "COMPLETE" if o.filled == o.qty else "OPEN"

    def by_type(self, order_type):
        return [o for o in self.book.values() if o.order_type == order_type]


class FakeKiteConnect:
    """The kiteconnect.KiteConnect DATA methods the app uses (instruments, quote, ohlc, historical_data, margins),
    backed by dictionaries the test fills in. Records every call so tests can check batching."""

    api_key = "fake-key"
    access_token = "fake-token"

    def __init__(self, nse: list[dict] | None = None, nfo: list[dict] | None = None, quotes: dict | None = None,
                 candles: dict | None = None):
        self.rows = {"NSE": nse or [], "NFO": nfo or []}
        self.quotes = quotes or {}                 # "NSE:RELIANCE" -> quote dict
        self.candles = candles or {}               # (token, interval) -> [{"date", "open", ...}]
        self.calls: list[tuple] = []
        self.fail: str | None = None               # name of a method that raises next time
        self.margin_rate = 0.12

    def _check(self, name):
        if self.fail == name:
            self.fail = None
            raise RuntimeError(f"{name} failed")

    def instruments(self, exchange):
        self.calls.append(("instruments", exchange))
        self._check("instruments")
        return [dict(r) for r in self.rows.get(exchange, [])]

    def quote(self, keys):
        keys = list(keys) if isinstance(keys, (list, tuple)) else [keys]
        self.calls.append(("quote", len(keys)))
        self._check("quote")
        return {k: self.quotes[k] for k in keys if k in self.quotes}

    def ohlc(self, keys):
        keys = list(keys) if isinstance(keys, (list, tuple)) else [keys]
        self.calls.append(("ohlc", len(keys)))
        self._check("ohlc")
        return {k: {"last_price": q["last_price"], "ohlc": q.get("ohlc", {})} for k, q in self.quotes.items() if k in keys}

    def historical_data(self, token, since, until, interval):
        self.calls.append(("historical", token, interval))
        self._check("historical")
        rows = self.candles.get((token, interval), [])
        a = since if isinstance(since, dt.datetime) else dt.datetime.combine(since, dt.time(0, 0))
        b = until if isinstance(until, dt.datetime) else dt.datetime.combine(until, dt.time(23, 59))
        return [r for r in rows if a <= r["date"].replace(tzinfo=None) <= b]

    def order_margins(self, orders):
        self.calls.append(("order_margins", len(orders)))
        self._check("order_margins")
        return [{"tradingsymbol": o["tradingsymbol"], "total": round(o["quantity"] * o["price"] * self.margin_rate, 2),
                 "charges": {"total": round(20 + o["quantity"] * o["price"] * 0.0005, 2)}} for o in orders]

    def basket_order_margins(self, orders, consider_positions=True, mode=None):
        self.calls.append(("basket", len(orders)))
        self._check("basket")
        gross = sum(o["quantity"] * o["price"] * self.margin_rate for o in orders)
        return {"initial": {"total": gross}, "final": {"total": round(gross * 0.5, 2)}, "orders": []}


class FakeTicker:
    """kiteconnect.KiteTicker without a socket: tests call connect() and push ticks by hand."""

    MODE_FULL, MODE_QUOTE, MODE_LTP = "full", "quote", "ltp"
    made: list["FakeTicker"] = []

    def __init__(self, api_key, access_token, **kw):
        self.kw = kw
        self.subscribed: set[int] = set()
        self.modes: dict[int, str] = {}
        self.closed = False
        self.connected = False
        self.on_ticks = self.on_connect = self.on_close = self.on_error = self.on_noreconnect = self.on_reconnect = None
        FakeTicker.made.append(self)

    def connect(self, threaded=False):
        self.connected = True
        self.on_connect(self, {})

    def subscribe(self, tokens):
        self.subscribed |= set(tokens)

    def set_mode(self, mode, tokens):
        self.modes.update({t: mode for t in tokens})

    def unsubscribe(self, tokens):
        self.subscribed -= set(tokens)

    def close(self, code=None, reason=None):
        self.closed = True

    def stop_retry(self):
        pass

    def push(self, ticks):
        self.on_ticks(self, ticks)
