"""Global paper trading: the dollar risk check, the fill rules, and whole sessions replayed on a fake database."""

from __future__ import annotations

import datetime as dt

import pytest

from app.global_markets import paper, paper_sim as sim
from app.global_markets.costs import CostModel
from app.global_markets.instruments import resolve
from app.global_markets.risk import AccountState, Limits, OpenPosition, OrderRequest, PriceInfo, check, max_lots
from app.market.models import Bar

from .fakes import FakeDB

UTC = dt.timezone.utc
GOLD = resolve("XAUUSD")
Q = sim.Quotes(spread=0.60, slippage=0.10)
# Tuesday 3 Nov 2026, New York on standard time: 09:00 New York = 14:00 UTC; entries 08:00 London (08:00 UTC) to 16:00 UTC;
# flat by 13:00 New York = 18:00 UTC
DAY = dt.datetime(2026, 11, 3, tzinfo=UTC)


def at(h: int, m: int = 0, s: int = 0) -> dt.datetime:
    return DAY.replace(hour=h, minute=m, second=s)


def bar(h: int, m: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(t=int(at(h, m).timestamp()), o=o, h=hi, l=lo, c=c, v=0, complete=True)


def fresh(price: float = 4000.0, age: float = 30) -> PriceInfo:
    return PriceInfo(price, age, "spot")


# ---------------------------------------------------------------- sizing and the risk check

def test_size_is_the_smaller_of_max_loss_and_position_cap():
    lim = Limits()
    assert max_lots(GOLD, 4000.0, 3990.0, lim) == 0.01          # $20 / ($10 + $0.20) = 1.96 oz -> 0.01 lot
    assert max_lots(GOLD, 4000.0, 3998.0, lim) == 0.02          # 9.6 oz by risk, but $10,000 caps it at 2 oz
    assert max_lots(GOLD, 4000.0, 3970.0, lim) == 0.0           # $30 away: even 1 oz loses more than $20


def test_a_clean_ticket_passes_and_counts_slippage_in_the_max_loss():
    v = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0, 4020.0), Limits(), AccountState(capital=10_000), at(14), fresh())
    assert v.verdict == "PASS", v.reasons
    assert v.numbers["max_loss"] == pytest.approx(10.8)          # 1 oz x $10.60 + 2 x $0.10 slippage
    assert v.numbers["value"] == pytest.approx(4000.6)


def test_your_limits_can_be_overridden_on_paper_but_hard_blocks_cannot():
    late = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), Limits(), AccountState(capital=10_000), at(16, 30), fresh())
    assert late.verdict == "BLOCK" and late.to_dict()["overridable"]           # outside the entry window: your rule
    stale = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), Limits(), AccountState(capital=10_000), at(14),
                  fresh(age=30 * 60))
    assert stale.to_dict()["hard"] and not stale.to_dict()["overridable"]        # no fair price: never
    shut = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), Limits(), AccountState(capital=10_000),
                 at(22, 30), fresh())                                         # daily break, 17:30 New York
    assert any("market closed" in r for r in shut.to_dict()["hard"])
    flat = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), Limits(), AccountState(capital=10_000), at(18, 5), fresh())
    assert any("flat-by" in r for r in flat.to_dict()["hard"])


def test_daily_open_risk_position_cap_and_kill_switch():
    lim = Limits()
    st = AccountState(capital=10_000, realized_today=-45.0)
    v = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), lim, st, at(14), fresh())
    assert v.verdict == "PASS"                                       # $45 lost + $10.80 at risk is inside $60
    st = AccountState(capital=10_000, realized_today=-50.0)
    v = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), lim, st, at(14), fresh())
    assert any("would be over your daily limit" in r for r in v.reasons)
    st = AccountState(capital=10_000, open_positions=[OpenPosition("XAUUSD", "sell", 2, 4010, 4015)])
    v = check(OrderRequest(GOLD, "buy", 0.02, 4000.6, 3995.0), lim, st, at(14), fresh())
    assert any("already have an open XAUUSD" in r for r in v.reasons)
    assert any("over your cap" in r for r in v.reasons)
    v = check(OrderRequest(GOLD, "buy", 0.01, 4000.6, 3990.0), lim, AccountState(capital=10_000, kill_switch=True), at(14), fresh())
    assert v.to_dict()["hard"] == ["the kill switch is on for this trading day"]


def test_a_limit_far_from_the_price_or_a_price_past_the_stop_is_refused():
    v = check(OrderRequest(GOLD, "buy", 0.01, 4020.0, 4000.0), Limits(), AccountState(capital=10_000), at(14), fresh(4000.0))
    assert any("too far to be a fair fill" in r for r in v.reasons)
    v = check(OrderRequest(GOLD, "sell", 0.01, 3999.4, 4001.0), Limits(), AccountState(capital=10_000), at(14), fresh(4002.0))
    assert any("already beyond the stop" in r for r in v.reasons)


