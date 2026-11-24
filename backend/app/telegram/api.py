"""Telegram approval routes: status, a link code for your chat, unlink, and a test message."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app.auth import User, current_user
from app.db import DBError
from app.telegram import service
from app.telegram.bot import TelegramError

router = APIRouter(prefix="/telegram", tags=["telegram"])


async def _run(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except TelegramError as e:
        raise HTTPException(409, str(e))
    except DBError as e:
        msg = str(e)
        if "telegram_links" in msg:
            msg = "the Telegram table is missing: run supabase/migrations/0010_telegram.sql in the Supabase SQL editor"
        raise HTTPException(503, f"database: {msg}")


@router.get("")
async def telegram_status(user: User = Depends(current_user)) -> dict:
    """Is a bot set up, and is your chat linked?"""
    return await _run(service.status, user.id)


@router.post("/link")
async def telegram_link(user: User = Depends(current_user)) -> dict:
    """A one-time code (10 minutes). Open the link, or send "/start <code>" to the bot, to link that chat."""
    return await _run(service.new_code, user.id)


@router.post("/unlink")
async def telegram_unlink(user: User = Depends(current_user)) -> dict:
    await _run(service.unlink, user.id)
    return {"ok": True}


@router.post("/test")
async def telegram_test(user: User = Depends(current_user)) -> dict:
    await _run(service.send_test, user.id)
    return {"ok": True}
