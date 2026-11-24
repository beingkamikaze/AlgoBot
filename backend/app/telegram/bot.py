"""A small client for the Telegram Bot API (https://core.telegram.org/bots/api).

The bot token sits in the URL of every call, so this module never logs a URL, keeps httpx's request log quiet, and
scrubs the token from any error before it is raised.
"""

from __future__ import annotations

import logging

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx logs each request URL at INFO, token included

API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class Bot:
    def __init__(self, token: str, client: httpx.Client | None = None):
        if not token:
            raise TelegramError("no bot token: put TELEGRAM_BOT_TOKEN in backend/.env")
        self._token = token
        self._http = client or httpx.Client(timeout=httpx.Timeout(10.0, read=40.0))

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***")

    def call(self, method: str, **params) -> object:
        try:
            r = self._http.post(f"{API}/bot{self._token}/{method}", json={k: v for k, v in params.items() if v is not None})
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(self._scrub(f"{method}: {type(e).__name__}: {e}")) from None
        if not body.get("ok"):
            raise TelegramError(self._scrub(f"{method}: {body.get('description') or r.status_code}"))
        return body.get("result")

    def me(self) -> dict:
        return self.call("getMe")  # type: ignore[return-value]

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])  # type: ignore[return-value]

    def send(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True,
                         reply_markup=keyboard(buttons))  # type: ignore[return-value]

    def edit(self, chat_id: int, message_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                      disable_web_page_preview=True, reply_markup=keyboard(buttons))
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190] or None)
        except TelegramError:
            pass                       # the tap is old (Telegram allows about 15 s to answer): the message still updates


def keyboard(buttons: list[list[tuple[str, str]]] | None) -> dict | None:
    """[[(label, data), ...], ...] -> an inline keyboard. An empty list removes the buttons from an edited message."""
    if buttons is None:
        return None
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
