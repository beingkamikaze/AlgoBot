"""Telegram approval: linking a chat, ideas and tickets with buttons, two taps to confirm, and only your chat counts."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

from app.global_markets import paper
from app.global_markets.agent import desk
from app.telegram import bot as bot_mod
from app.telegram import service

from .test_global_agent import NOW, FakeModel, both, buy, fdb, look  # noqa: F401 - fixtures used by name

ME, STRANGER = 42, 99


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, list | None]] = []
        self.edits: list[tuple[int, int, str, list | None]] = []
        self.answers: list[tuple[str, str]] = []

    def me(self):
        return {"username": "gold_desk_bot"}

    def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((chat_id, message_id, text, buttons))

    def answer(self, callback_id, text=""):
        self.answers.append((callback_id, text))


@pytest.fixture
def tg(fdb, monkeypatch):  # noqa: F811
    b = FakeBot()
    monkeypatch.setattr(service, "db", lambda: fdb)
    monkeypatch.setattr(service, "get_bot", lambda: b)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(telegram_bot_token="123:abc"))
    monkeypatch.setitem(service.state, "bot_username", "gold_desk_bot")
    while not service._outbox.empty():
        service._outbox.get_nowait()
    return b


def say(text: str, chat: int = ME, now: dt.datetime = NOW) -> None:
    service.handle({"message": {"chat": {"id": chat, "type": "private"}, "from": {"id": chat, "username": "me"},
                                "text": text}}, now)


def tap(data: str, chat: int = ME, now: dt.datetime = NOW) -> None:
    service.handle({"callback_query": {"id": f"cb-{data}", "from": {"id": chat}, "data": data,
                                       "message": {"message_id": 7, "chat": {"id": chat, "type": "private"},
                                                   "text": "earlier message"}}}, now)


def linked(b: FakeBot) -> None:
    code = service.new_code("u1", NOW)["code"]
    say(f"/start {code}")
    b.sent.clear()


def test_a_chat_links_only_with_a_fresh_code_from_the_app(tg, fdb):  # noqa: F811
    c = service.new_code("u1", NOW)
    assert c["link"] == f"https://t.me/gold_desk_bot?start={c['code']}" and len(c["code"]) == 10
    say("/start wrongcode")
    assert "wrong or has expired" in tg.sent[-1][1] and not service.status("u1")["linked"]
    say(f"/start {c['code']}", now=NOW + dt.timedelta(minutes=11))                      # 10 minutes only
    assert "expired" in tg.sent[-1][1] and not service.status("u1")["linked"]
    c = service.new_code("u1", NOW)
    say(f"/start {c['code']}")
    st = service.status("u1")
    assert st["linked"] and st["username"] == "me" and "Linked" in tg.sent[-1][1]
    say(f"/start {c['code']}", chat=STRANGER)                                            # a code works once
    assert service._user_for(STRANGER) is None
    say("/stop")
    assert not service.status("u1")["linked"]


def test_only_live_trade_ideas_are_sent_with_buttons(tg, both):  # noqa: F811
    linked(tg)
    idea = look(NOW, FakeModel(strategist=buy(4002.9)))
    assert idea["state"] == "new"
    service.on_idea(idea)
    service.on_idea({**idea, "state": "vetoed"})
    service.on_idea({**idea, "kind": "pass"})
    assert service.flush() == 1
    chat, text, buttons = tg.sent[-1]
    assert chat == ME and "BUY XAUUSD" in text and "Graded <b>low</b>" in text and "untested" in text
    assert buttons == [[("Make paper ticket", f"t:{idea['id']}"), ("Skip", f"s:{idea['id']}")]]


def test_the_desk_tells_its_listeners_and_a_broken_listener_does_not_stop_a_look(tg, both, monkeypatch):  # noqa: F811
    got = []
    monkeypatch.setattr(desk, "listeners", [lambda i: 1 / 0, got.append])
    idea = look(NOW, FakeModel(strategist=buy(4002.9)))
    assert idea and got and got[0]["id"] == idea["id"]


def test_idea_to_trade_takes_three_taps_and_the_risk_check_runs_again(tg, both):  # noqa: F811
    fdb, clock = both
    linked(tg)
    idea = look(NOW, FakeModel(strategist=buy(4002.9)))
    later = NOW + dt.timedelta(minutes=1)
    clock["now"] = later
    tap(f"t:{idea['id']}", now=later)
    order = fdb.select("global_orders")[0]
    assert order["state"] == "proposed" and order["params"]["idea_id"] == idea["id"]
    chat, text, buttons = tg.sent[-1]
    assert "Paper ticket: BUY" in text and "Risk check: <b>PASS</b>" in text
    assert buttons == [[("Confirm paper trade", f"c:{order['id']}"), ("Reject", f"r:{order['id']}")]]
    tap(f"c:{order['id']}", now=later)                                                  # first tap only asks again
    assert fdb.select("global_orders")[0]["state"] == "proposed"
    assert tg.edits[-1][3] == [[("Yes, take it (paper)", f"y:{order['id']}"), ("No", f"r:{order['id']}")]]
    tap(f"y:{order['id']}", now=later)
    assert fdb.select("global_orders")[0]["state"] == "working"
    assert "Confirmed (paper)" in tg.edits[-1][2] and tg.edits[-1][3] == []
    tap(f"y:{order['id']}", now=later)                                                  # a second yes does nothing
    assert "Not done" in tg.edits[-1][2] and len(fdb.select("global_orders")) == 1


def test_a_stranger_cannot_confirm_and_a_reject_places_nothing(tg, both):  # noqa: F811
    fdb, clock = both
    linked(tg)
    idea = look(NOW, FakeModel(strategist=buy(4002.9)))
    clock["now"] = NOW + dt.timedelta(minutes=1)
    tap(f"t:{idea['id']}", now=clock["now"])
    oid = fdb.select("global_orders")[0]["id"]
    tap(f"y:{oid}", chat=STRANGER, now=clock["now"])
    assert fdb.select("global_orders")[0]["state"] == "proposed" and "not linked" in tg.answers[-1][1]
    tap(f"r:{oid}", now=clock["now"])
    assert fdb.select("global_orders")[0]["state"] == "rejected" and "Nothing was placed" in tg.edits[-1][2]


def test_an_expired_idea_cannot_become_a_ticket_from_the_phone(tg, both):  # noqa: F811
    fdb, _ = both
    linked(tg)
    idea = look(NOW, FakeModel(strategist=buy(4002.9)))
    tap(f"t:{idea['id']}", now=NOW + dt.timedelta(minutes=11))
    assert not fdb.select("global_orders") and "expired" in tg.edits[-1][2] and tg.edits[-1][3] == []


def test_a_blocked_ticket_can_only_be_rejected_on_the_phone():
    o = {"id": "o1", "side": "buy", "lots": 0.01, "symbol": "XAUUSD", "params": {"limit": 1, "stop": 0.5, "target": 2},
         "risk_verdict": {"verdict": "BLOCK", "reasons": ["outside your entry window"], "numbers": {"max_loss": 5}}}
    assert service._ticket_buttons(o) == [[("Reject", "r:o1")]]
    assert "only on the app's Paper page" in service.ticket_text(o)


def test_fills_and_exits_go_only_to_the_trader_s_chat(tg, fdb):  # noqa: F811
    linked(tg)
    fdb.insert(service.TABLE, {"user_id": "u2", "chat_id": STRANGER})
    service.on_paper("filled", "u1", {"symbol": "XAUUSD", "side": "buy", "lots": 0.02, "price": 4003.1, "stop": 3999,
                                      "target": 4011})
    service.on_paper("closed", "u1", {"symbol": "XAUUSD", "side": "buy", "price": 4011, "reason": "target", "net": 14.2,
                                      "r": 1.8})
    service.flush()
    assert [c for c, _, _ in tg.sent] == [ME, ME]
    assert "Filled (paper)" in tg.sent[0][1] and "+1.80R" in tg.sent[1][1]


def test_paper_tells_its_listeners_when_a_trade_closes(monkeypatch):
    got = []
    monkeypatch.setattr(paper, "listeners", [lambda *a: 1 / 0, lambda *a: got.append(a)])
    paper._tell("closed", "u1", {"x": 1})
    assert got == [("closed", "u1", {"x": 1})]


def test_the_bot_token_never_shows_in_an_error():
    def fail(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")
    b = bot_mod.Bot("123:SECRET", client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(bot_mod.TelegramError) as e:
        b.me()
    assert "SECRET" not in str(e.value) and "***" in str(e.value)
    ok = bot_mod.Bot("123:SECRET", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": False, "description": "Unauthorized"}))))
    with pytest.raises(bot_mod.TelegramError, match="Unauthorized"):
        ok.me()
