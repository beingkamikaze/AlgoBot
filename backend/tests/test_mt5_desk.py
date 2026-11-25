"""The gold desk routed to an MT5 demo account: confirm -> the bridge -> the fake MT5 -> sync records the broker's fills."""

from __future__ import annotations

import datetime as dt

import pytest

from app.global_markets import paper
from app.global_markets.mt5 import desk as mt5

from .test_global_agent import NOW, both, fdb  # noqa: F401 - fixtures used by name
from .test_mt5 import rig

LATER = NOW + dt.timedelta(minutes=1)


@pytest.fixture
def demo(both, monkeypatch):  # noqa: F811
    db_, clock = both
    clock["now"] = LATER
    fake, http, b = rig()
    fake.time = int(LATER.timestamp())
    monkeypatch.setattr(mt5, "db", lambda: db_)
    monkeypatch.setattr(mt5, "broker", lambda: b)
    monkeypatch.setattr(mt5, "configured", lambda: True)
    told = []
    monkeypatch.setattr(paper, "listeners", [lambda *a: told.append(a)])
    monkeypatch.setattr(paper, "on_trade_closed", lambda trade_id: None)    # the post-mortem has its own tests
    paper.set_route("u1", "mt5_demo")
    return db_, fake, http, b, told


def ticket(db_, fake, side="buy", stop_off=4.0, target_off=8.0, at=LATER, ask_gap=0.1):
    """A ticket priced from the app's feed, with the broker's ask just inside its limit (a marketable limit)."""
    px = paper.price_info("XAUUSD", "spot", at).last
    long = side == "buy"
    o = paper.propose("u1", symbol="XAUUSD", side=side, stop=px - stop_off if long else px + stop_off,
                      target=px + target_off if long else px - target_off, why="test", now=at)
    lim = o["params"]["limit"]
    if long:
        fake.bid, fake.ask = round(lim - ask_gap - 0.3, 2), round(lim - ask_gap, 2)
    else:
        fake.bid, fake.ask = round(lim + ask_gap, 2), round(lim + ask_gap + 0.3, 2)
    return o


def test_a_confirmed_ticket_fills_at_mt5_and_the_target_is_the_broker_s(demo):
    db_, fake, _, b, told = demo
    o = ticket(db_, fake)
    assert o["risk_verdict"]["verdict"] == "PASS"
    w = paper.approve("u1", o["id"], now=LATER)
    p = w["params"]
    assert w["state"] == "working" and p["broker"] == "mt5" and p["mt5"]["state"] == "filled"
    assert len(fake.positions) == 1
    assert paper.tick(LATER)["filled"] == 0                                # the simulator leaves MT5 orders alone
    assert mt5.sync(LATER)["filled"] == 1
    t = db_.select("global_trades")[0]
    assert t["mode"] == "demo" and t["entry"] == fake.deals[0]["price"] and t["entry_context"]["mt5_position"]
    assert db_.select("global_orders")[0]["state"] == "filled" and told[-1][0] == "filled" and told[-1][2]["where"] == "MT5 demo"
    fake.move(p["target"] + 0.1)                                          # the broker's target, not the app's simulator
    assert mt5.sync(LATER + dt.timedelta(minutes=5))["closed"] == 1
    t = db_.select("global_trades")[0]
    assert t["exit_reason"] == "target" and t["exit"] == p["target"] and t["net_pnl"] == fake.deals[-1]["profit"]
    assert t["r_multiple"] > 1 and told[-1][0] == "closed" and mt5.state["mismatches"] == []


def test_demo_trades_count_toward_the_same_daily_limits(demo):
    db_, fake, _, _, _ = demo
    paper.approve("u1", ticket(db_, fake)["id"], now=LATER)
    mt5.sync(LATER)
    acct, _ = paper.ensure_account("u1")
    st, _ = paper.account_state("u1", acct, LATER)
    assert st.entries_today == 1 and len(st.open_positions) == 1


def test_no_override_on_the_mt5_route(demo):
    db_, fake, _, _, _ = demo
    o = ticket(db_, fake)
    with pytest.raises(paper.PaperError, match="override is for the simulator only"):
        paper.approve("u1", o["id"], override=True, reason="x", now=LATER)
    assert fake.sent == []


def test_an_mt5_refusal_rejects_the_order_and_nothing_is_placed(demo):
    db_, fake, _, _, _ = demo
    o = ticket(db_, fake)
    fake.market_open = False
    r = paper.approve("u1", o["id"], now=LATER)
    assert r["state"] == "rejected" and "market is closed" in r["params"]["mt5_error"] and not fake.positions


