"""A fake of the MetaTrader5 Python package for one gold symbol: the same function names, constants (MT5's real values) and
namedtuple results, with a tiny broker behind them. Pending limits fill when price reaches them, positions close at their
stop or target, and order_send refuses what a broker would (volume, price, stops, market closed, algo trading off).

Test helpers: move(bid, ask) to change the price and run fills; fail_next = <retcode> to refuse the next request.
"""

from __future__ import annotations

import itertools
from collections import namedtuple

Account = namedtuple("Account", "login server name currency trade_mode balance equity margin_free leverage trade_allowed trade_expert")
Symbol = namedtuple("Symbol", "name digits point trade_contract_size volume_min volume_step volume_max trade_stops_level filling_mode trade_mode")
Tick = namedtuple("Tick", "time bid ask last")
Order = namedtuple("Order", "ticket time_setup type magic volume_current price_open sl tp comment symbol time_expiration type_time")
Position = namedtuple("Position", "ticket identifier time type magic volume price_open sl tp profit swap comment symbol")
Deal = namedtuple("Deal", "ticket order position_id time type entry magic volume price profit commission swap fee reason comment symbol")
Result = namedtuple("Result", "retcode deal order volume price comment request_id")


class FakeMT5:
    TRADE_ACTION_DEAL, TRADE_ACTION_PENDING, TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE = 1, 5, 6, 7, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_TIME_GTC, ORDER_TIME_DAY, ORDER_TIME_SPECIFIED = 0, 1, 2
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
    DEAL_REASON_EXPERT, DEAL_REASON_SL, DEAL_REASON_TP = 3, 4, 5
    TRADE_RETCODE_REQUOTE, TRADE_RETCODE_PLACED, TRADE_RETCODE_DONE = 10004, 10008, 10009
    TRADE_RETCODE_INVALID, TRADE_RETCODE_INVALID_VOLUME, TRADE_RETCODE_INVALID_PRICE = 10013, 10014, 10015
    TRADE_RETCODE_INVALID_STOPS, TRADE_RETCODE_MARKET_CLOSED, TRADE_RETCODE_CLIENT_DISABLES_AT = 10016, 10018, 10027

    def __init__(self, *, symbol="XAUUSD", bid=4000.0, ask=4000.3, trade_mode=0, contract_size=100.0, volume_min=0.01,
                 volume_step=0.01, volume_max=50.0, digits=2, stops_level=0, filling_mode=2, balance=10_000.0):
        self.symbol, self.bid, self.ask, self.trade_mode = symbol, bid, ask, trade_mode
        self.spec = Symbol(symbol, digits, 10 ** -digits, contract_size, volume_min, volume_step, volume_max, stops_level,
                           filling_mode, 4)
        self.balance = balance
        self.time = 1_795_000_000
        self.connected = False
        self.algo_trading = True
        self.market_open = True
        self.fail_next: int | None = None
        self.orders: dict[int, dict] = {}
        self.positions: dict[int, dict] = {}
        self.deals: list[dict] = []
        self.sent: list[dict] = []
        self._ids = itertools.count(500_001)

    # ------------------------------------------------------------ the package's functions

    def initialize(self, **kw) -> bool:
        self.connected = True
        return True

    def shutdown(self) -> None:
        self.connected = False

    def last_error(self):
        return (1, "Success") if self.connected else (-10004, "No IPC connection")

    def account_info(self):
        if not self.connected:
            return None
        eq = self.balance + sum(p["profit"] for p in self.positions.values())
        return Account(5_012_345, "Demo-Server", "Test", "USD", self.trade_mode, self.balance, eq, eq, 100, True, True)

    def symbol_select(self, name, enable=True) -> bool:
        return name == self.symbol

    def symbol_info(self, name):
        return self.spec if name == self.symbol else None

    def symbol_info_tick(self, name):
        return Tick(self.time, self.bid, self.ask, 0.0) if name == self.symbol else None

    def orders_get(self, symbol=None):
        return tuple(Order(**o) for o in self.orders.values() if symbol in (None, o["symbol"]))

    def positions_get(self, symbol=None):
        return tuple(Position(**{**p, "profit": self._profit(p)}) for p in self.positions.values() if symbol in (None, p["symbol"]))

    def history_deals_get(self, date_from, date_to):
        lo, hi = date_from.timestamp(), date_to.timestamp()
        return tuple(Deal(**d) for d in self.deals if lo <= d["time"] <= hi)

    def order_send(self, req: dict):
        self.sent.append(dict(req))
        if self.fail_next:
            code, self.fail_next = self.fail_next, None
            return self._res(code, comment="refused")
        if not self.algo_trading:
            return self._res(self.TRADE_RETCODE_CLIENT_DISABLES_AT, comment="AutoTrading disabled by client")
        a = req["action"]
        if a in (self.TRADE_ACTION_DEAL, self.TRADE_ACTION_PENDING) and not self.market_open:
            return self._res(self.TRADE_RETCODE_MARKET_CLOSED, comment="Market closed")
        if a == self.TRADE_ACTION_PENDING:
            return self._pending(req)
        if a == self.TRADE_ACTION_DEAL:
            return self._close(req) if req.get("position") else self._open(req)
        if a == self.TRADE_ACTION_REMOVE:
            if self.orders.pop(req["order"], None) is None:
                return self._res(self.TRADE_RETCODE_INVALID, comment="Invalid request")
            return self._res(self.TRADE_RETCODE_DONE, order=req["order"])
        if a == self.TRADE_ACTION_SLTP:
            p = self.positions.get(req["position"])
            if p is None:
                return self._res(self.TRADE_RETCODE_INVALID, comment="Invalid request")
            long = p["type"] == self.POSITION_TYPE_BUY
            if not self._stops_ok(long, self.bid if long else self.ask, req.get("sl") or 0, req.get("tp") or 0):
                return self._res(self.TRADE_RETCODE_INVALID_STOPS, comment="Invalid stops")
            p.update(sl=req.get("sl") or 0.0, tp=req.get("tp") or 0.0)
            return self._res(self.TRADE_RETCODE_DONE)
        return self._res(self.TRADE_RETCODE_INVALID, comment="Invalid request")

    # ------------------------------------------------------------ test helpers

    def move(self, bid: float, ask: float | None = None, seconds: int = 60) -> None:
        """New price: pending limits fill, expired orders go, then stops and targets are checked."""
        self.bid, self.ask = bid, ask if ask is not None else round(bid + 0.3, 2)
        self.time += seconds
        for t, o in list(self.orders.items()):
            if o["type_time"] == self.ORDER_TIME_SPECIFIED and o["time_expiration"] and self.time >= o["time_expiration"]:
                del self.orders[t]
                continue
            buy = o["type"] == self.ORDER_TYPE_BUY_LIMIT
            if (self.ask <= o["price_open"]) if buy else (self.bid >= o["price_open"]):
                del self.orders[t]
                self._new_position(t, buy, o["volume_current"], o["price_open"], o["sl"], o["tp"], o["magic"], o["comment"])
        for t, p in list(self.positions.items()):
            long = p["type"] == self.POSITION_TYPE_BUY
            px = self.bid if long else self.ask
            if p["sl"] and ((px <= p["sl"]) if long else (px >= p["sl"])):
                self._exit(p, p["sl"], self.DEAL_REASON_SL, f"[sl {p['sl']:.2f}]")
            elif p["tp"] and ((px >= p["tp"]) if long else (px <= p["tp"])):
                self._exit(p, p["tp"], self.DEAL_REASON_TP, f"[tp {p['tp']:.2f}]")

    def manual_position(self, side: str = "buy", volume: float = 0.1) -> int:
        """A position you opened by hand in the terminal (magic 0): the bridge must not see or touch it."""
        t = next(self._ids)
        self._new_position(t, side == "buy", volume, self.ask, 0.0, 0.0, 0, "")
        return t

    # ------------------------------------------------------------ the tiny broker

    def _res(self, code, *, deal=0, order=0, volume=0.0, price=0.0, comment=""):
        return Result(code, deal, order, volume, price, comment, 1)

    def _profit(self, p: dict, price: float | None = None) -> float:
        long = p["type"] == self.POSITION_TYPE_BUY
        px = price if price is not None else (self.bid if long else self.ask)
        return round((px - p["price_open"]) * (1 if long else -1) * p["volume"] * self.spec.trade_contract_size, 2)

    def _volume_ok(self, v: float) -> bool:
        s = self.spec
        steps = v / s.volume_step
        return s.volume_min - 1e-9 <= v <= s.volume_max + 1e-9 and abs(steps - round(steps)) < 1e-6

    def _price_ok(self, p: float) -> bool:
        return abs(p * 10 ** self.spec.digits - round(p * 10 ** self.spec.digits)) < 1e-6

    def _stops_ok(self, long: bool, ref: float, sl: float, tp: float) -> bool:
        gap = self.spec.trade_stops_level * self.spec.point
        if sl and ((sl >= ref - gap) if long else (sl <= ref + gap)):
            return False
        if tp and ((tp <= ref + gap) if long else (tp >= ref - gap)):
            return False
        return True

    def _pending(self, r: dict):
        buy = r["type"] == self.ORDER_TYPE_BUY_LIMIT
        if not self._volume_ok(r["volume"]):
            return self._res(self.TRADE_RETCODE_INVALID_VOLUME, comment="Invalid volume")
        if not self._price_ok(r["price"]) or ((r["price"] >= self.ask) if buy else (r["price"] <= self.bid)):
            return self._res(self.TRADE_RETCODE_INVALID_PRICE, comment="Invalid price")
        if not self._stops_ok(buy, r["price"], r.get("sl", 0), r.get("tp", 0)):
            return self._res(self.TRADE_RETCODE_INVALID_STOPS, comment="Invalid stops")
        t = next(self._ids)
        self.orders[t] = {"ticket": t, "time_setup": self.time, "type": r["type"], "magic": r.get("magic", 0),
                          "volume_current": r["volume"], "price_open": r["price"], "sl": r.get("sl", 0.0),
                          "tp": r.get("tp", 0.0), "comment": r.get("comment", "")[:31], "symbol": r["symbol"],
                          "time_expiration": r.get("expiration", 0), "type_time": r.get("type_time", 0)}
        return self._res(self.TRADE_RETCODE_PLACED, order=t, volume=r["volume"], price=r["price"])

    def _open(self, r: dict):
        buy = r["type"] == self.ORDER_TYPE_BUY
        market = self.ask if buy else self.bid
        if not self._volume_ok(r["volume"]):
            return self._res(self.TRADE_RETCODE_INVALID_VOLUME, comment="Invalid volume")
        if abs(market - r["price"]) > r.get("deviation", 0) * self.spec.point + 1e-9:
            return self._res(self.TRADE_RETCODE_REQUOTE, comment="Requote")
        if not self._stops_ok(buy, market, r.get("sl", 0), r.get("tp", 0)):
            return self._res(self.TRADE_RETCODE_INVALID_STOPS, comment="Invalid stops")
        t = next(self._ids)
        deal = self._new_position(t, buy, r["volume"], market, r.get("sl", 0.0), r.get("tp", 0.0), r.get("magic", 0),
                                  r.get("comment", "")[:31])
        return self._res(self.TRADE_RETCODE_DONE, deal=deal, order=t, volume=r["volume"], price=market)

    def _close(self, r: dict):
        p = self.positions.get(r["position"])
        if p is None:
            return self._res(self.TRADE_RETCODE_INVALID, comment="Position not found")
        long = p["type"] == self.POSITION_TYPE_BUY
        market = self.bid if long else self.ask
        if abs(market - r["price"]) > r.get("deviation", 0) * self.spec.point + 1e-9:
            return self._res(self.TRADE_RETCODE_REQUOTE, comment="Requote")
        deal = self._exit(p, market, self.DEAL_REASON_EXPERT, r.get("comment", ""))
        return self._res(self.TRADE_RETCODE_DONE, deal=deal, order=next(self._ids), volume=p["volume"], price=market)

    def _deal(self, **kw) -> int:
        t = next(self._ids)
        self.deals.append({"ticket": t, "time": self.time, "commission": 0.0, "swap": 0.0, "fee": 0.0,
                           "symbol": self.symbol, **kw})
        return t

    def _new_position(self, ticket, buy, volume, price, sl, tp, magic, comment) -> int:
        self.positions[ticket] = {"ticket": ticket, "identifier": ticket, "time": self.time,
                                  "type": self.POSITION_TYPE_BUY if buy else self.POSITION_TYPE_SELL, "magic": magic,
                                  "volume": volume, "price_open": price, "sl": sl or 0.0, "tp": tp or 0.0, "profit": 0.0,
                                  "swap": 0.0, "comment": comment, "symbol": self.symbol}
        return self._deal(order=ticket, position_id=ticket, type=self.DEAL_TYPE_BUY if buy else self.DEAL_TYPE_SELL,
                          entry=self.DEAL_ENTRY_IN, magic=magic, volume=volume, price=price, profit=0.0,
                          reason=self.DEAL_REASON_EXPERT, comment=comment)

    def _exit(self, p: dict, price: float, reason: int, comment: str) -> int:
        profit = self._profit(p, price)
        self.balance += profit
        del self.positions[p["ticket"]]
        long = p["type"] == self.POSITION_TYPE_BUY
        return self._deal(order=p["ticket"], position_id=p["ticket"], type=self.DEAL_TYPE_SELL if long else self.DEAL_TYPE_BUY,
                          entry=self.DEAL_ENTRY_OUT, magic=p["magic"], volume=p["volume"], price=price, profit=profit,
                          reason=reason, comment=comment)