# ---------------------------------------------------------------- fill rules

def test_entry_fills_at_the_ask_with_slippage_and_never_worse_than_the_limit():
    placed = int(at(14).timestamp())
    f = sim.entry_fill("buy", 4000.6, placed, [bar(13, 59, 3990, 4100, 3900, 4000), bar(14, 0, 4000.1, 4001, 3999.5, 4000.5)], Q)
    assert (f.t, f.price, f.slippage) == (placed, 4000.5, 0.1)      # the 13:59 bar began before the confirm: skipped
    f = sim.entry_fill("buy", 4000.6, placed, [bar(14, 0, 4002.0, 4003, 3999.0, 4001)], Q)
    assert f.price == 4000.6                                         # opened above the limit, traded down to it
    assert sim.entry_fill("buy", 4000.6, placed, [bar(14, 0, 4002.0, 4003, 4001.0, 4002)], Q) is None
    f = sim.entry_fill("sell", 3999.4, placed, [bar(14, 0, 4000.0, 4000.5, 3999, 3999.5)], Q)
    assert (f.price, f.slippage) == (3999.6, 0.1)                    # bid 3999.70 - 0.10


def test_expiry_runs_on_bar_time_so_a_delayed_feed_does_not_expire_orders_early():
    placed = int(at(14).timestamp())
    assert sim.entry_expired(placed, [bar(14, 3, 1, 1, 1, 1)], 600, int(at(16).timestamp())) is None
    assert "10 minutes" in sim.entry_expired(placed, [bar(14, 9, 1, 1, 1, 1)], 600, int(at(16).timestamp()))
    assert sim.entry_expired(placed, [bar(14, 3, 1, 1, 1, 1)], 600, int(at(14, 2).timestamp())) == "the entry window ended"


def test_exits_stop_first_gaps_targets_through_and_flat_by():
    after = int(at(14).timestamp())
    flat = int(at(18).timestamp())
    both = sim.exit_check("buy", 4000.5, 3990.0, 4020.0, after, [bar(14, 1, 4000, 4030, 3980, 4000)], flat, Q)
    assert (both.fill.reason, both.fill.price) == ("stop", 3989.9)   # stop wins; filled at the stop less slippage
    gap = sim.exit_check("buy", 4000.5, 3990.0, None, after, [bar(14, 1, 3985, 3986, 3984, 3985)], flat, Q)
    assert gap.fill.price == round(3985 - 0.3 - 0.1, 2)               # opened past the stop: the open's bid
    touch = sim.exit_check("buy", 4000.5, 3990.0, 4020.0, after, [bar(14, 1, 4010, 4020.3, 4009, 4015)], flat, Q)
    assert touch.fill is None                                        # bid touched 4020.00 exactly: not through
    t = sim.exit_check("buy", 4000.5, 3990.0, 4020.0, after, [bar(18, 0, 4010, 4011, 4009, 4010)], flat, Q)
    assert (t.fill.reason, t.fill.price) == ("time", 4009.6)
    short = sim.exit_check("sell", 4000.0, 4010.0, None, after, [bar(14, 1, 4005, 4009.8, 4004, 4006)], flat, Q)
    assert short.fill.reason == "stop"                               # the ask (4010.10) reached it


def test_costs_add_up():
    r = sim.pnl("buy", 4000.5, 4020.0, 1.0, 0.1, 0.0, Q)
    assert r == {"net_pnl": 19.5, "costs": 0.7, "gross_pnl": 20.2}


# ---------------------------------------------------------------- replayed sessions

class Market:
    """Bars arrive as the clock moves: only bars that have closed by `now` are visible, as from a live feed."""

    def __init__(self):
        self.now = at(14)
        self.all: list[Bar] = []

    def bars(self, symbol, source):
        return [b for b in self.all if b.t + 60 <= self.now.timestamp()]


@pytest.fixture
def world(monkeypatch):
    fdb, mkt = FakeDB(), Market()
    monkeypatch.setattr(paper, "db", lambda: fdb)
    monkeypatch.setattr(paper, "bars", mkt.bars)
    monkeypatch.setattr(paper, "_now", lambda: mkt.now)
    monkeypatch.setattr(paper.feed, "default_source", lambda: "spot")
    monkeypatch.setattr(paper, "_headlines", lambda symbol: [{"title": "Gold steadies", "source": "Test", "time": None}])
    closed: list[str] = []
    monkeypatch.setattr(paper, "on_trade_closed", closed.append)
    mkt.closed = closed
    return fdb, mkt


