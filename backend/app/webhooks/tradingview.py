"""TradingView alerts -> signal cards and risk-checked tickets that wait for YOUR confirmation.

TradingView rules (tradingview.com/support/solutions/43000529348):
  - it POSTs from four IP addresses only (TV_IPS), to ports 80 or 443 only;
  - it gives up after 3 seconds, so this endpoint checks the secret, answers at once,
    and does the rest (prices, news, sizing, risk check) in the background;
  - it cannot set headers, so the secret travels in the JSON body.

Your Mac is not reachable from the internet: run a tunnel (Cloudflare Tunnel is free)
that gives an https:// address forwarding to http://localhost:8100. The tunnel passes
TradingView's address in the CF-Connecting-IP header.

Nothing here approves an order. An alert becomes a ticket in the inbox; an unreviewed
ticket expires after 15 minutes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import get_settings
from app.core.calendar import IST
from app.db import db, eq

log = logging.getLogger("webhooks")
TV_IPS = {"52.89.214.238", "34.212.75.30", "54.218.53.128", "52.32.178.7"}
RATE_PER_MINUTE = 30
DEDUPE_SECONDS = 300
INBOX_EXPIRE_MINUTES = 15
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tv-alerts")
_recent: dict[str, deque] = {}
_seen: dict[str, float] = {}
_lock = threading.Lock()
_count_lock = threading.Lock()

# TradingView tickers -> this app's symbols
_ALIASES = {"NIFTY": "NIFTY-FUT", "NIFTY50": "NIFTY-FUT", "NIFTY1!": "NIFTY-FUT", "NIFTY2!": "NIFTY-FUT",
            "BANKNIFTY": "BANKNIFTY-FUT", "BANKNIFTY1!": "BANKNIFTY-FUT", "BANKNIFTY2!": "BANKNIFTY-FUT", "CNXNIFTY": "NIFTY-FUT"}


class WebhookError(Exception):
    def __init__(self, status: int, message: str, outcome: str = "rejected"):
        super().__init__(message)
        self.status, self.outcome = status, outcome


class Alert(BaseModel):
    secret: str = Field(min_length=10, max_length=200)
    symbol: str = Field(min_length=1, max_length=40)                 # {{ticker}}, e.g. RELIANCE, NSE:RELIANCE, NIFTY1!
    side: Literal["buy", "sell", "long", "short"]
    stop: float = Field(gt=0)                                        # required: no stop, no ticket
    target: float | None = Field(default=None, gt=0)
    price: float | None = Field(default=None, gt=0)                  # {{close}}
    strategy: str = Field(default="tradingview", max_length=60)
    note: str = Field(default="", max_length=500)                    # why, in your words
    alert_id: str | None = Field(default=None, max_length=80)
    time: str | None = Field(default=None, max_length=40)            # {{timenow}}

    @field_validator("side")
    @classmethod
    def norm_side(cls, v: str) -> str:
        return "buy" if v in ("buy", "long") else "sell"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def app_symbol(tv_ticker: str) -> str:
    s = tv_ticker.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.replace(".NS", "")
    if s in _ALIASES:
        return _ALIASES[s]
    if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", s):
        raise WebhookError(422, f"cannot map TradingView ticker {tv_ticker!r} to an NSE symbol")
    return s


def client_ip(headers: dict, peer: str | None) -> str | None:
    """Behind a Cloudflare tunnel the peer is 127.0.0.1 and TradingView's address is in CF-Connecting-IP."""
    for h in ("cf-connecting-ip", "x-real-ip"):
        if headers.get(h):
            return headers[h].strip()
    if headers.get("x-forwarded-for"):
        return headers["x-forwarded-for"].split(",")[0].strip()
    return peer


