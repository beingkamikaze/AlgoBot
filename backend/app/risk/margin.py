"""Exact margin and charges from Kite's margin APIs when you are logged in to Kite today; None otherwise, and the
caller keeps the app's estimate (`Instrument.margin_pct`, `app.core.costs`). Every result says which was used.

  - one order: Kite's order-margins API for the entry, plus the exit at the stop for the round-trip charges;
  - an option structure: Kite's basket-margins API (the hedge benefit counts), and the order-margins API on the
    entry legs and the same legs closed at the same prices for the round-trip charges.
"""

from __future__ import annotations

import logging

from app.market import kite as K

log = logging.getLogger("margin")


def _order(exchange: str, tradingsymbol: str, side: str, qty: int, price: float, product: str = "MIS") -> dict:
    return {"exchange": exchange, "tradingsymbol": tradingsymbol, "transaction_type": side.upper(), "variety": "regular",
            "product": product, "order_type": "LIMIT", "quantity": int(qty), "price": float(price), "trigger_price": 0}


def _charges(rows: list[dict]) -> float | None:
    vals = [(r.get("charges") or {}).get("total") for r in rows]
    return round(sum(float(v) for v in vals), 2) if vals and all(v is not None for v in vals) else None


def for_order(exchange: str, tradingsymbol: str, side: str, qty: int, price: float, stop: float | None = None) -> dict | None:
    """{"margin", "charges" (round trip at the stop, or None), "source": "kite"}, or None without a Kite login."""
    kd = K.current()
    if kd is None or qty <= 0:
        return None
    orders = [_order(exchange, tradingsymbol, side, qty, price)]
    if stop is not None:
        orders.append(_order(exchange, tradingsymbol, "sell" if side == "buy" else "buy", qty, stop))
    try:
        rows = kd.order_margins(orders)
    except K.KiteDataError as e:
        log.info("Kite margin for %s failed, using the estimate: %s", tradingsymbol, e)
        return None
    if not rows or rows[0].get("total") is None:
        return None
    return {"margin": round(float(rows[0]["total"]), 2), "charges": _charges(rows) if stop is not None else None,
            "source": "kite"}


def for_basket(legs: list[dict]) -> dict | None:
    """legs: {exchange, tradingsymbol, side, qty, price}. {"margin", "charges" (round trip), "source": "kite"} or None."""
    kd = K.current()
    if kd is None or not legs:
        return None
    entry = [_order(lg.get("exchange", "NFO"), lg["tradingsymbol"], lg["side"], lg["qty"], lg["price"]) for lg in legs]
    closing = [{**o, "transaction_type": "SELL" if o["transaction_type"] == "BUY" else "BUY"} for o in entry]
    try:
        basket = kd.basket_margins(entry)
        rows = kd.order_margins(entry + closing)
    except K.KiteDataError as e:
        log.info("Kite basket margin failed, using the estimate: %s", e)
        return None
    total = ((basket.get("final") or {}).get("total") if isinstance(basket, dict) else None)
    if total is None:
        return None
    return {"margin": round(float(total), 2), "charges": _charges(rows), "source": "kite"}
