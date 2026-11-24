"""Telegram approval: the gold agent's ideas and your paper tickets on your phone, confirmed with a tap.

Linking: the app gives you a one-time code (10 minutes); you send it to the bot (the app's link opens the chat with
"/start <code>"). Only that private chat is linked to your account. Anyone else who messages the bot is told to link
from the app, and their taps do nothing.

Flow, the same steps as the web page:
  agent idea  -> message with [Make paper ticket] [Skip]
  Make ticket -> the risk check prices and sizes it now -> ticket with [Confirm paper trade] [Reject] (or only [Reject]
                 when the check blocks it: the paper override stays on the web page, behind hold-to-confirm)
  Confirm     -> "are you sure?" with [Yes, take it (paper)] [No]: two taps, so a stray tap never trades
  Yes         -> paper.approve, which runs the risk check again on a fresh price
Fills and exits of your paper trades are sent too. Nothing here sizes, approves or places anything by itself: every
step is your tap, and every tap is checked again against the account it belongs to.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import queue
import secrets
import string
import threading

from app.config import get_settings
from app.db import DBError, db, eq
from app.global_markets import paper
from app.global_markets.agent import desk
from app.global_markets.clock import IST
from app.telegram.bot import Bot, TelegramError

log = logging.getLogger("telegram")
TABLE = "telegram_links"
CODE_MINUTES = 10
CODE_CHARS = string.ascii_letters + string.digits

state: dict = {"status": "not started", "error": None, "bot_username": None}
_outbox: "queue.Queue[tuple[int, str, list | None]]" = queue.Queue()
_bot: Bot | None = None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat()


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(get_settings().telegram_bot_token)
    return _bot


def configured() -> bool:
    return bool(get_settings().telegram_bot_token)


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def _ist(iso: str | None) -> str:
    return dt.datetime.fromisoformat(iso).astimezone(IST).strftime("%H:%M") if iso else ""


def _usd(x) -> str:
    return "–" if x is None else f"${float(x):,.2f}"


# ---------------------------------------------------------------- linking

def status(user_id: str) -> dict:
    rows = db().select(TABLE, {"user_id": eq(user_id)})
    r = rows[0] if rows else {}
    return {"configured": configured(), "bot_username": state.get("bot_username"), "linked": bool(r.get("chat_id")),
            "username": r.get("username"), "linked_at": r.get("linked_at"), "loop": state.get("status"),
            "error": state.get("error")}


def new_code(user_id: str, now: dt.datetime | None = None) -> dict:
    """A one-time link code for your account. Sending it to the bot links that chat."""
    now = now or _now()
    if not configured():
        raise TelegramError("Telegram is not set up: put TELEGRAM_BOT_TOKEN in backend/.env and restart the backend")
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(10))
    until = now + dt.timedelta(minutes=CODE_MINUTES)
    d, row = db(), {"link_code": code, "code_expires_at": _iso(until), "updated_at": _iso(now)}
    if d.select(TABLE, {"user_id": eq(user_id)}):
        d.update(TABLE, {"user_id": eq(user_id)}, row)             # a linked chat stays linked until the code is used
    else:
        d.insert(TABLE, {"user_id": user_id, **row})
    name = state.get("bot_username")
    return {"code": code, "expires_at": _iso(until), "bot_username": name,
            "link": f"https://t.me/{name}?start={code}" if name else None}


def unlink(user_id: str) -> None:
    db().update(TABLE, {"user_id": eq(user_id)}, {"chat_id": None, "username": None, "linked_at": None, "link_code": None,
                                                 "updated_at": _iso(_now())})


def _claim(code: str, chat_id: int, username: str | None, now: dt.datetime) -> bool:
    d = db()
    rows = d.select(TABLE, {"link_code": eq(code)})
    if not rows or not rows[0].get("code_expires_at") or rows[0]["code_expires_at"] < _iso(now):
        return False
    for other in d.select(TABLE, {"chat_id": eq(chat_id)}):          # one chat links one account
        if other["user_id"] != rows[0]["user_id"]:
            d.update(TABLE, {"user_id": eq(other["user_id"])}, {"chat_id": None, "linked_at": None})
    d.update(TABLE, {"user_id": eq(rows[0]["user_id"])}, {"chat_id": chat_id, "username": username, "linked_at": _iso(now),
                                                         "link_code": None, "code_expires_at": None, "updated_at": _iso(now)})
    return True


def _user_for(chat_id: int) -> str | None:
    rows = db().select(TABLE, {"chat_id": eq(chat_id)})
    return rows[0]["user_id"] if rows else None


def _chats(user_id: str | None = None) -> list[int]:
    f = {"chat_id": "not.is.null"}
    if user_id:
        f["user_id"] = eq(user_id)
    return [int(r["chat_id"]) for r in db().select(TABLE, f)]


# ---------------------------------------------------------------- what the messages say

def idea_text(idea: dict) -> str:
    p = idea.get("proposal") or {}
    n = p.get("plan") or {}
    r = idea.get("review") or {}
    late = idea.get("price_source") == "futures"
    lines = [f"<b>Gold agent idea: {esc(idea['side']).upper()} XAUUSD</b> ({esc(idea.get('setup') or 'other')})",
             f"Price seen {n.get('entry')} · stop {n.get('stop')} · target {n.get('target')} · reward:risk "
             f"{n.get('reward_risk')} : 1", "", "<b>Why</b>"]
    lines += [f"• {esc(w)}" for w in p.get("why") or []]
    if p.get("wrong_if"):
        lines.append(f"Wrong if: {esc(p['wrong_if'])}")
    if r.get("verdict"):
        lines += ["", f"<b>Reviewer: {esc(r['verdict'])}</b>. {esc(r.get('summary') or '')}"]
        lines += [f"• {esc(c)}" for c in r.get("concerns") or []]
    lines += ["", "Graded <b>low</b>: the agent made this up from the live market, it is untested."
              + (" Prices are COMEX futures about 10 minutes late." if late else ""),
              f"Open until {_ist(idea.get('expires_at'))} IST. A ticket is priced and sized when you ask for it."]
    return "\n".join(lines)


def ticket_text(o: dict) -> str:
    p, v = o.get("params") or {}, o.get("risk_verdict") or {}
    nums = v.get("numbers") or {}
    lines = [f"<b>Paper ticket: {esc(o['side']).upper()} {o['lots']:g} lots {esc(o['symbol'])}</b>",
             f"Limit {p.get('limit')} · stop {p.get('stop')} · target {p.get('target')}",
             f"Max loss {_usd(nums.get('max_loss'))} at the stop, including costs", "",
             f"Risk check: <b>{esc(v.get('verdict'))}</b>"]
    lines += [f"• {esc(x)}" for x in v.get("reasons") or []]
    lines += [f"• note: {esc(x)}" for x in v.get("warnings") or []]
    if v.get("verdict") != "PASS":
        lines += ["", "It cannot be confirmed here. A paper override, if your rules allow one, is only on the app's "
                      "Paper page (hold to confirm, recorded as rules not followed)."]
    lines.append(f"The ticket expires {paper.TICKET_MINUTES} minutes after it was made.")
    return "\n".join(lines)


def _ticket_buttons(o: dict) -> list[list[tuple[str, str]]]:
    if (o.get("risk_verdict") or {}).get("verdict") == "PASS":
        return [[("Confirm paper trade", f"c:{o['id']}"), ("Reject", f"r:{o['id']}")]]
    return [[("Reject", f"r:{o['id']}")]]


def _sure_text(o: dict) -> str:
    p, nums = o.get("params") or {}, (o.get("risk_verdict") or {}).get("numbers") or {}
    return (f"<b>Confirm on paper?</b>\n{esc(o['side']).upper()} {o['lots']:g} lots {esc(o['symbol'])}, limit {p.get('limit')}, "
            f"stop {p.get('stop')}, target {p.get('target')}.\nMax loss {_usd(nums.get('max_loss'))}. The risk check runs "
            "again on a fresh price when you say yes.")


# ---------------------------------------------------------------- your taps

def handle(update: dict, now: dt.datetime | None = None) -> None:
    now = now or _now()
    if "callback_query" in update:
        _tap(update["callback_query"], now)
    elif "message" in update:
        _message(update["message"], now)


def _reply(chat_id: int, text: str, buttons=None) -> None:
    get_bot().send(chat_id, text, buttons)


def _message(m: dict, now: dt.datetime) -> None:
    chat = m.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id, text = int(chat["id"]), (m.get("text") or "").strip()
    if text.startswith("/start"):
        code = text[6:].strip()
        if code and _claim(code, chat_id, (m.get("from") or {}).get("username"), now):
            _reply(chat_id, "Linked. Gold agent ideas and your paper tickets will come here, and you can confirm them "
                            "with a tap. Paper only. Send /stop to unlink.")
        elif code:
            _reply(chat_id, "That code is wrong or has expired. Make a new one on the app's XAUUSD agent page.")
        else:
            _reply(chat_id, "To link this chat, open the app's XAUUSD agent page and press Connect Telegram.")
    elif text == "/stop":
        uid = _user_for(chat_id)
        if uid:
            unlink(uid)
        _reply(chat_id, "Unlinked. Nothing more will be sent here.")
    else:
        if _user_for(chat_id):
            _reply(chat_id, "Ideas and tickets arrive here with buttons. Send /stop to unlink this chat.")
        else:
            _reply(chat_id, "This chat is not linked. Open the app's XAUUSD agent page and press Connect Telegram.")


def _tap(q: dict, now: dt.datetime) -> None:
    bot = get_bot()
    msg = q.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or (q.get("from") or {}).get("id"))
    mid = msg.get("message_id")
    uid = _user_for(chat_id)
    if not uid or int((q.get("from") or {}).get("id", 0)) != chat_id:
        bot.answer(q["id"], "This chat is not linked to the app.")
        return
    action, _, ref = (q.get("data") or "").partition(":")
    old = msg.get("text") or ""
    try:
        if action == "t":                                    # make a ticket from an idea
            o = desk.ticket(uid, ref, now)
            bot.answer(q["id"], "Ticket made")
            bot.edit(chat_id, mid, esc(old) + "\n\n→ Ticket made (below).", [])
            bot.send(chat_id, ticket_text(o), _ticket_buttons(o))
        elif action == "s":                                  # skip an idea
            bot.answer(q["id"], "Skipped")
            bot.edit(chat_id, mid, esc(old) + "\n\n→ Skipped.", [])
        elif action == "c":                                  # first tap on confirm: ask again
            o = _order(uid, ref)
            bot.answer(q["id"])
            bot.edit(chat_id, mid, _sure_text(o), [[("Yes, take it (paper)", f"y:{ref}"), ("No", f"r:{ref}")]])
        elif action == "y":                                  # your confirmation
            o = paper.approve(uid, ref, now=now)
            if o["state"] == "working":
                p = o["params"]
                bot.answer(q["id"], "Confirmed")
                bot.edit(chat_id, mid, f"<b>Confirmed (paper).</b> {esc(o['side']).upper()} {o['lots']:g} lots "
                                       f"{esc(o['symbol'])} is working at limit {p.get('limit')}, stop {p.get('stop')}, "
                                       f"target {p.get('target')}. You'll get a message when it fills.", [])
            else:
                reasons = "\n".join(f"• {esc(x)}" for x in (o.get("risk_verdict") or {}).get("reasons") or [])
                bot.answer(q["id"], "Blocked")
                bot.edit(chat_id, mid, f"<b>Blocked at confirmation</b> on a fresh price. Nothing was placed.\n{reasons}", [])
        elif action == "r":                                  # reject a ticket
            paper.cancel(uid, ref)
            bot.answer(q["id"], "Rejected")
            bot.edit(chat_id, mid, esc(old) + "\n\n→ Rejected. Nothing was placed.", [])
        else:
            bot.answer(q["id"], "Unknown button")
    except (desk.AgentError, paper.PaperError) as e:
        bot.answer(q["id"], str(e))
        bot.edit(chat_id, mid, esc(old) + f"\n\n→ Not done: {esc(e)}", [])


def _order(user_id: str, order_id: str) -> dict:
    rows = db().select("global_orders", {"id": eq(order_id), "user_id": eq(user_id)})
    if not rows:
        raise paper.PaperError("no such order")
    if rows[0]["state"] != "proposed":
        raise paper.PaperError(f"this ticket is {rows[0]['state']}, not waiting for your confirmation")
    return rows[0]


# ---------------------------------------------------------------- what the app tells you

def on_idea(idea: dict) -> None:
    """The agent stored an idea. Live trade ideas go to every linked chat (the agent is not tied to one account)."""
    if idea.get("kind") != "trade" or idea.get("state") != "new" or not configured():
        return
    for chat in _chats():
        _outbox.put((chat, idea_text(idea), [[("Make paper ticket", f"t:{idea['id']}"), ("Skip", f"s:{idea['id']}")]]))


def on_paper(kind: str, user_id: str, data: dict) -> None:
    """A fill, an exit or an expired order on your paper account."""
    if not configured():
        return
    if kind == "filled":
        text = (f"<b>Filled (paper):</b> {esc(data['side']).upper()} {data['lots']:g} lots {esc(data['symbol'])} at "
                f"{data['price']}. Stop {data.get('stop')}, target {data.get('target')}.")
    elif kind == "closed":
        r = data.get("r")
        text = (f"<b>Closed (paper):</b> {esc(data['side']).upper()} {esc(data['symbol'])} at {data['price']} "
                f"({esc(data['reason'])}). Net {_usd(data.get('net'))}" + (f", {r:+.2f}R." if r is not None else "."))
    elif kind == "expired":
        text = f"Paper order expired without a fill: {esc(data.get('why') or '')}."
    else:
        return
    for chat in _chats(user_id):
        _outbox.put((chat, text, None))


def send_test(user_id: str) -> None:
    chats = _chats(user_id)
    if not chats:
        raise TelegramError("no Telegram chat is linked to your account")
    for c in chats:
        get_bot().send(c, "Test from TradingBot: this chat is linked. Ideas and tickets will arrive here.")


def flush(limit: int = 50, first: tuple | None = None) -> int:
    """Send what is waiting (starting with `first`, already taken off the queue). Returns how many went out."""
    sent = 0
    while sent < limit:
        if first is not None:
            (chat, text, buttons), first = first, None
        else:
            try:
                chat, text, buttons = _outbox.get_nowait()
            except queue.Empty:
                break
        try:
            get_bot().send(chat, text, buttons)
            sent += 1
        except TelegramError as e:
            log.warning("telegram send failed: %s", e)
            state.update(error=str(e)[:300])
    return sent


# ---------------------------------------------------------------- background threads

class TelegramLoop:
    """Two threads: one long-polls Telegram for your messages and taps, one sends what the app has to tell you."""

    def __init__(self):
        self._stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for name, fn in (("telegram-poll", self._poll), ("telegram-send", self._send)):
            t = threading.Thread(target=fn, daemon=True, name=name)
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def _send(self) -> None:
        while not self._stop.is_set():
            try:
                item = _outbox.get(timeout=2)
            except queue.Empty:
                continue
            flush(first=item)

    def _poll(self) -> None:
        offset = None
        while not self._stop.is_set():
            try:
                if not state.get("bot_username"):
                    state["bot_username"] = get_bot().me().get("username")
                state.update(status="listening")
                for u in get_bot().updates(offset):
                    offset = u["update_id"] + 1
                    try:
                        handle(u)
                    except (TelegramError, DBError) as e:
                        log.warning("telegram update failed: %s", e)
                        state.update(error=str(e)[:300])
                state.update(error=None)
            except Exception as e:  # noqa: BLE001 - keep listening; the page says why
                msg = str(e)[:300]
                if msg != state.get("error"):
                    log.warning("telegram poll failed: %s", msg)
                state.update(status="error", error=msg)
                self._stop.wait(10)
