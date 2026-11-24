"""Kite as the primary NSE data source, against a fake Kite: bars from ticks, the source order (Kite live, Kite
historical, Yahoo), option-chain shape parity with the NSE scrape, Kite margins with the estimate as fallback, and
the tick loop's start, subscriptions and stop."""

import dataclasses
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.core.calendar import IST
from app.main import app
from app.market import feed, kite, store, ticks, yahoo
from app.market.models import Bar, Candles
from app.options import chain as ch
from app.options import pricing
from app.risk import margin
from app.risk.engine import AccountState, Limits, OrderRequest, check

from .fakes import FakeKiteConnect, FakeTicker

DAY = dt.date(2026, 10, 27)                    # a Tuesday
PREV = dt.date(2026, 10, 26)


def at(h, m, s=0, d=DAY):
    return dt.datetime(d.year, d.month, d.day, h, m, s, tzinfo=IST)


NSE_ROWS = [
    {"instrument_token": 738561, "tradingsymbol": "RELIANCE", "name": "RELIANCE INDUSTRIES", "segment": "NSE",
     "instrument_type": "EQ", "exchange": "NSE", "lot_size": 1, "tick_size": 0.1, "strike": 0, "expiry": ""},
    {"instrument_token": 256265, "tradingsymbol": "NIFTY 50", "name": "NIFTY 50", "segment": "INDICES",
     "instrument_type": "EQ", "exchange": "NSE", "lot_size": 0, "tick_size": 0, "strike": 0, "expiry": ""},
]
NOW = dt.datetime.now(IST)
EXP = NOW.date() + dt.timedelta(days=7)        # option tests run on the real clock: an expiry a week ahead


def nfo_rows():
    rows = [{"instrument_token": 111, "tradingsymbol": "NIFTY26OCTFUT", "name": "NIFTY", "segment": "NFO-FUT",
             "instrument_type": "FUT", "exchange": "NFO", "lot_size": 65, "tick_size": 0.1, "strike": 0, "expiry": dt.date(2026, 10, 28)}]
    tok = 1000
    for k in list(range(24000, 26001, 250)) + [30000]:
        for kind in ("CE", "PE"):
            tok += 1
            rows.append({"instrument_token": tok, "tradingsymbol": f"NIFTY26O28{k}{kind}", "name": "NIFTY",
                         "segment": "NFO-OPT", "instrument_type": kind, "exchange": "NFO", "lot_size": 65,
                         "tick_size": 0.05, "strike": float(k), "expiry": EXP})
    return rows


def candle(d, h, m, o, c=None, v=1000):
    c = o if c is None else c
    return {"date": dt.datetime(d.year, d.month, d.day, 9, 15) + dt.timedelta(minutes=h * 60 + m - 555), "open": o, "high": max(o, c) + 1, "low": min(o, c) - 1,
            "close": c, "volume": v}


