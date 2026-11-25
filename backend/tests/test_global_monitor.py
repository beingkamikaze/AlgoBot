"""The gold trade monitor: fixed checks on closed bars, health, and alerts that reach Telegram."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

import pytest

from app.global_markets import macro, monitor
from app.market.models import Bar
from app.telegram import service as tg

from .fakes import FakeDB
from .test_global_agent import LONDON, NOW, market

FLAT_BY = dt.datetime(2026, 11, 3, 18, 0, tzinfo=dt.timezone.utc)       # 13:00 New York


def setup(side="buy", entry=4001.5, stop=3997.5, target=4009.5, strategy=None, at=LONDON + dt.timedelta(minutes=15)):
    return monitor.Setup(side=side, entry=entry, stop=stop, target=target, entry_t=int(at.timestamp()), flat_by=FLAT_BY,
                         strategy=strategy)


def read(s, now=NOW, bars=None):
    b = bars or market(now)
    return monitor.analyze(s, b["1m"], b["5m"], b["15m"], now)


def check(a, key):
    return next(c for c in a["checks"] if c["key"] == key)


@pytest.fixture(autouse=True)
def quiet_calendar(monkeypatch):
    monkeypatch.setattr(macro, "near", lambda now: [])


def test_a_buy_in_a_rising_market_is_on_track_and_measured_in_r():
    a = read(setup(strategy="asia-breakout"))
    assert a["health"] == "on track", a["headline"]
    assert a["progress"]["price"] == 4002.9 and a["progress"]["r"] == round((4002.9 - 4001.5) / 4, 2)
    assert check(a, "thesis")["ok"] and "Asian high 4,001.00" in check(a, "thesis")["detail"]
    assert {c["key"] for c in a["checks"]} >= {"thesis", "ema", "momentum", "shock", "stop", "room", "release", "time"}
    assert a["time"]["minutes_to_flat"] == 570 and a["sr"]


def test_a_sell_against_the_market_loses_its_thesis_and_says_so():
    a = read(setup(side="sell", entry=4001.5, stop=4004.5, target=3995.5, strategy="asia-breakout"))
    assert not check(a, "thesis")["ok"] and not check(a, "ema")["ok"] and not check(a, "momentum")["ok"]
    assert a["health"] == "thesis broken" and "back inside the Asian range" in a["headline"]


def test_near_the_stop_is_at_risk_even_without_a_strategy():
    a = read(setup(entry=4003.5, stop=4002.0, target=4010.0))           # -0.4R, 0.6R from a 1.5 stop
    assert check(a, "stop")["ok"]
    a = read(setup(entry=4004.0, stop=4002.5, target=4010.0))           # -0.73R: within 0.4R of the stop
    assert not check(a, "stop")["ok"] and a["health"] == "at risk"
    assert "thesis" not in {c["key"] for c in a["checks"]}               # no strategy: general checks only


def test_a_sudden_bar_against_the_trade_is_a_shock():
    b = market(NOW)
    last = b["1m"][-1]
    b["1m"][-1] = Bar(last.t, last.o, last.o + 0.2, last.o - 12.0, last.o - 11.5, 0, True)   # a $12 drop in a minute
    a = read(setup(), bars=b)
    assert not check(a, "shock")["ok"] and "against the trade" in check(a, "shock")["detail"]
    assert a["health"] in ("at risk", "thesis broken")


def test_a_us_release_close_by_is_flagged(monkeypatch):
    e = macro.Event("US CPI (inflation)", NOW + dt.timedelta(minutes=12), "inflation figures often move gold sharply")
    monkeypatch.setattr(macro, "near", lambda now: [e])
    a = read(setup())
    c = check(a, "release")
    assert not c["ok"] and c["severity"] == "high" and "US CPI (inflation) in 12 min" in c["detail"]


def test_stale_prices_are_named():
    later = NOW + dt.timedelta(minutes=10)
    a = read(setup(), now=later, bars=market(NOW))                       # no bars for the last 10 minutes
    assert not check(a, "prices")["ok"] and "10 min" in check(a, "prices")["detail"]


def test_failed_break_thesis_wants_price_back_inside_yesterday_s_range():
    a = read(setup(side="sell", entry=4003.0, stop=4006.0, target=3997.0, strategy="failed-break"))
    th = check(a, "thesis")
    assert not th["ok"] and "yesterday's high 4,001.00" in th["detail"] and "broke out again" in th["detail"]


def test_no_bars_means_no_data():
    a = monitor.analyze(setup(), [], [], [], NOW)
    assert a["health"] == "no data" and a["checks"] == []


# ---------------------------------------------------------------- alerts

@pytest.fixture
def fdb(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(monitor, "db", lambda: d)
    monitor._state.clear()
    d.insert("global_trades", {"id": "t1", "user_id": "u1", "mode": "paper", "symbol": "XAUUSD", "side": "buy", "lots": 0.01,
                               "units": 1.0, "price_source": "spot", "entry": 4001.5, "entry_ts": (LONDON + dt.timedelta(minutes=15)).isoformat(),
                               "exit_ts": None, "trading_day": "2026-11-03",
                               "entry_context": {"stop": 3997.5, "target": 4009.5, "order_id": None}})
    return d


def test_an_alert_is_raised_when_health_gets_worse_and_reaches_telegram(fdb, monkeypatch):
    got = []
    monkeypatch.setattr(monitor, "listeners", [lambda *a: 1 / 0, lambda uid, al: got.append((uid, al))])
    first = monitor.analyze_trade("u1", "t1", NOW, bars=tuple(market(NOW)[k] for k in ("1m", "5m", "15m")))
    assert first["health"] == "on track" and first["alerts"] == [] and first["unrealized"] is not None
    e = macro.Event("US jobs report (NFP)", NOW + dt.timedelta(minutes=5), "the jobs report often moves gold")
    monkeypatch.setattr(macro, "near", lambda now: [e])
    again = monitor.analyze_trade("u1", "t1", NOW, bars=tuple(market(NOW)[k] for k in ("1m", "5m", "15m")))
    titles = [a["title"] for a in again["alerts"]]
    assert titles == ["XAUUSD buy: no us release close failed", "XAUUSD buy: on track → at risk"]
    assert [a["title"] for _, a in got] == titles and got[0][0] == "u1"
    assert len(fdb.select("risk_events")) == 2
    assert monitor.analyze_trade("u1", "t1", NOW, bars=tuple(market(NOW)[k] for k in ("1m", "5m", "15m")))["alerts"] == again["alerts"]


def test_another_user_cannot_read_your_trade(fdb):
    with pytest.raises(LookupError):
        monitor.analyze_trade("u2", "t1", NOW)


def test_telegram_gets_the_alert_in_plain_words(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "configured", lambda: True)
    monkeypatch.setattr(tg, "_chats", lambda uid=None: [42])
    monkeypatch.setattr(tg._outbox, "put", sent.append)
    tg.on_monitor("u1", {"level": "high", "title": "XAUUSD sell: watch → at risk", "detail": "within 0.30R of the stop"})
    assert sent == [(42, "⚠️ <b>Monitor: XAUUSD sell: watch → at risk</b>\nwithin 0.30R of the stop", None)]


def test_the_monitor_does_not_load_nse_market_code():
    code = ("import sys; import app.global_markets.monitor; "
            "bad = ['app.core.calendar', 'app.market.yahoo', 'app.market.nse', 'app.market.store', 'app.paper.service', "
            "'app.risk.engine', 'app.monitor.analysis']; print([m for m in bad if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"
