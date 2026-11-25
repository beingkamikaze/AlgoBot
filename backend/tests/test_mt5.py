"""MT5 adapter end to end against a fake MT5: the app's broker -> HTTP -> the real bridge -> the fake terminal."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app.global_markets.mt5 import bridge
from app.global_markets.mt5.broker import BridgeClient, Mt5Broker, Mt5Error, tag_for

from .fake_mt5 import FakeMT5

TOKEN = "t" * 32


class LossyClient(TestClient):
    """The network between the app and the bridge. lose = "before" (never arrives) or "after" (arrives, answer lost)."""
    lose: str | None = None
    lose_path: str | None = None          # only lose a request to this path (None: the next request, whatever it is)

    def request(self, method, url, **kw):
        if self.lose and self.lose_path and not str(url).endswith(self.lose_path):
            kw.pop("timeout", None)
            return super().request(method, url, **kw)
        if self.lose == "before":
            self.lose = None
            raise httpx.ConnectError("connection refused")
        kw.pop("timeout", None)                      # the test client runs in-process: no network timeout
        r = super().request(method, url, **kw)
        if self.lose == "after":
            self.lose = None
            raise httpx.ReadTimeout("timed out")
        return r


def rig(**fake_kw):
    fake = FakeMT5(**fake_kw)
    fake.initialize()
    http = LossyClient(bridge.create_app(fake, token=TOKEN, symbol="XAUUSD"))
    b = Mt5Broker(BridgeClient("http://testserver", TOKEN, http=http))
    b.connect()
    return fake, http, b


# ---------------------------------------------------------------- the bridge's own guards

def test_the_bridge_needs_a_long_token_and_checks_it():
    with pytest.raises(SystemExit):
        bridge.create_app(FakeMT5(), token="short", symbol="XAUUSD")
    fake = FakeMT5()
    http = TestClient(bridge.create_app(fake, token=TOKEN, symbol="XAUUSD"))
    assert http.get("/health", headers={"X-Bridge-Token": "w" * 32}).status_code == 401
    h = http.get("/health", headers={"X-Bridge-Token": TOKEN}).json()
    assert h["account"]["trade_mode"] == "demo" and fake.connected        # it connects to the terminal on first use


def test_the_bridge_module_imports_nothing_else_from_the_app():
    code = ("import sys; import app.global_markets.mt5.bridge; "
            "print(sorted(m for m in sys.modules if m.startswith('app.') and m not in "
            "('app.global_markets', 'app.global_markets.mt5', 'app.global_markets.mt5.bridge')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_a_real_account_is_refused_by_the_app_and_by_the_bridge_but_closing_still_works():
    fake, http, b = rig()
    placed = b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    fake.trade_mode = 2                                                   # someone logs the terminal in to a real account
    with pytest.raises(Mt5Error, match="real account: the app connects to demo accounts only"):
        Mt5Broker(b.client).connect()
    with pytest.raises(Mt5Error, match="trades demo accounts only"):
        b.place_entry("order-2", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    assert b.close(placed["ticket"], max_slippage=0.5)["ok"]              # getting out is never refused
    assert not fake.positions


# ---------------------------------------------------------------- entries

def test_a_limit_through_the_market_fills_now_but_never_worse_than_the_limit():
    fake, _, b = rig()
    r = b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    sent = fake.sent[-1]
    assert sent["action"] == fake.TRADE_ACTION_DEAL and sent["price"] == 4000.3 and sent["deviation"] == 50   # $0.50 room
    assert r["state"] == "filled" and r["price"] == 4000.3 and r["volume"] == 0.02 and r["tag"] == tag_for("order-1")
    p = b.client.positions()[0]
    assert (p["sl"], p["tp"], p["comment"]) == (3995.0, 4010.0, "aborder1")


def test_a_resting_limit_waits_fills_and_exits_at_its_target():
    fake, _, b = rig()
    r = b.place_entry("order-1", "buy", 0.03, 3998.0, 3994.0, 4006.0, expires_at=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))
    assert r["state"] == "pending" and b.client.orders()[0]["expires_at"] == 1893456000
    fake.move(3997.5)                                                     # ask 3997.8 reaches the 3998 limit
    assert not b.client.orders() and b.client.positions()[0]["price_open"] == 3998.0
    fake.move(4006.2)
    deals = b.client.deals(0)
    assert [d["entry"] for d in deals] == ["in", "out"] and deals[-1]["reason"] == "target"
    assert deals[-1]["profit"] == round((4006.0 - 3998.0) * 3, 2)          # 0.03 lot = 3 oz


def test_a_stop_closes_the_trade_at_the_stop():
    fake, _, b = rig()
    b.place_entry("order-1", "sell", 0.01, 3999.5, 4004.0, 3990.0)
    fake.move(4004.1, 4004.4)
    out = b.client.deals(0)[-1]
    assert out["reason"] == "stop" and out["price"] == 4004.0 and not fake.positions


def test_an_unfilled_limit_expires_and_can_be_cancelled():
    fake, _, b = rig()
    r = b.place_entry("order-1", "buy", 0.01, 3990.0, 3985.0, 4000.0, expires_at=dt.datetime.fromtimestamp(fake.time + 300, dt.timezone.utc))
    fake.move(3999.0, seconds=400)
    assert not b.client.orders()
    r = b.place_entry("order-2", "buy", 0.01, 3990.0, 3985.0, 4000.0)
    assert b.cancel(r["ticket"])["ok"] and not b.client.orders()


# ---------------------------------------------------------------- sizes and prices

def test_lots_are_converted_to_the_broker_lot_and_always_rounded_down():
    _, _, one_oz = rig(contract_size=1.0, volume_min=1.0, volume_step=1.0, volume_max=5000.0)
    assert one_oz.volume(0.05) == 5.0                                     # 0.05 x 100 oz = 5 of a 1 oz lot
    _, _, coarse = rig(volume_min=0.1, volume_step=0.1)
    assert coarse.volume(0.17) == 0.1                                     # never more than the risk check passed
    with pytest.raises(Mt5Error, match="below its minimum"):
        coarse.volume(0.05)


def test_bad_stops_are_refused_before_anything_is_sent():
    fake, _, b = rig(stops_level=100)                                     # this broker wants stops $1.00 away
    with pytest.raises(Mt5Error, match="stop must be below the limit"):
        b.place_entry("o", "buy", 0.01, 4000.8, 4001.0, 4010.0)
    with pytest.raises(Mt5Error, match="at least 1 from the price"):
        b.place_entry("o", "buy", 0.01, 4000.8, 4000.5, 4010.0)
    assert fake.sent == []


def test_broker_refusals_come_back_in_plain_words():
    fake, _, b = rig()
    fake.algo_trading = False
    with pytest.raises(Mt5Error, match="press its Algo Trading button"):
        b.place_entry("o", "buy", 0.01, 4000.8, 3995.0, 4010.0)
    fake.algo_trading, fake.market_open = True, False
    with pytest.raises(Mt5Error, match="the market is closed") as e:
        b.place_entry("o", "buy", 0.01, 4000.8, 3995.0, 4010.0)
    assert e.value.retcode == 10018


# ---------------------------------------------------------------- lost answers: never send an order twice

def test_a_lost_answer_after_the_order_arrived_finds_it_instead_of_sending_again():
    fake, http, b = rig()
    http.lose = "after"
    r = b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    assert r["found_after_timeout"] and r["state"] == "filled"
    assert len([s for s in fake.sent if s["action"] == fake.TRADE_ACTION_DEAL]) == 1 and len(fake.positions) == 1


def test_a_request_that_never_arrived_is_a_plain_failure_and_a_lost_refusal_stays_unknown():
    fake, http, b = rig()
    http.lose = "before"
    with pytest.raises(Mt5Error) as e:
        b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    assert not e.value.ambiguous and fake.sent == []
    fake.market_open = False                                              # it arrives, is refused, and the answer is lost
    http.lose = "after"
    with pytest.raises(Mt5Error, match="Do not send it again") as e:
        b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    assert e.value.ambiguous and not fake.positions


def test_the_token_never_shows_in_an_error():
    b = BridgeClient("http://127.0.0.1:1", TOKEN, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError(f"refused {r.headers}")))))
    with pytest.raises(Mt5Error) as e:
        b.health()
    assert TOKEN not in str(e.value)


# ---------------------------------------------------------------- managing, and checking against the app

def test_only_the_app_s_own_positions_are_seen_or_touched():
    fake, _, b = rig()
    mine = fake.manual_position()
    assert b.client.positions() == []
    with pytest.raises(Mt5Error, match="no such open position"):
        b.close(mine, max_slippage=0.5)
    assert mine in fake.positions


def test_stops_move_and_a_close_beyond_the_allowed_slippage_is_a_requote():
    fake, _, b = rig()
    t = b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)["ticket"]
    assert b.move_stops(t, stop=3999.0)["ok"] and b.client.positions()[0]["sl"] == 3999.0
    with pytest.raises(Mt5Error, match="stop or target is too close"):
        b.move_stops(t, stop=4001.0)                                       # above the bid: wrong side
    fake.fail_next = 10004
    with pytest.raises(Mt5Error, match="requote"):
        b.close(t, max_slippage=0.0)
    assert b.close(t, max_slippage=0.3)["ok"] and not fake.positions


def test_reconcile_names_every_difference_between_the_app_and_mt5():
    fake, _, b = rig()
    b.place_entry("order-1", "buy", 0.02, 4000.8, 3995.0, 4010.0)
    b.place_entry("order-2", "sell", 0.01, 3999.5, 4005.0, 3990.0)
    fake.manual_position()                                                # yours by hand: not the app's business
    assert b.reconcile([{"tag": tag_for("order-1"), "side": "buy", "lots": 0.02},
                        {"tag": tag_for("order-2"), "side": "sell", "lots": 0.01}]) == []
    out = b.reconcile([{"tag": tag_for("order-1"), "side": "buy", "lots": 0.03},
                       {"tag": tag_for("order-9"), "side": "buy", "lots": 0.01}])
    assert out == ["aborder1: the app has buy 0.03 lots, MT5 has buy 0.02",
                   "the app has an open buy (aborder9) that MT5 does not have",
                   "MT5 has an open sell 0.01 lots (aborder2) that the app does not know about"]