def _flat(mkt: Market, start: dt.datetime, minutes: int, price: float) -> None:
    for i in range(minutes):
        t = start + dt.timedelta(minutes=i)
        mkt.all.append(bar(t.hour, t.minute, price, price + 0.2, price - 0.2, price))


def test_a_session_from_ticket_to_target(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)                              # last close 4000.00 at 13:59
    o = paper.propose("u1", symbol="xauusd", side="buy", stop=3990, target=4020, why="test")
    assert o["risk_verdict"]["verdict"] == "PASS", o["risk_verdict"]
    assert (o["params"]["limit"], o["params"]["lots"], o["params"]["source"]) == (4000.6, 0.01, "spot")
    assert paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990, target=4020)["id"] == o["id"]   # no duplicate

    mkt.now = at(14, 0, 30)
    assert paper.approve("u1", o["id"])["state"] == "working"
    assert paper.tick()["filled"] == 0                              # no bar after the confirm has closed yet

    mkt.all.append(bar(14, 1, 4000.1, 4001, 3999.5, 4000.5))
    mkt.all.append(bar(14, 2, 4005, 4021.0, 4004, 4018))            # bid high 4020.70: through the target
    mkt.now = at(14, 3)
    s = paper.tick()
    assert (s["filled"], s["closed"]) == (1, 1)
    t = fdb.tables["global_trades"][0]
    assert (t["entry"], t["exit"], t["exit_reason"]) == (4000.5, 4020.0, "target")
    assert (t["net_pnl"], t["costs"], t["gross_pnl"]) == (19.5, 0.7, 20.2)
    assert t["r_multiple"] == pytest.approx(19.5 / 10.7, abs=1e-3)
    assert t["trading_day"] == "2026-11-03" and t["price_source"] == "spot" and t["rules_followed"] is True
    acct = paper.overview("u1")["account"]
    assert (acct["capital"], acct["realized_today"], acct["entries_today"]) == (10_019.5, 19.5, 1)


def test_flat_by_closes_an_open_trade(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)
    o = paper.propose("u1", symbol="XAUUSD", side="sell", stop=4010, target=3950)
    mkt.now = at(14, 0, 10)
    paper.approve("u1", o["id"])
    _flat(mkt, at(14, 0), 4 * 60 + 5, 4001.0)                        # nothing happens until 18:00 UTC (13:00 New York)
    mkt.now = at(18, 6)
    paper.tick()
    t = fdb.tables["global_trades"][0]
    assert t["exit_reason"] == "time" and t["exit_ts"].startswith("2026-11-03T18:00")
    assert t["exit"] == round(4001.0 + 0.3 + 0.1, 2)                 # bought back at the ask plus slippage


def test_kill_switch_closes_cancels_and_blocks_until_the_next_day(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990, target=4020)
    mkt.now = at(14, 0, 10)
    paper.approve("u1", o["id"])
    _flat(mkt, at(14, 0), 5, 4000.0)
    mkt.now = at(14, 5)
    paper.tick()
    waiting = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3995, target=4030)
    out = paper.kill_switch("u1")
    assert out == {"cancelled_orders": 1, "closing_positions": 1}
    assert next(r for r in fdb.tables["global_orders"] if r["id"] == waiting["id"])["state"] == "cancelled"
    _flat(mkt, at(14, 5), 2, 4002.0)
    mkt.now = at(14, 7)
    paper.tick()
    t = fdb.tables["global_trades"][0]
    assert t["exit_reason"] == "kill switch" and t["exit"] == round(4002.0 - 0.3 - 0.1, 2)
    again = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3995, target=4030)
    assert "the kill switch is on for this trading day" in again["risk_verdict"]["hard"]


def test_override_takes_a_blocked_ticket_and_records_it(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)
    fdb.insert("global_limits", {"user_id": "u1", "max_trades_per_day": 1})
    fdb.insert("global_accounts", {"user_id": "u1", "starting_capital": 10_000})
    fdb.insert("global_trades", {"user_id": "u1", "mode": "paper", "symbol": "XAUUSD", "side": "buy", "lots": 0.01, "units": 1,
                                 "price_source": "spot", "entry": 3990, "entry_ts": at(9).isoformat(), "exit_ts": at(10).isoformat(),
                                 "net_pnl": 5.0, "trading_day": "2026-11-03", "entry_context": {"stop": 3980}})
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990)
    assert o["risk_verdict"]["overridable"]
    mkt.now = at(14, 0, 10)
    assert paper.approve("u1", o["id"])["state"] == "rejected"      # a plain confirm respects the block
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3991)
    taken = paper.approve("u1", o["id"], override=True, reason="testing")
    assert taken["state"] == "working" and taken["params"]["override"]["note"] == "testing"
    _flat(mkt, at(14, 0), 2, 4000.0)
    mkt.now = at(14, 2)
    paper.tick()
    assert fdb.tables["global_trades"][-1]["rules_followed"] is False