def _rate_ok(source_id: str) -> bool:
    now = time.monotonic()
    with _lock:
        q = _recent.setdefault(source_id, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MINUTE:
            return False
        q.append(now)
        return True


def _dupe(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        for k in [k for k, t in _seen.items() if now - t > DEDUPE_SECONDS]:
            _seen.pop(k, None)
        if key in _seen:
            return True
        _seen[key] = now
        return False


def _event(source: dict, ip: str | None, payload: dict, outcome: str, detail: str = "", **ids) -> None:
    try:
        safe = {k: v for k, v in payload.items() if k != "secret"}           # the secret is never stored
        db().insert("webhook_events", {"source_id": source["id"], "user_id": source["user_id"], "remote_ip": ip,
                                       "payload": safe, "outcome": outcome, "detail": detail[:500], **ids})
        col = "alerts_received" if outcome == "accepted" else "alerts_rejected"
        with _count_lock:                                                     # read the current count, not a stale copy
            now = db().select("webhook_sources", {"id": eq(source["id"])}, columns=col)
            db().update("webhook_sources", {"id": eq(source["id"])},
                        {col: int((now[0] if now else {}).get(col) or 0) + 1,
                         "last_seen_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    except Exception as e:  # noqa: BLE001 - logging an event must never fail the webhook
        log.warning("webhook event log failed: %s", e)


# ---------------------------------------------------------------- sources (your settings)

def create_source(user_id: str, name: str) -> dict:
    secret = secrets.token_urlsafe(24)
    row = db().insert("webhook_sources", {"user_id": user_id, "name": name.strip()[:60], "secret_hash": hash_secret(secret)})[0]
    return {"id": row["id"], "name": row["name"], "secret": secret, "path": f"/api/webhooks/tradingview/{row['id']}",
            "message_template": message_template(secret, name)}


def message_template(secret: str, name: str) -> str:
    """What to paste into TradingView's alert "Message" box. Replace the stop and target with your Pine values."""
    return json.dumps({"secret": secret, "symbol": "{{ticker}}", "side": "buy", "price": "{{close}}",
                       "stop": 0, "target": 0, "strategy": name[:60], "note": "why this alert fired",
                       "time": "{{timenow}}"}, indent=2).replace('"{{close}}"', "{{close}}")


def list_sources(user_id: str) -> list[dict]:
    return db().select("webhook_sources", {"user_id": eq(user_id)},
                       columns="id,name,enabled,created_at,last_seen_at,alerts_received,alerts_rejected", order="created_at")


def set_enabled(user_id: str, source_id: str, enabled: bool) -> dict:
    out = db().update("webhook_sources", {"id": eq(source_id), "user_id": eq(user_id)}, {"enabled": enabled})
    if not out:
        raise WebhookError(404, "no such source")
    return out[0]


# ---------------------------------------------------------------- receiving

def receive(source_id: str, body: dict, headers: dict, peer: str | None, trusted: bool = False) -> dict:
    """Validate fast, queue the work, answer. `trusted` is the in-app "send a test alert" button."""
    rows = db().select("webhook_sources", {"id": eq(source_id)}) if re.fullmatch(r"[0-9a-f-]{36}", source_id) else []
    if not rows or not rows[0]["enabled"]:
        raise WebhookError(404, "unknown or disabled webhook")
    source = rows[0]
    ip = client_ip(headers, peer)
    if not trusted:
        if get_settings().webhook_require_tradingview_ip and ip not in TV_IPS:
            _event(source, ip, body, "rejected", "not from a TradingView address")
            raise WebhookError(403, "not from a TradingView address")
        if not hmac.compare_digest(hash_secret(str(body.get("secret", ""))), source["secret_hash"]):
            _event(source, ip, body, "rejected", "wrong secret")
            raise WebhookError(401, "wrong secret")
    if not _rate_ok(source_id):
        _event(source, ip, body, "rate_limited", f"more than {RATE_PER_MINUTE} alerts a minute")
        raise WebhookError(429, "too many alerts", "rate_limited")
    try:
        alert = Alert(**{**body, "secret": body.get("secret") if not trusted else "trusted-test-alert"})
        symbol = app_symbol(alert.symbol)
    except WebhookError as e:
        _event(source, ip, body, "rejected", str(e))
        raise
    except ValidationError as e:                                              # never echo the input: it holds the secret
        problems = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'message'}: {x['msg']}" for x in e.errors(include_input=False))
        _event(source, ip, body, "rejected", f"bad alert message: {problems}"[:300])
        raise WebhookError(422, f"bad alert message: {problems}"[:300])
    except Exception:  # noqa: BLE001
        _event(source, ip, body, "rejected", "bad alert message")
        raise WebhookError(422, "bad alert message: check the JSON in TradingView's Message box")
    key = alert.alert_id or f"{source_id}|{symbol}|{alert.side}|{alert.stop}|{alert.price}|{alert.time}"
    if _dupe(key):
        _event(source, ip, body, "duplicate", "same alert within 5 minutes")
        return {"accepted": False, "duplicate": True}
    _pool.submit(_process, source, alert, symbol, ip, body)
    return {"accepted": True}


def _process(source: dict, alert: Alert, symbol: str, ip: str | None, body: dict) -> None:
    from app.paper import service
    from app.signals.external import card_for_alert
    try:
        card = card_for_alert(source, alert, symbol)
        order = service.propose(source["user_id"], None, symbol=symbol, side=alert.side, stop=alert.stop,
                                target=alert.target, source="tradingview", card=card)
        _event(source, ip, body, "accepted", f"ticket {order['risk_verdict']['verdict']}",
               signal_id=order.get("signal_id"), order_id=order["id"])
    except Exception as e:  # noqa: BLE001 - every failure is visible in the events list
        log.exception("processing a TradingView alert failed")
        _event(source, ip, body, "rejected", f"could not make a ticket: {e}"[:300])


def inbox(user_id: str) -> dict:
    d = db()
    orders = d.select("orders", {"user_id": eq(user_id), "mode": eq("paper"), "params->>source": eq("tradingview")},
                      order="created_at.desc", limit=30)
    events = d.select("webhook_events", {"user_id": eq(user_id)}, order="received_at.desc", limit=50,
                      columns="id,source_id,received_at,remote_ip,payload,outcome,detail,signal_id,order_id")
    sigs = {}
    ids = ",".join(o["signal_id"] for o in orders if o.get("signal_id"))
    if ids:
        sigs = {s["id"]: s for s in d.select("signals", {"id": f"in.({ids})"}, columns="id,explanation,created_at")}
    return {"tickets": [{**o, "explanation": (sigs.get(o.get("signal_id")) or {}).get("explanation")} for o in orders],
            "events": events}


def expire_stale_tickets(now: dt.datetime) -> int:
    """Alert tickets nobody reviewed within INBOX_EXPIRE_MINUTES expire: a stale alert is a different trade."""
    cutoff = (now - dt.timedelta(minutes=INBOX_EXPIRE_MINUTES)).astimezone(dt.timezone.utc).isoformat()
    d = db()
    stale = d.select("orders", {"state": eq("proposed"), "params->>source": eq("tradingview"), "created_at": f"lt.{cutoff}"},
                     columns="id")
    for o in stale:
        d.update("orders", {"id": eq(o["id"])}, {"state": "expired", "updated_at": now.astimezone(dt.timezone.utc).isoformat()})
    return len(stale)


__all__ = ["receive", "create_source", "list_sources", "set_enabled", "inbox", "expire_stale_tickets", "WebhookError",
           "TV_IPS", "IST"]
