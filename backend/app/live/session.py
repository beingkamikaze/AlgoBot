"""The daily Kite session: login, the encrypted token, and who is logged in.

SEBI requires a fresh two-factor login to the broker every trading day, and Kite's
access token expires at 06:00 IST the next morning. Flow:
  1. the app asks for a login URL; it carries a one-time `state` tied to you (10 minutes);
  2. you log in on kite.zerodha.com (password + 2FA there, never in this app);
  3. Kite sends your browser to /api/live/kite/callback with a request_token;
  4. the backend swaps it for an access token, encrypts it, and stores it.
The token never reaches the browser.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.core.calendar import IST
from app.db import db, eq
from app.live.broker import Broker, BrokerError, KiteBroker

STATE_TTL = 600
_states: dict[str, tuple[str, float]] = {}            # state -> (user_id, created)
_brokers: dict[str, tuple[str, Broker]] = {}          # user_id -> (token fingerprint, broker)
_lock = threading.Lock()
_factory = KiteBroker                                  # tests swap this


class LiveError(Exception):
    pass


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise LiveError("TOKEN_ENCRYPTION_KEY is not set in backend/.env")
    return Fernet(key.encode())


def encrypt(token: str) -> str:
    """bytea for PostgREST: \\x + hex of the Fernet ciphertext."""
    return "\\x" + _fernet().encrypt(token.encode()).hex()


def decrypt(stored: str) -> str:
    raw = bytes.fromhex(stored[2:] if stored.startswith("\\x") else stored)
    try:
        return _fernet().decrypt(raw).decode()
    except InvalidToken:
        raise LiveError("the stored Kite token cannot be decrypted (was TOKEN_ENCRYPTION_KEY changed?): log in to Kite again")


def expiry_after(login: dt.datetime) -> dt.datetime:
    """Kite tokens die at 06:00 IST the morning after the login."""
    t = login.astimezone(IST)
    nxt = t.date() + dt.timedelta(days=1) if t.time() >= dt.time(6, 0) else t.date()
    return dt.datetime.combine(nxt, dt.time(6, 0), IST)


def login_url(user_id: str) -> str:
    s = get_settings()
    if not s.kite_configured:
        raise LiveError("Kite is not configured: set KITE_API_KEY, KITE_API_SECRET and TOKEN_ENCRYPTION_KEY in backend/.env")
    state = secrets.token_urlsafe(24)
    with _lock:
        now = time.monotonic()
        for k in [k for k, (_, t) in _states.items() if now - t > STATE_TTL]:
            _states.pop(k, None)
        _states[state] = (user_id, now)
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={s.kite_api_key}&redirect_params={quote('state=' + state)}"


def complete_login(state: str, request_token: str, now: dt.datetime | None = None) -> str:
    """Called by the callback. Returns the user id the session now belongs to."""
    with _lock:
        hit = _states.pop(state, None)
    if not hit or time.monotonic() - hit[1] > STATE_TTL:
        raise LiveError("this Kite login link expired or was already used: start again from the Live page")
    user_id = hit[0]
    s = get_settings()
    from kiteconnect import KiteConnect
    try:
        data = KiteConnect(api_key=s.kite_api_key).generate_session(request_token, api_secret=s.kite_api_secret)
    except Exception as e:  # noqa: BLE001
        raise LiveError(f"Kite refused the login: {e}")
    store_token(user_id, data["access_token"], data.get("user_id"), now)
    return user_id


def _account(user_id: str) -> dict | None:
    rows = db().select("broker_accounts", {"user_id": eq(user_id), "broker": eq("zerodha")}, limit=1)
    return rows[0] if rows else None


def store_token(user_id: str, token: str, kite_user_id: str | None, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(IST)
    values = {"access_token_enc": encrypt(token), "token_created_at": now.astimezone(dt.timezone.utc).isoformat(),
              "token_expires_at": expiry_after(now).astimezone(dt.timezone.utc).isoformat(), "kite_user_id": kite_user_id,
              "is_live": True}
    acct = _account(user_id)
    if acct:
        db().update("broker_accounts", {"id": eq(acct["id"])}, values)
    else:
        db().insert("broker_accounts", {"user_id": user_id, "broker": "zerodha", "label": "Zerodha", **values})
    with _lock:
        _brokers.pop(user_id, None)


def logout(user_id: str) -> None:
    acct = _account(user_id)
    if acct:
        db().update("broker_accounts", {"id": eq(acct["id"])}, {"access_token_enc": None, "token_expires_at": None})
    with _lock:
        _brokers.pop(user_id, None)


def status(user_id: str, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(IST)
    acct = _account(user_id)
    if not acct or not acct.get("access_token_enc") or not acct.get("token_expires_at"):
        return {"logged_in": False, "account_id": acct["id"] if acct else None}
    exp = dt.datetime.fromisoformat(acct["token_expires_at"])
    return {"logged_in": now < exp, "account_id": acct["id"], "kite_user_id": acct.get("kite_user_id"),
            "expires_at": exp.isoformat(), "logged_in_at": acct.get("token_created_at")}


def broker_for(user_id: str, now: dt.datetime | None = None) -> Broker:
    """Today's broker for this user, or LiveError if there is no valid Kite session."""
    st = status(user_id, now)
    if not st["logged_in"]:
        raise LiveError("not logged in to Kite today")
    acct = _account(user_id)
    fp = acct["access_token_enc"][-16:]
    with _lock:
        hit = _brokers.get(user_id)
        if hit and hit[0] == fp:
            return hit[1]
    b = _factory(get_settings().kite_api_key, decrypt(acct["access_token_enc"]), get_settings().kite_algo_id)
    with _lock:
        _brokers[user_id] = (fp, b)
    return b


def any_broker() -> Broker | None:
    """Any live Kite session (single-user app): used for live prices, also for paper trading."""
    with _lock:
        cached = list(_brokers.items())
    for uid, _ in cached:
        try:
            return broker_for(uid)
        except (LiveError, BrokerError):
            continue
    try:
        rows = db().select("broker_accounts", {"broker": eq("zerodha"), "token_expires_at": f"gt.{dt.datetime.now(dt.timezone.utc).isoformat()}"},
                           columns="user_id", limit=1)
    except Exception:  # noqa: BLE001 - before migration 0005 or without Supabase: no session
        return None
    for r in rows:
        try:
            return broker_for(r["user_id"])
        except (LiveError, BrokerError):
            pass
    return None