def option_quotes(spot=25000.0, now=None):
    now = now or NOW
    exp_close = dt.datetime.combine(EXP, dt.time(15, 30), IST)
    t = pricing.years_to_expiry(now.timestamp(), exp_close.timestamp())
    q = {"NSE:NIFTY 50": {"last_price": spot, "ohlc": {"close": 24900.0}, "timestamp": dt.datetime(2026, 10, 27, 10, 0, 5, tzinfo=IST)}}
    for r in nfo_rows():
        if r["instrument_type"] not in ("CE", "PE"):
            continue
        p = max(pricing.black76(r["instrument_type"], spot, r["strike"], t, 0.15).price, 0.05)
        q[f"NFO:{r['tradingsymbol']}"] = {
            "last_price": round(p, 2), "oi": 50000, "volume": 12000,
            "depth": {"buy": [{"price": round(p - 0.5, 2), "quantity": 650, "orders": 3}] + [{"price": 0, "quantity": 0, "orders": 0}] * 4,
                      "sell": [{"price": round(p + 0.5, 2), "quantity": 650, "orders": 2}] + [{"price": 0, "quantity": 0, "orders": 0}] * 4}}
    return q


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Logged in to Kite (a fake), with the bar store and the instruments cache in a temporary folder."""
    k = FakeKiteConnect(
        nse=NSE_ROWS, nfo=nfo_rows(), quotes=option_quotes(),
        candles={(738561, "5minute"): [candle(PREV, 9, 15 + 5 * i, 1000 + i) for i in range(6)]
                 + [candle(DAY, 9, 15 + 5 * i, 1100 + i) for i in range(12)]})
    k.quotes["NSE:RELIANCE"] = {"last_price": 1105.0, "ohlc": {"close": 1095.0}}
    monkeypatch.setattr(store, "ROOT", tmp_path / "bars")
    monkeypatch.setattr(kite, "CACHE_DIR", tmp_path / "kite")
    monkeypatch.setattr(kite, "_instruments", kite._Instruments())
    monkeypatch.setattr(kite, "resolver", lambda: k)
    for name in kite.THROTTLES:
        monkeypatch.setattr(kite.THROTTLES[name], "gap", 0.0)
    kite.forget()
    return k


def yahoo_candles(calls):
    def fn(symbol, tf="5m", rng=None):
        calls.append((symbol, tf, rng))
        return Candles(symbol=symbol, provider_symbol=f"{symbol}.NS", name=symbol, tf=tf,
                       bars=[Bar(t=int(at(9, 15).timestamp()), o=1, h=2, l=0.5, c=1.5, v=10, complete=True)])
    return fn


# ---------------------------------------------------------------- bars from ticks

class TestBarBuilder:
    def test_ticks_make_1m_and_5m_bars_with_volume_deltas(self):
        b = ticks.BarBuilder()
        b.mark_gap(at(9, 10))                                  # connected before the open
        b.on_tick("RELIANCE", 100.0, 1000, at(9, 15, 1), prev_close=99.0)
        b.on_tick("RELIANCE", 101.0, 1500, at(9, 15, 30))
        b.on_tick("RELIANCE", 99.5, 1600, at(9, 16, 5))
        b.on_tick("RELIANCE", 102.0, 2000, at(9, 19, 59))
        b.on_tick("RELIANCE", 103.0, 2100, at(9, 20, 0))
        one = b.bars("RELIANCE", "1m", forming=False)
        assert [(dt.datetime.fromtimestamp(x.t, IST).strftime("%H:%M"), x.o, x.h, x.l, x.c, x.v) for x in one] == [
            ("09:15", 100.0, 101.0, 100.0, 101.0, 500), ("09:16", 99.5, 99.5, 99.5, 99.5, 100),
            ("09:19", 102.0, 102.0, 102.0, 102.0, 400)]
        five = b.bars("RELIANCE", "5m")
        assert (five[0].o, five[0].h, five[0].l, five[0].c, five[0].v, five[0].complete) == (100.0, 102.0, 99.5, 102.0, 1000, True)
        assert five[-1].t == int(at(9, 20).timestamp()) and not five[-1].complete      # still forming
        assert b.prev_close["RELIANCE"] == 99.0 and b.last_price("RELIANCE")[0] == 103.0

    def test_a_bar_not_seen_from_its_start_is_never_complete(self):
        b = ticks.BarBuilder()
        b.mark_gap(at(9, 17, 30))                              # the app started mid-bar
        b.on_tick("TCS", 3000.0, 10, at(9, 17, 40))
        b.on_tick("TCS", 3001.0, 20, at(9, 18, 1))
        b.roll(at(9, 21))
        assert [dt.datetime.fromtimestamp(x.t, IST).strftime("%H:%M") for x in b.bars("TCS", "1m")] == ["09:18"]
        assert b.bars("TCS", "5m") == []                       # 09:15 5m bar started before the socket did
        assert b.missing("TCS", "5m", at(9, 21)) and b.missing("TCS", "1m", at(9, 21))

    def test_a_reconnect_marks_the_forming_bar_incomplete(self):
        b = ticks.BarBuilder()
        b.mark_gap(at(9, 0))
        b.on_tick("TCS", 3000.0, 10, at(9, 15, 5))
        b.mark_gap(at(9, 15, 40))                              # dropped and came back inside the bar
        b.on_tick("TCS", 3002.0, 30, at(9, 15, 50))
        b.roll(at(9, 16, 1))
        assert b.bars("TCS", "1m") == []

    def test_late_ticks_and_ticks_outside_the_session_do_not_make_bars(self):
        b = ticks.BarBuilder()
        b.mark_gap(at(9, 0))
        b.on_tick("TCS", 3000.0, 10, at(9, 14, 59))           # pre-open
        b.on_tick("TCS", 3001.0, 20, at(9, 16, 1))
        b.on_tick("TCS", 2900.0, 30, at(9, 15, 59))           # late, for a bar already passed
        b.on_tick("TCS", 3005.0, 40, at(15, 31))              # after the close: price only
        assert all(x.l > 2950 for x in b.bars("TCS", "1m"))
        assert b.last_price("TCS")[0] == 3005.0

    def test_roll_closes_bars_without_a_new_tick_and_drain_returns_them_once(self):
        b = ticks.BarBuilder()
        b.mark_gap(at(9, 0))
        b.on_tick("NIFTY", 25000.0, None, at(9, 15, 2))       # indices: no volume
        b.roll(at(9, 16))
        assert len(b.bars("NIFTY", "1m", forming=False)) == 1 and b.bars("NIFTY", "1m")[0].v == 0
        drained = b.drain()
        assert ("NIFTY", "1m") in drained and b.drain() == {}


# ---------------------------------------------------------------- source order

class TestSourceOrder:
    def test_not_logged_in_uses_yahoo(self, monkeypatch):
        calls = []
        monkeypatch.setattr(yahoo, "candles", yahoo_candles(calls))
        c = feed.candles("RELIANCE", "5m", "5d")
        assert c.source == "yahoo" and c.delayed and calls == [("RELIANCE", "5m", "5d")]

    def test_logged_in_without_ticks_uses_kite_historical(self, fake, monkeypatch):
        monkeypatch.setattr(yahoo, "candles", lambda *a, **k: pytest.fail("Yahoo must not be called"))
        c = feed.candles("RELIANCE", "5m", "5d", now=at(10, 12))
        assert c.source == "kite" and not c.delayed and c.name == "RELIANCE INDUSTRIES"
        today = [b for b in c.bars if dt.datetime.fromtimestamp(b.t, IST).date() == DAY]
        assert len(today) == 12 and [b.complete for b in today][-2:] == [True, False]   # 10:10 still forming at 10:12
        assert c.prev_close == 1005.0                              # the previous session's last close
        assert ("historical", 738561, "5minute") in fake.calls

    def test_fresh_ticks_win_and_earlier_days_come_from_the_store(self, fake, monkeypatch):
        monkeypatch.setattr(yahoo, "candles", lambda *a, **k: pytest.fail("Yahoo must not be called"))
        b = ticks.builder
        b.mark_gap(at(9, 0))
        for i, (p, v) in enumerate([(2000.0, 100), (2010.0, 200), (2005.0, 300)]):
            b.on_tick("RELIANCE", p, v, at(9, 15, 10 + 10 * i))
        b.on_tick("RELIANCE", 2020.0, 400, at(9, 20, 5))
        c = feed.candles("RELIANCE", "5m", "5d", now=at(9, 21))
        assert c.source == "kite_live" and not c.delayed
        days = sorted({dt.datetime.fromtimestamp(x.t, IST).date() for x in c.bars})
        assert days == [PREV, DAY]
        first_today = next(x for x in c.bars if dt.datetime.fromtimestamp(x.t, IST).date() == DAY)
        assert (first_today.o, first_today.h, first_today.c) == (2000.0, 2010.0, 2005.0)    # the tick bar, not Kite's 1100
        assert c.bars[-1].complete is False and c.last_price == 2020.0
        assert len(store.read("RELIANCE", "5m")) == 6                  # the store was topped up with the previous day
        hist_calls = [x for x in fake.calls if x[0] == "historical"]
        feed.candles("RELIANCE", "5m", "5d", now=at(9, 21))
        assert [x for x in fake.calls if x[0] == "historical"] == hist_calls     # once a day, not per request

    def test_a_tick_gap_is_filled_from_kite_historical(self, fake):
        b = ticks.builder
        b.mark_gap(at(9, 26))                                   # started late: 09:15 and 09:20 never streamed
        b.on_tick("RELIANCE", 2000.0, 100, at(9, 30, 1))
        b.on_tick("RELIANCE", 2001.0, 200, at(9, 35, 1))
        c = feed.candles("RELIANCE", "5m", "1d", now=at(9, 36))
        assert c.source == "kite_live"
        assert [(dt.datetime.fromtimestamp(x.t, IST).strftime("%H:%M"), x.o) for x in c.bars][:4] == [
            ("09:15", 1100.0), ("09:20", 1101.0), ("09:25", 1102.0), ("09:30", 2000.0)]

    def test_a_kite_failure_falls_back_to_yahoo(self, fake, monkeypatch):
        calls = []
        monkeypatch.setattr(yahoo, "candles", yahoo_candles(calls))
        fake.fail = "historical"
        c = feed.candles("RELIANCE", "15m", "1mo", now=at(10, 0))
        assert c.source == "yahoo" and calls

    def test_unknown_symbol_on_kite_falls_back_to_yahoo(self, fake, monkeypatch):
        calls = []
        monkeypatch.setattr(yahoo, "candles", yahoo_candles(calls))
        assert feed.candles("NOSUCH", "5m", "5d").source == "yahoo"

    def test_store_history_says_which_source_it_used(self, fake):
        c = store.history("RELIANCE", "5m")
        assert c.source == "kite"
        assert all(b.complete for b in store.read("RELIANCE", "5m"))

    def test_chart_api_reports_the_source(self, monkeypatch):
        calls = []
        monkeypatch.setattr(yahoo, "candles", yahoo_candles(calls))
        body = TestClient(app).get("/api/market/chart", params={"symbol": "TCS", "tf": "5m"}).json()
        assert body["source"] == "yahoo" and body["source_label"] == "Yahoo, delayed" and body["delayed"] is True
        src = TestClient(app).get("/api/market/source").json()
        assert src["kite"] is False and src["label"] == "Yahoo, delayed"

    def test_quotes_use_live_ticks_then_kite(self, fake):
        ticks.builder.mark_gap(dt.datetime.now(IST))
        ticks.builder.on_tick("NIFTY", 25100.0, None, dt.datetime.now(IST), prev_close=25000.0)
        q = {x.symbol: x for x in feed.quotes(["NIFTY", "RELIANCE"])}
        assert q["NIFTY"].last == 25100.0 and q["NIFTY"].change_pct == 0.4
        assert q["RELIANCE"].last == 1105.0 and q["RELIANCE"].prev_close == 1095.0
        assert ("ohlc", 1) in fake.calls

    def test_worst_source_labels_a_mixed_scan(self):
        assert feed.worst(["kite_live", "yahoo", "kite"]) == "yahoo" and feed.worst(["kite_live", "kite"]) == "kite"


# ---------------------------------------------------------------- option chains

def nse_doc(spot=25000.0):
    data = []
    for k in range(24000, 26001, 250):
        row = {"strikePrice": k}
        for kind in ("CE", "PE"):
            row[kind] = {"lastPrice": 100.0, "buyPrice1": 99.5, "sellPrice1": 100.5, "impliedVolatility": 15.0,
                         "openInterest": 5000, "changeinOpenInterest": 100, "totalTradedVolume": 1000}
        data.append(row)
    return {"records": {"underlyingValue": spot, "timestamp": "27-Oct-2026 10:00:00", "data": data}}


class TestKiteChain:
    def test_kite_chain_has_the_nse_chain_shape(self, fake, monkeypatch):
        monkeypatch.setattr(ch, "_cache", {})
        k = ch.chain("NIFTY", EXP)
        n = ch.parse(nse_doc(), "NIFTY", EXP, [EXP])
        assert k.source == "kite" and n.source == "nse"
        assert {f.name for f in dataclasses.fields(k)} == {f.name for f in dataclasses.fields(n)}
        assert type(k.spot) is type(n.spot) and type(k.as_of) is type(n.as_of) and k.as_of == "27-Oct-2026 10:00:05"
        assert k.strikes() == n.strikes()                       # ±10% band: the 30000 strike is not quoted
        kq, nq = k.quote(25000, "CE"), n.quote(25000, "CE")
        for f in ("ltp", "bid", "ask", "iv", "oi", "volume", "mid", "spread_pct"):
            assert type(getattr(kq, f)) is type(getattr(nq, f)), f
        assert kq.bid == kq.ltp - 0.5 and kq.ask == kq.ltp + 0.5 and len(kq.depth["buy"]) == 5
        assert abs(kq.iv - 15.0) < 0.6 and kq.chg_oi is None     # our IV from the mid
        assert k.lot_size == 65 and k.expiries == [EXP] and k.tradingsymbol(25000, "CE").endswith("25000CE")
        assert abs(k.forward() - 25000) < 60                    # put-call parity is used on Kite chains too
        g = k.greeks(25000, "CE")
        assert g is not None and 0.4 < g.delta < 0.6
        assert sum(1 for c in fake.calls if c[0] == "quote") == 1    # one batched quote call

    def test_api_rows_have_the_same_keys_for_kite_and_nse(self, fake, monkeypatch):
        monkeypatch.setattr(ch, "_cache", {})
        k = ch.chain("NIFTY", EXP)
        n = ch.parse(nse_doc(), "NIFTY", EXP, [EXP])
        bodies = []
        for c in (k, n):
            monkeypatch.setattr(ch, "chain", lambda *a, _c=c, **kw: _c)
            r = TestClient(app).get("/api/options/chain", params={"underlying": "NIFTY", "width": 3})
            assert r.status_code == 200, r.text
            bodies.append(r.json())
        assert bodies[0].keys() == bodies[1].keys()
        assert bodies[0]["rows"][0].keys() == bodies[1]["rows"][0].keys()
        assert bodies[0]["rows"][0]["CE"].keys() == bodies[1]["rows"][0]["CE"].keys()

    def test_quote_batches_respect_kites_500_instrument_limit(self, fake, monkeypatch):
        kd = kite.KiteData(fake)
        kd.quote([f"NFO:X{i}" for i in range(1201)])
        assert [c[1] for c in fake.calls if c[0] == "quote"] == [500, 500, 201]

    def test_not_logged_in_uses_the_nse_scrape(self, monkeypatch):
        monkeypatch.setattr(ch, "_cache", {})
        monkeypatch.setattr(ch, "_get", lambda url: {"expiryDates": [EXP.strftime("%d-%b-%Y")]} if "contract-info" in url else nse_doc())
        c = ch.chain("NIFTY", EXP, allow_modeled=False)
        assert c.source == "nse"

    def test_a_kite_failure_falls_back_to_the_nse_scrape(self, fake, monkeypatch):
        monkeypatch.setattr(ch, "_cache", {})
        monkeypatch.setattr(ch, "_get", lambda url: {"expiryDates": [EXP.strftime("%d-%b-%Y")]} if "contract-info" in url else nse_doc())
        fake.fail = "quote"
        assert ch.chain("NIFTY", EXP, allow_modeled=False).source == "nse"

    def test_instruments_are_downloaded_once_a_day(self, fake, monkeypatch):
        monkeypatch.setattr(ch, "_cache", {})
        ch.chain("NIFTY", EXP)
        monkeypatch.setattr(ch, "_cache", {})
        ch.chain("NIFTY", EXP)
        assert [c for c in fake.calls if c == ("instruments", "NFO")] == [("instruments", "NFO")]
        monkeypatch.setattr(kite, "_instruments", kite._Instruments())          # a restart: read from the disk cache
        kite.KiteData(fake).instruments("NFO")
        assert [c for c in fake.calls if c == ("instruments", "NFO")] == [("instruments", "NFO")]


# ---------------------------------------------------------------- margins

def fut_order(**kw):
    base = dict(symbol="NIFTY26OCTFUT", side="buy", qty=65, limit=25000.0, stop=24980.0, target=25060.0, kind="future",
                lot_size=65, margin_pct=12.0)
    return OrderRequest(**{**base, **kw})


STATE = AccountState(capital=1_000_000, cash_free=1_000_000)


class TestMargins:
    def test_without_kite_the_estimate_is_used_and_labelled(self):
        assert margin.for_order("NFO", "NIFTY26OCTFUT", "buy", 65, 25000.0, 24980.0) is None
        assert margin.for_basket([{"tradingsymbol": "X", "side": "buy", "qty": 65, "price": 100.0}]) is None
        v = check(fut_order(), Limits(), STATE, at(10, 0), 25000.0)
        assert v.numbers["margin"] == round(65 * 25000 * 0.12, 2)
        assert v.numbers["margin_source"] == "estimate" and v.numbers["charges_source"] == "estimate"

    def test_kite_margin_and_round_trip_charges_replace_the_estimate(self, fake):
        m = margin.for_order("NFO", "NIFTY26OCTFUT", "buy", 65, 25000.0, 24980.0)
        assert m["source"] == "kite" and m["margin"] == round(65 * 25000 * 0.12, 2)
        assert m["charges"] == round(20 + 65 * 25000 * 0.0005 + 20 + 65 * 24980 * 0.0005, 2)   # entry + exit at the stop
        fake.margin_rate = 0.10
        m = margin.for_order("NFO", "NIFTY26OCTFUT", "buy", 65, 25000.0, 24980.0)
        v = check(fut_order(margin_exact=m["margin"], charges_exact=m["charges"]), Limits(max_loss_per_trade=5000),
                  STATE, at(10, 0), 25000.0)
        assert v.numbers["margin"] == m["margin"] and v.numbers["margin_source"] == "Kite"
        assert v.numbers["charges_at_stop"] == m["charges"] and v.numbers["charges_source"] == "Kite"

    def test_stocks_keep_blocking_their_full_value(self, fake):
        m = margin.for_order("NSE", "RELIANCE", "buy", 10, 1100.0, 1090.0)
        v = check(OrderRequest("RELIANCE", "buy", 10, 1100.0, 1090.0, 1120.0, margin_exact=m["margin"],
                               charges_exact=m["charges"]), Limits(), STATE, at(10, 0), 1100.0)
        assert v.numbers["margin"] == 11000.0 and v.numbers["margin_source"] == "full value"
        assert v.numbers["charges_source"] == "Kite"

    def test_a_kite_margin_failure_falls_back_to_the_estimate(self, fake):
        fake.fail = "order_margins"
        assert margin.for_order("NFO", "NIFTY26OCTFUT", "buy", 65, 25000.0, 24980.0) is None

    def test_basket_margin_counts_the_hedge(self, fake):
        legs = [{"tradingsymbol": "NIFTY26O2825000CE", "side": "sell", "qty": 65, "price": 100.0},
                {"tradingsymbol": "NIFTY26O2825500CE", "side": "buy", "qty": 65, "price": 30.0}]
        m = margin.for_basket(legs)
        assert m["margin"] == round(65 * 130 * 0.12 * 0.5, 2) and m["charges"] is not None
        assert ("order_margins", 4) in fake.calls                  # entry legs + the same legs closed

    def test_option_ticket_uses_kite_basket_margin(self, fake, monkeypatch):
        from app.options import structures as S
        from app.paper import options as paper_options
        monkeypatch.setattr(ch, "_cache", {})
        c = ch.chain("NIFTY", EXP)
        s = S.build(c, "bull_call_spread", {}, NOW)
        est = s.margin
        req = paper_options.request_for(s, paper_options.plan_exits(s, {}))
        assert req.margin_source == "Kite" and s.margin_source == "Kite" and req.margin != est


# ---------------------------------------------------------------- the tick loop

@pytest.fixture
def loop_env(fake, monkeypatch):
    FakeTicker.made.clear()
    positions = [("NFO", "NIFTY26OCTFUT")]
    monkeypatch.setattr(ticks, "base_symbols", lambda: ["NIFTY", "RELIANCE"])
    monkeypatch.setattr(ticks, "position_instruments", lambda: list(positions))
    lp = ticks.FeedLoop(ticker_factory=FakeTicker, bars=ticks.BarBuilder())
    return lp, positions


class TestFeedLoop:
    def test_starts_after_a_login_and_subscribes_indices_stocks_and_positions(self, loop_env):
        lp, _ = loop_env
        lp.step(at(9, 5))
        ws = FakeTicker.made[-1]
        assert ws.subscribed == {256265, 738561, 111} and set(ws.modes.values()) == {"full"}
        assert lp.state["status"] == "live" and lp.state["instruments"] == 3

    def test_positions_are_added_and_removed(self, loop_env):
        lp, positions = loop_env
        lp.step(at(9, 5))
        ws = FakeTicker.made[-1]
        positions.clear()
        lp.step(at(9, 6))
        assert 111 not in ws.subscribed
        positions.append(("NFO", "NIFTY26OCTFUT"))
        lp.step(at(9, 7))
        assert 111 in ws.subscribed

    def test_ticks_become_bars_keyed_by_our_symbol(self, loop_env):
        lp, _ = loop_env
        lp.step(at(9, 5))
        ws = FakeTicker.made[-1]
        ws.push([{"instrument_token": 256265, "last_price": 25000.0, "exchange_timestamp": dt.datetime(2026, 10, 27, 9, 15, 3)},
                 {"instrument_token": 111, "last_price": 25050.0, "volume_traded": 5000,
                  "exchange_timestamp": dt.datetime(2026, 10, 27, 9, 15, 4)}])
        assert lp.bars.last_price("NIFTY")[0] == 25000.0 and lp.bars.last_price("NIFTY26OCTFUT")[0] == 25050.0

    def test_stops_when_the_token_expires_and_not_before_the_window(self, loop_env, monkeypatch):
        lp, _ = loop_env
        lp.step(at(8, 30))
        assert lp.ticker is None                                  # before 09:00: no socket yet
        lp.step(at(9, 5))
        ws = FakeTicker.made[-1]
        monkeypatch.setattr(kite, "resolver", lambda: None)       # token expired / logged out
        kite.forget()
        lp.step(at(9, 6))
        assert ws.closed and lp.ticker is None and lp.state["status"] == "no Kite login today"

    def test_after_the_close_the_socket_closes(self, loop_env):
        lp, _ = loop_env
        lp.step(at(9, 5))
        ws = FakeTicker.made[-1]
        lp.step(at(15, 36))
        assert ws.closed and lp.ticker is None

    def test_a_dead_socket_is_restarted_after_a_backoff(self, loop_env):
        lp, _ = loop_env
        lp.step(at(9, 5))
        lp.ticker.dead = True
        lp.step(at(9, 6))
        assert lp.ticker is None and lp.failures == 1
        lp.step(at(9, 6, 15))
        assert lp.ticker is None                                  # still backing off
        lp.next_start = 0
        lp.step(at(9, 7))
        assert lp.ticker is not None and len(FakeTicker.made) == 2

    def test_a_403_stops_the_socket(self, loop_env):
        lp, _ = loop_env
        lp.step(at(9, 5))
        lp.ticker._on_error(FakeTicker.made[-1], 403, "Forbidden")
        assert lp.ticker.dead

    def test_the_instrument_limit_keeps_positions_first(self, fake, monkeypatch):
        monkeypatch.setattr(ticks, "MAX_TOKENS", 2)
        monkeypatch.setattr(ticks, "base_symbols", lambda: ["NIFTY", "RELIANCE"])
        monkeypatch.setattr(ticks, "position_instruments", lambda: [("NFO", "NIFTY26OCTFUT")])
        w = ticks.wanted(kite.KiteData(fake), DAY)
        assert len(w) == 2 and w[111] == "NIFTY26OCTFUT"

    def test_after_the_close_kite_history_overwrites_tick_bars(self, fake):
        bad = Bar(t=int(at(9, 15).timestamp()), o=1.0, h=1.0, l=1.0, c=1.0, v=1, complete=True)
        store.write("RELIANCE", "5m", [bad])
        r = ticks.correct_day(kite.KiteData(fake), ["RELIANCE"], DAY, tfs=("5m",), log_=lambda *_: None)
        bars = {b.t: b for b in store.read("RELIANCE", "5m")}
        assert r["bars"] == 12 and bars[bad.t].o == 1100.0

    def test_completed_bars_are_saved_to_the_store(self, fake):
        b = ticks.BarBuilder()
        b.mark_gap(at(9, 0))
        b.on_tick("RELIANCE", 2000.0, 1, at(9, 15, 5))
        b.roll(at(9, 21))
        assert ticks.persist(b) >= 2
        assert store.read("RELIANCE", "5m")[0].o == 2000.0 and store.read("RELIANCE", "1m")[0].o == 2000.0
