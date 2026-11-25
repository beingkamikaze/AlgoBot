"""The MT5 bridge: a small web service that runs on the WINDOWS machine next to the MetaTrader 5 terminal, and is the only
code that touches the MetaTrader5 Python package (it works only on Windows).

The app never talks to MT5 directly. It calls this bridge over HTTP with a secret token, in plain words ("buy_limit",
"close"), and the bridge turns them into MT5 requests. The bridge is deliberately narrow:

  - ONE symbol (BRIDGE_SYMBOL) and ONE magic number: it only sees and touches the app's own orders and positions, never
    anything you placed by hand;
  - entries are LIMITS with a stop and a target. A limit still away from the market rests as a pending order; a limit
    already through the market (the app's usual "marketable limit") goes as a deal that MT5 may fill only up to the limit
    (its "deviation"), never worse. Then: cancelling, moving a position's stop/target, and closing a position;
  - DEMO accounts only, unless BRIDGE_ALLOW_REAL=1 (and the app has its own separate guard). Live gold trading waits for your
    lawyer's answer;
  - it refuses to start without a token of at least 24 characters, and listens on 127.0.0.1 unless told otherwise.

Run on Windows, from the backend folder, with the MT5 terminal open and logged in to the demo account:
    pip install MetaTrader5 fastapi uvicorn
    set BRIDGE_TOKEN=<a long random secret, the same as MT5_BRIDGE_TOKEN in backend/.env>
    set BRIDGE_SYMBOL=XAUUSD          (your broker's name for gold: XAUUSD, XAUUSD.m, GOLD, ...)
    python -m app.global_markets.mt5.bridge
This file imports nothing else from the app, so it runs without the app's other requirements.
"""

from __future__ import annotations

import datetime as dt
import hmac
import os
import threading
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

MAGIC = 2611250                     # marks the app's orders inside MT5
DEFAULT_DEVIATION = 30              # points of slippage allowed when closing (30 points = $0.30 on 2-digit gold)
TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}


class Entry(BaseModel):
    side: str = Field(pattern="^(buy|sell)$")
    volume: float = Field(gt=0)
    price: float = Field(gt=0)
    sl: float = Field(gt=0)
    tp: float = Field(gt=0)
    comment: str = Field(min_length=1, max_length=31)
    expires_at: int | None = None   # unix seconds: the broker cancels the order then


class Stops(BaseModel):
    sl: float | None = None
    tp: float | None = None


class Close(BaseModel):
    deviation: int = Field(DEFAULT_DEVIATION, ge=0, le=500)
    comment: str = Field("close", max_length=31)


def _d(x) -> dict:
    return x._asdict() if hasattr(x, "_asdict") else dict(x)