def test_a_resting_order_waits_then_expires_at_mt5(demo):
    db_, fake, _, _, _ = demo
    o = ticket(db_, fake, ask_gap=-1.0)                                    # the broker's ask is above the limit: it rests
    w = paper.approve("u1", o["id"], now=LATER)
    assert w["params"]["mt5"]["state"] == "pending"
    assert mt5.sync(LATER)["filled"] == 0 and db_.select("global_orders")[0]["state"] == "working"
    fake.move(fake.bid + 2, seconds=11 * 60)                               # past the 10-minute life: MT5 removes it
    assert mt5.sync(LATER + dt.timedelta(minutes=11))["expired"] == 1
    assert "expiry" in db_.select("global_orders")[0]["params"]["expired_because"]


def test_a_lost_answer_waits_before_saying_the_order_never_arrived(demo):
    db_, fake, http, _, _ = demo
    o = ticket(db_, fake)
    fake.market_open, http.lose, http.lose_path = False, "after", "/orders"   # refused at MT5, and the answer is lost
    w = paper.approve("u1", o["id"], now=LATER)
    assert w["state"] == "working" and w["params"]["mt5"]["state"] == "unknown"
    assert mt5.sync(LATER + dt.timedelta(seconds=30))["expired"] == 0      # too soon to say
    assert mt5.sync(LATER + dt.timedelta(minutes=3))["expired"] == 1
    assert db_.select("global_orders")[0]["params"]["expired_because"] == "the order never reached MT5"


def test_closing_from_the_app_closes_at_mt5_and_keeps_your_reason(demo):
    db_, fake, _, _, _ = demo
    paper.approve("u1", ticket(db_, fake)["id"], now=LATER)
    mt5.sync(LATER)
    t = db_.select("global_trades")[0]
    paper.close_position("u1", t["id"], reason="manual", now=LATER)
    assert not fake.positions                                              # out at the broker at once
    mt5.sync(LATER + dt.timedelta(seconds=20))
    t = db_.select("global_trades")[0]
    assert t["exit_reason"] == "manual" and t["exit_ts"]
    assert [o["state"] for o in db_.select("global_orders") if o["leg"] == "exit"] == ["filled"]


def test_flat_by_closes_the_demo_trade(demo):
    db_, fake, _, _, _ = demo
    paper.approve("u1", ticket(db_, fake)["id"], now=LATER)
    mt5.sync(LATER)
    mt5.sync(LATER + dt.timedelta(seconds=10), flat_by_for=lambda t: int(LATER.timestamp()))
    assert not fake.positions
    mt5.sync(LATER + dt.timedelta(seconds=20))
    assert db_.select("global_trades")[0]["exit_reason"] == "flat-by"


def test_the_kill_switch_cancels_a_resting_order_at_mt5(demo):
    db_, fake, _, _, _ = demo
    paper.approve("u1", ticket(db_, fake, ask_gap=-1.0)["id"], now=LATER)
    assert len(fake.orders) == 1
    assert paper.kill_switch("u1", now=LATER)["cancelled_orders"] == 1 and not fake.orders


def test_the_kill_switch_closes_a_demo_position_at_mt5(demo):
    db_, fake, _, _, _ = demo
    paper.approve("u1", ticket(db_, fake)["id"], now=LATER)
    mt5.sync(LATER)
    assert paper.kill_switch("u1", now=LATER)["closing_positions"] == 1 and not fake.positions
    mt5.sync(LATER + dt.timedelta(seconds=10))
    assert db_.select("global_trades")[0]["exit_reason"] == "kill switch"


def test_a_position_closed_behind_the_app_s_back_is_named(demo):
    db_, fake, _, _, _ = demo
    paper.approve("u1", ticket(db_, fake)["id"], now=LATER)
    mt5.sync(LATER)
    fake.positions.clear()                                                 # gone without a closing deal
    mt5.sync(LATER + dt.timedelta(seconds=10))
    assert mt5.state["mismatches"] and "MT5 does not have" in mt5.state["mismatches"][0]
    assert db_.select("global_trades")[0].get("exit_ts") is None           # nothing is changed automatically


def test_the_route_needs_mt5_set_up_and_a_demo_account(both, monkeypatch):  # noqa: F811
    db_, _ = both
    monkeypatch.setattr(mt5, "configured", lambda: False)
    with pytest.raises(paper.PaperError, match="MT5 is not set up"):
        paper.set_route("u1", "mt5_demo")
    fake, _, b = rig()
    fake.trade_mode = 2
    monkeypatch.setattr(mt5, "configured", lambda: True)
    monkeypatch.setattr(mt5, "broker", lambda: b)
    with pytest.raises(paper.PaperError, match="demo accounts only"):
        paper.set_route("u1", "mt5_demo")
    assert paper.set_route("u1", "paper") == {"route": "paper"}
