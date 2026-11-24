"""Every condition that must hold before a real-money order is sent. All of them, every time.

Exits are never gated: closing a live position always goes through.
"""

from __future__ import annotations

import datetime as dt

from app.config import get_settings
from app.core.calendar import IST, market_status
from app.db import db, eq
from app.live import session


def _today(now: dt.datetime) -> str:
    return now.astimezone(IST).date().isoformat()


def live_limits_row(user_id: str) -> dict:
    rows = db().select("risk_limits", {"user_id": eq(user_id)})
    return rows[0] if rows else {}


def check(user_id: str, aal: str | None, now: dt.datetime | None = None, *, for_arming: bool = False) -> list[dict]:
    """[{gate, ok, detail}] in the order you would fix them."""
    now = now or dt.datetime.now(IST)
    s = get_settings()
    row = live_limits_row(user_id)
    migrated = "live_max_loss_per_trade" in row
    sess = session.status(user_id, now) if s.kite_configured and migrated else {"logged_in": False}
    gates = [
        {"gate": "server", "ok": s.live_trading_enabled,
         "detail": "LIVE_TRADING_ENABLED=true in backend/.env" if not s.live_trading_enabled else "live trading is enabled on this server"},
        {"gate": "kite_app", "ok": s.kite_configured,
         "detail": "KITE_API_KEY, KITE_API_SECRET and TOKEN_ENCRYPTION_KEY set" if s.kite_configured else
         "set KITE_API_KEY, KITE_API_SECRET and TOKEN_ENCRYPTION_KEY in backend/.env"},
        {"gate": "database", "ok": migrated,
         "detail": "live limits exist" if migrated else "run supabase/migrations/0005_live.sql"},
        {"gate": "kite_login", "ok": bool(sess.get("logged_in")),
         "detail": f"logged in to Kite as {sess.get('kite_user_id')} until 06:00" if sess.get("logged_in") else
         "log in to Kite today (SEBI requires a fresh login every trading day)"},
        {"gate": "two_factor", "ok": aal == "aal2",
         "detail": "signed in to this app with two-factor" if aal == "aal2" else
         "turn on two-factor sign-in for this app and verify your code"},
    ]
    if not for_arming:
        armed = migrated and str(row.get("live_armed_on") or "") == _today(now)
        gates.append({"gate": "armed", "ok": armed,
                      "detail": "armed for today" if armed else "arm live trading for today on the Live page"})
        gates.append({"gate": "market", "ok": market_status(now).is_open,
                      "detail": "market open" if market_status(now).is_open else "the market is closed"})
    return gates


def all_ok(gates: list[dict]) -> bool:
    return all(g["ok"] for g in gates)


def blocking(gates: list[dict]) -> list[str]:
    return [g["detail"] for g in gates if not g["ok"]]