def create_app(mt5, *, token: str, symbol: str, allow_real: bool = False) -> FastAPI:
    """`mt5` is the MetaTrader5 module (or the tests' fake of it)."""
    if len(token) < 24:
        raise SystemExit("BRIDGE_TOKEN must be at least 24 characters")
    app = FastAPI(title="MT5 bridge", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()           # the MetaTrader5 package is not safe to call from two threads at once

    def auth(x_bridge_token: str = Header(default="")) -> None:
        if not hmac.compare_digest(x_bridge_token, token):
            raise HTTPException(401, "wrong bridge token")

    def connect() -> None:
        if mt5.account_info() is None and not mt5.initialize():
            raise HTTPException(503, f"MT5 terminal not reachable: {mt5.last_error()}")
        if not mt5.symbol_select(symbol, True):
            raise HTTPException(503, f"symbol {symbol} is not available in this MT5 account")

    def account() -> dict:
        a = mt5.account_info()
        if a is None:
            raise HTTPException(503, f"MT5 has no account logged in: {mt5.last_error()}")
        a = _d(a)
        return {"login": a["login"], "server": a.get("server"), "name": a.get("name"), "currency": a.get("currency"),
                "trade_mode": TRADE_MODES.get(a.get("trade_mode"), "unknown"), "balance": a.get("balance"),
                "equity": a.get("equity"), "margin_free": a.get("margin_free"), "leverage": a.get("leverage"),
                "trade_allowed": bool(a.get("trade_allowed")), "trade_expert": bool(a.get("trade_expert", True))}

    def guard_trade() -> None:
        acct = account()
        if acct["trade_mode"] != "demo" and not allow_real:
            raise HTTPException(403, f"this is a {acct['trade_mode']} account: the bridge trades demo accounts only "
                                     "(BRIDGE_ALLOW_REAL is not set)")

    def spec() -> dict:
        s = mt5.symbol_info(symbol)
        if s is None:
            raise HTTPException(503, f"no symbol info for {symbol}")
        s = _d(s)
        return {"symbol": symbol, "digits": s["digits"], "point": s["point"], "contract_size": s["trade_contract_size"],
                "volume_min": s["volume_min"], "volume_step": s["volume_step"], "volume_max": s["volume_max"],
                "stops_level": s.get("trade_stops_level", 0), "filling_mode": s.get("filling_mode", 0),
                "trade_mode": s.get("trade_mode")}

    def result(r) -> dict:
        if r is None:
            code, msg = mt5.last_error()
            return {"ok": False, "retcode": code, "message": f"MT5 did not answer: {msg}"}
        r = _d(r)
        ok = r["retcode"] in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
        return {"ok": ok, "retcode": r["retcode"], "message": r.get("comment") or "", "order": r.get("order") or None,
                "deal": r.get("deal") or None, "price": r.get("price") or None, "volume": r.get("volume") or None}

    def ours(rows) -> list[dict]:
        return [_d(x) for x in (rows or ()) if _d(x).get("magic") == MAGIC]

    def call(fn):
        with lock:
            connect()
            return fn()

    @app.get("/health", dependencies=[Depends(auth)])
    def health() -> dict:
        return call(lambda: {"symbol": symbol, "account": account(), "magic": MAGIC, "allow_real": allow_real})

    @app.get("/symbol", dependencies=[Depends(auth)])
    def symbol_info() -> dict:
        return call(spec)

    @app.get("/quote", dependencies=[Depends(auth)])
    def quote() -> dict:
        def go():
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                raise HTTPException(503, f"no price for {symbol}: {mt5.last_error()}")
            t = _d(t)
            return {"symbol": symbol, "bid": t["bid"], "ask": t["ask"], "time": t["time"]}
        return call(go)

    @app.get("/positions", dependencies=[Depends(auth)])
    def positions() -> list[dict]:
        def go():
            return [{"ticket": p["ticket"], "side": "buy" if p["type"] == mt5.POSITION_TYPE_BUY else "sell",
                     "volume": p["volume"], "price_open": p["price_open"], "sl": p["sl"] or None, "tp": p["tp"] or None,
                     "profit": p["profit"], "swap": p.get("swap", 0.0), "comment": p.get("comment") or "", "time": p["time"]}
                    for p in ours(mt5.positions_get(symbol=symbol))]
        return call(go)

    @app.get("/orders", dependencies=[Depends(auth)])
    def orders() -> list[dict]:
        names = {mt5.ORDER_TYPE_BUY_LIMIT: "buy_limit", mt5.ORDER_TYPE_SELL_LIMIT: "sell_limit"}

        def go():
            return [{"ticket": o["ticket"], "type": names.get(o["type"], str(o["type"])), "volume": o["volume_current"],
                     "price": o["price_open"], "sl": o["sl"] or None, "tp": o["tp"] or None, "comment": o.get("comment") or "",
                     "expires_at": o.get("time_expiration") or None, "time": o["time_setup"]}
                    for o in ours(mt5.orders_get(symbol=symbol))]
        return call(go)

    @app.get("/deals", dependencies=[Depends(auth)])
    def deals(since: int) -> list[dict]:
        def go():
            start = dt.datetime.fromtimestamp(since, dt.timezone.utc)
            end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
            rows = [d for d in ours(mt5.history_deals_get(start, end)) if d.get("symbol") == symbol]
            reasons = {mt5.DEAL_REASON_SL: "stop", mt5.DEAL_REASON_TP: "target"}
            return [{"ticket": d["ticket"], "order": d["order"], "position": d["position_id"], "time": d["time"],
                     "entry": "in" if d["entry"] == mt5.DEAL_ENTRY_IN else "out",
                     "side": "buy" if d["type"] == mt5.DEAL_TYPE_BUY else "sell", "volume": d["volume"], "price": d["price"],
                     "profit": d["profit"], "commission": d.get("commission", 0.0), "swap": d.get("swap", 0.0),
                     "fee": d.get("fee", 0.0), "reason": reasons.get(d.get("reason"), "app"), "comment": d.get("comment") or ""}
                    for d in rows]
        return call(go)

    def filling() -> int:
        fill = spec()["filling_mode"]
        return mt5.ORDER_FILLING_IOC if fill & 2 else mt5.ORDER_FILLING_FOK if fill & 1 else mt5.ORDER_FILLING_RETURN

    @app.post("/orders", dependencies=[Depends(auth)])
    def place(e: Entry) -> dict:
        def go():
            guard_trade()
            t = _d(mt5.symbol_info_tick(symbol))
            buy = e.side == "buy"
            through = e.price >= t["ask"] if buy else e.price <= t["bid"]
            req: dict[str, Any] = {"symbol": symbol, "volume": e.volume, "sl": e.sl, "tp": e.tp, "magic": MAGIC,
                                   "comment": e.comment, "type_time": mt5.ORDER_TIME_GTC}
            if through:                     # marketable: fill now at the market, at most up to the limit
                market = t["ask"] if buy else t["bid"]
                room = int(round(abs(e.price - market) / spec()["point"]))
                req.update(action=mt5.TRADE_ACTION_DEAL, price=market, deviation=room, type_filling=filling(),
                           type=mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL)
            else:
                req.update(action=mt5.TRADE_ACTION_PENDING, price=e.price, type_filling=mt5.ORDER_FILLING_RETURN,
                           type=mt5.ORDER_TYPE_BUY_LIMIT if buy else mt5.ORDER_TYPE_SELL_LIMIT)
                if e.expires_at:
                    req.update(type_time=mt5.ORDER_TIME_SPECIFIED, expiration=e.expires_at)
            return result(mt5.order_send(req))
        return call(go)

    @app.post("/orders/{ticket}/cancel", dependencies=[Depends(auth)])
    def cancel(ticket: int) -> dict:
        def go():
            if not any(o["ticket"] == ticket for o in ours(mt5.orders_get(symbol=symbol))):
                raise HTTPException(404, "no such pending order of the app's")
            return result(mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}))
        return call(go)

    def _position(ticket: int) -> dict:
        for p in ours(mt5.positions_get(symbol=symbol)):
            if p["ticket"] == ticket:
                return p
        raise HTTPException(404, "no such open position of the app's")

    @app.post("/positions/{ticket}/stops", dependencies=[Depends(auth)])
    def stops(ticket: int, s: Stops) -> dict:
        def go():
            p = _position(ticket)
            return result(mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": ticket,
                                          "sl": s.sl if s.sl is not None else p["sl"],
                                          "tp": s.tp if s.tp is not None else p["tp"], "magic": MAGIC}))
        return call(go)

    @app.post("/positions/{ticket}/close", dependencies=[Depends(auth)])
    def close(ticket: int, c: Close) -> dict:
        # getting out is never refused for the account type: a demo-only bridge must still be able to close
        def go():
            p = _position(ticket)
            t = _d(mt5.symbol_info_tick(symbol))
            long = p["type"] == mt5.POSITION_TYPE_BUY
            return result(mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "position": ticket, "volume": p["volume"],
                "type": mt5.ORDER_TYPE_SELL if long else mt5.ORDER_TYPE_BUY, "price": t["bid"] if long else t["ask"],
                "deviation": c.deviation, "magic": MAGIC, "comment": c.comment, "type_filling": filling(),
                "type_time": mt5.ORDER_TIME_GTC}))
        return call(go)

    return app


def main() -> None:                                          # pragma: no cover - runs on Windows next to MT5
    import MetaTrader5 as mt5
    import uvicorn
    app = create_app(mt5, token=os.environ.get("BRIDGE_TOKEN", ""), symbol=os.environ.get("BRIDGE_SYMBOL", "XAUUSD"),
                     allow_real=os.environ.get("BRIDGE_ALLOW_REAL") == "1")
    if not mt5.initialize():
        raise SystemExit(f"could not start MT5: {mt5.last_error()}. Open the terminal and log in to the demo account.")
    uvicorn.run(app, host=os.environ.get("BRIDGE_HOST", "127.0.0.1"), port=int(os.environ.get("BRIDGE_PORT", "8710")))


if __name__ == "__main__":                                    # pragma: no cover
    main()