def test_overriding_the_entry_window_lets_the_order_fill_until_flat_by(world):
    fdb, mkt = world
    _flat(mkt, at(16, 30), 10, 4000.0)                              # 11:30 New York: the entry window ended at 11:00
    mkt.now = at(16, 40)
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990)
    assert any("entry window" in r for r in o["risk_verdict"]["reasons"]) and o["risk_verdict"]["overridable"]
    assert paper.approve("u1", o["id"], override=True, reason="testing")["state"] == "working"
    assert paper.tick()["expired"] == 0                             # it used to expire here at once
    _flat(mkt, at(16, 40), 2, 4000.0)
    mkt.now = at(16, 42)
    assert paper.tick()["filled"] == 1
    assert fdb.tables["global_trades"][0]["rules_followed"] is False


def test_bars_after_the_order_ran_out_cannot_fill_it(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990)
    mkt.now = at(14, 0, 10)
    paper.approve("u1", o["id"])
    _flat(mkt, at(14, 0), 11, 4005.0)                               # above the limit for its 10 minutes...
    mkt.all.append(bar(14, 11, 4000, 4000.2, 3999.8, 4000))          # ...then back, in the same delayed batch
    mkt.now = at(14, 12)
    s = paper.tick()
    assert (s["filled"], s["expired"]) == (0, 1)


def test_an_unfilled_entry_expires_by_bar_time(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990)
    mkt.now = at(14, 0, 10)
    paper.approve("u1", o["id"])
    _flat(mkt, at(14, 0), 11, 4005.0)                               # ran away above the limit
    mkt.now = at(14, 11)
    assert paper.tick()["expired"] == 1
    assert fdb.tables["global_orders"][0]["params"]["expired_because"] == "not filled within 10 minutes"


def test_paper_does_not_load_nse_trading_code():
    import subprocess
    import sys
    code = ("import sys; import app.global_markets.paper, app.global_markets.api; "
            "print(sorted(m for m in sys.modules if m.startswith(('app.core.calendar', 'app.core.instruments', 'app.paper', "
            "'app.risk', 'app.live', 'app.options', 'app.journal', 'app.signals', 'app.market.news', 'app.market.yahoo', "
            "'app.market.nse'))))")      # shared maths (app.core.manage, its tick rounding) is allowed
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_costs_model_is_the_backtests():
    assert paper.COSTS == CostModel() and paper.QUOTES.spread == 0.60


def _open_buy(world):
    fdb, mkt = world
    _flat(mkt, at(13, 50), 10, 4000.0)
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990, target=4020)
    mkt.now = at(14, 0, 10)
    paper.approve("u1", o["id"])
    _flat(mkt, at(14, 0), 5, 4000.0)
    mkt.now = at(14, 5, 20)
    paper.tick()
    return fdb.tables["global_trades"][0]


def test_a_close_fills_at_once_at_the_live_price(world, monkeypatch):
    fdb, mkt = world
    t = _open_buy(world)
    monkeypatch.setattr(paper.feed, "quote", lambda s, src=None: paper.feed.Quote(4008.0, mkt.now.timestamp() - 2, "test"))
    o = paper.close_position("u1", t["id"])
    assert o["state"] == "filled" and o["params"]["fill_from"] == "live price" and o["params"]["quote"] == 4008.0
    t = fdb.tables["global_trades"][0]
    assert t["exit"] == round(4008.0 - 0.3 - 0.1, 2) and t["exit_ts"].startswith("2026-11-03T14:05:20")   # sold at the bid
    assert t["exit_reason"] == "manual" and mkt.closed == [t["id"]]
    assert paper.tick()["closed"] == 0                                      # nothing left for the loop to fill


def test_a_close_without_a_fresh_price_waits_for_the_next_bar(world, monkeypatch):
    fdb, mkt = world
    t = _open_buy(world)
    monkeypatch.setattr(paper.feed, "quote", lambda s, src=None: paper.feed.Quote(4008.0, mkt.now.timestamp() - 60, "test"))
    assert paper.close_position("u1", t["id"])["state"] == "working"         # a minute-old price is not used
    assert paper.overview("u1")["positions"][0]["closing"] is True          # the page shows "Closing…", not a live button
    with pytest.raises(paper.PaperError, match="already working"):
        paper.close_position("u1", t["id"])
    _flat(mkt, at(14, 6), 2, 4003.0)
    mkt.now = at(14, 8)
    paper.tick()
    assert fdb.tables["global_trades"][0]["exit"] == round(4003.0 - 0.3 - 0.1, 2)
