"""Supabase Auth for the API: the browser sends its Supabase access token, and the
backend asks Supabase who it belongs to. Works with both the newer asymmetric
signing keys and the legacy JWT secret, because Supabase does the checking."""

from __future__ import annotations

import base64
import json
import time

import httpx
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.config import Settings, get_settings


class User(BaseModel):
    id: str
    email: str | None = None
    aal: str | None = None          # "aal2" once you signed in with two-factor (needed for live trading)


def _claims(token: str) -> dict:
    """The token's payload. Only read AFTER Supabase has confirmed the token is valid."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


_seen: dict[str, tuple[float, "User"]] = {}   # token -> (checked at, user): ask Supabase at most once a minute


async def current_user(authorization: str = Header(default=""),
                       settings: Settings = Depends(get_settings)) -> User:
    if not settings.supabase_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Supabase is not configured: set SUPABASE_URL and SUPABASE_ANON_KEY in backend/.env")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    hit = _seen.get(token)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
                             headers={"Authorization": f"Bearer {token}", "apikey": settings.supabase_anon_key})
    if r.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired session")
    data = r.json()
    allowed = settings.allowed_email_list
    if allowed and (data.get("email") or "").lower() not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this account may not use this app")
    user = User(id=data["id"], email=data.get("email"), aal=_claims(token).get("aal"))
    if len(_seen) > 500:
        _seen.clear()
    _seen[token] = (time.monotonic(), user)
    return user


async def optional_user(authorization: str = Header(default=""), settings: Settings = Depends(get_settings)) -> User | None:
    """The signed-in user if there is a valid session, else None (for pages that work signed out too)."""
    if not authorization or not settings.supabase_configured:
        return None
    try:
        return await current_user(authorization, settings)
    except HTTPException:
        return None
