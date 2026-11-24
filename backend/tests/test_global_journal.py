"""X6: US releases, gold headlines, the why card, post-mortems and reviews for global paper trades."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.global_markets import journal, macro, news, paper, signals
from app.market.models import Bar

from .fakes import FakeDB
from .test_global_paper import Market, _flat, at, bar  # noqa: F401 - shared helpers

UTC = dt.timezone.utc


# ---------------------------------------------------------------- US releases

def test_releases_come_from_the_official_schedules_in_new_york_time():
    cpi = [e for e in macro.EVENTS if e.name.startswith("US CPI") and e.at.date() == dt.date(2026, 11, 10)][0]
    assert cpi.at.astimezone(UTC) == dt.datetime(2026, 11, 10, 13, 30, tzinfo=UTC)          # 08:30 New York, standard time
    jobs = [e for e in macro.EVENTS if e.name.startswith("US jobs") and e.at.date() == dt.date(2027, 6, 4)][0]
    assert jobs.at.astimezone(UTC) == dt.datetime(2027, 6, 4, 12, 30, tzinfo=UTC)           # daylight saving
    fed = [e for e in macro.EVENTS if e.name.startswith("Fed") and e.at.date() == dt.date(2026, 12, 9)][0]
    assert fed.at.astimezone(UTC) == dt.datetime(2026, 12, 9, 19, 0, tzinfo=UTC)
    assert macro.coverage_ends() == dt.date(2027, 12, 3)


def test_ticket_warnings_around_a_release():
    before = dt.datetime(2026, 11, 10, 13, 10, tzinfo=UTC)                                   # 20 minutes before CPI
    w = macro.warnings_for(before)
    assert w == ["US CPI (inflation) at 19:00 IST (in 20 minutes): price can jump past your stop"]
    assert "10 minutes ago" in macro.warnings_for(dt.datetime(2026, 11, 10, 13, 40, tzinfo=UTC))[0]
    assert macro.warnings_for(dt.datetime(2026, 11, 10, 14, 0, tzinfo=UTC)) == []
    early = dt.datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    held = macro.warnings_for(early, until=dt.datetime(2026, 11, 10, 18, 0, tzinfo=UTC))
    assert held == ["US CPI (inflation) at 19:00 IST comes while this trade can still be open"]
    assert "has run out" in macro.warnings_for(dt.datetime(2028, 1, 4, 12, 0, tzinfo=UTC))[-1]


# ---------------------------------------------------------------- headlines

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Gold climbs as dollar slips - Reuters</title><source>Reuters</source><pubDate>Tue, 10 Nov 2026 12:00:00 GMT</pubDate><link>https://example.com/a</link></item>
<item><title>Gold climbs as dollar slips - Reuters</title><source>Reuters</source><pubDate>Tue, 10 Nov 2026 11:00:00 GMT</pubDate></item>
<item><title>Stocks rally on tech earnings - CNBC</title><source>CNBC</source><pubDate>Tue, 10 Nov 2026 13:00:00 GMT</pubDate></item>
<item><title>Gold rate today in Mumbai, check prices - Site</title><source>Site</source><pubDate>Tue, 10 Nov 2026 13:00:00 GMT</pubDate></item>
<item><title>XAU/USD holds near 4,030 ahead of CPI - FXStreet</title><source>FXStreet</source><pubDate>Tue, 10 Nov 2026 13:05:00 GMT</pubDate></item>
</channel></rss>"""


def test_headlines_keep_gold_news_only_newest_first_and_never_raise():
    news._cache.clear()
    out = news.headlines("XAUUSD", fetch=lambda q: RSS)
    assert [h["title"] for h in out["headlines"]] == ["XAU/USD holds near 4,030 ahead of CPI", "Gold climbs as dollar slips"]
    assert out["headlines"][1]["source"] == "Reuters" and out["error"] is None
    news._cache.clear()

    def down(q):
        raise ConnectionError("offline")
    bad = news.headlines("XAUUSD", fetch=down)
    assert bad["headlines"] == [] and "could not be fetched" in bad["error"]
    news._cache.clear()


# ---------------------------------------------------------------- the why card

def _day_bars() -> list[Bar]:
    """Mon 2 Nov (trading day starts Sun 23:00 UTC) and Tue 3 Nov, New York on standard time (London 08:00 = 08:00 UTC)."""
    out = []
    t = dt.datetime(2026, 11, 2, 0, 0, tzinfo=UTC)               # Monday's Asian hours
    while t < dt.datetime(2026, 11, 2, 21, 0, tzinfo=UTC):
        p = 3990.0 if t.hour < 8 else 4000.0
        out.append(Bar(int(t.timestamp()), p, p + 1, p - 1, p, 0, True))
        t += dt.timedelta(minutes=1)
    t = dt.datetime(2026, 11, 2, 23, 0, tzinfo=UTC)              # Tuesday's trading day
    while t < dt.datetime(2026, 11, 3, 14, 0, tzinfo=UTC):
        p = 4012.0 if (t.day == 3 and t.hour >= 8) else 4005.0
        out.append(Bar(int(t.timestamp()), p, p + 2, p - 2, p, 0, True))
        t += dt.timedelta(minutes=1)
    return out


def test_card_says_why_with_levels_typical_move_events_and_low_confidence():
    now = dt.datetime(2026, 11, 3, 14, 0, tzinfo=UTC)
    c = signals.card(symbol="XAUUSD", side="buy", lots=0.02, units=2.0, limit=4012.6, stop=4004.6, target=4028.6,
                     why="  London held above the Asian high  ", max_loss=16.4, source="futures", bars=_day_bars(), now=now,
                     flat_by=dt.datetime(2026, 11, 3, 18, 0, tzinfo=UTC), headlines=[{"title": "Gold steadies"}] * 5,
                     warnings=["priced from COMEX futures"])
    assert c["why"] == "London held above the Asian high"
    assert c["plan"]["risk_per_oz"] == 8.0 and c["plan"]["reward_risk"] == 2.0 and c["plan"]["flat_by_ist"] == "23:30"
    lv = c["levels"]
    assert (lv["asia_low"], lv["asia_high"]) == (4003.0, 4007.0)
    assert (lv["yesterday_low"], lv["yesterday_high"], lv["yesterday_close"]) == (3989.0, 4001.0, 4000.0)
    assert c["typical_5m_move"] == 4.0
    assert any("above it" in x for x in c["context"]) and any("above yesterday's high" in x for x in c["context"])
    assert any("2.0 of those away" in x for x in c["context"])
    assert c["confidence"]["grade"] == "low" and len(c["headlines"]) == 3
    assert c["events"] == []                                     # no US release on 3 Nov
    assert "10 minutes late" in c["prices_from"]
    blank = signals.card(symbol="XAUUSD", side="sell", lots=0.01, units=1.0, limit=4000, stop=4010, target=None, why="",
                         max_loss=None, source="spot", bars=[], now=now, flat_by=None)
    assert blank["why"].startswith("No reason was written") and blank["typical_5m_move"] is None


# ---------------------------------------------------------------- the card on a real ticket, and the post-mortem hook

@pytest.fixture
def world(monkeypatch):
    fdb, mkt = FakeDB(), Market()
    monkeypatch.setattr(paper, "db", lambda: fdb)
    monkeypatch.setattr(journal, "db", lambda: fdb)
    monkeypatch.setattr(paper, "bars", mkt.bars)
    monkeypatch.setattr(paper, "_now", lambda: mkt.now)
    monkeypatch.setattr(paper.feed, "default_source", lambda: "spot")
    monkeypatch.setattr(paper, "_headlines", lambda symbol: [{"title": "Gold steadies", "source": "Test", "time": None}])
    closed: list[str] = []
    monkeypatch.setattr(paper, "on_trade_closed", closed.append)
    mkt.closed = closed
    monkeypatch.setattr(journal, "get_settings", lambda: SimpleNamespace(llm_configured=False))
    return fdb, mkt


def _trade_to_stop(fdb, mkt, why="Broke the Asian high"):
    _flat(mkt, at(13, 50), 10, 4000.0)
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990, target=4020, why=why)
    mkt.now = at(14, 0, 30)
    paper.approve("u1", o["id"])
    mkt.all.append(bar(14, 1, 4000.1, 4001, 3999.5, 4000.5))
    mkt.all.append(bar(14, 2, 4000, 4000.5, 3985, 3986))           # through the stop
    mkt.now = at(14, 3)
    paper.tick()
    return o, fdb.tables["global_trades"][0]


def test_a_ticket_carries_its_card_into_the_trade_and_closing_schedules_the_post_mortem(world):
    fdb, mkt = world
    o, t = _trade_to_stop(fdb, mkt)
    card = o["params"]["card"]
    assert card["why"] == "Broke the Asian high" and card["headlines"][0]["title"] == "Gold steadies"
    assert card["plan"]["entry"] == o["params"]["limit"] and card["risks"] == o["risk_verdict"]["warnings"]
    assert t["entry_context"]["card"] == card and t["exit_reason"] == "stop"
    assert mkt.closed == [t["id"]]


def test_a_ticket_before_cpi_warns_but_is_not_blocked(world, monkeypatch):
    fdb, mkt = world
    day = dt.datetime(2026, 11, 10, tzinfo=UTC)
    mkt.now = day.replace(hour=13, minute=10)                       # 08:10 New York, CPI at 08:30
    for i in range(10):
        t = day.replace(hour=13) + dt.timedelta(minutes=i)
        mkt.all.append(Bar(int(t.timestamp()), 4000, 4000.2, 3999.8, 4000, 0, True))
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990)
    v = o["risk_verdict"]
    assert v["verdict"] == "PASS"
    assert v["warnings"][0] == "US CPI (inflation) at 19:00 IST (in 20 minutes): price can jump past your stop"
    assert o["params"]["card"]["events"][0]["name"] == "US CPI (inflation)"


def test_post_mortem_from_facts_with_a_template_when_no_model(world):
    fdb, mkt = world
    _, t = _trade_to_stop(fdb, mkt, why="")
    row = journal.write(t["id"])
    assert row["ai_model"] == "template"
    assert "no reason written for the trade" in row["mistake_tags"]
    f = row["facts"]
    assert f["exit_reason"] == "stop" and f["session_at_entry"] == "New York session"
    assert f["entry_time"] == "19:31 IST (09:01 New York)" and f["us_releases_during_trade"] == []
    assert f["result_in_R"] == t["r_multiple"] and "rupees" not in str(row) and "NSE" not in str(row)
    journal.save_notes("u1", t["id"], "I chased it")
    again = journal.write(t["id"])                                   # rewriting keeps your notes
    assert again["user_notes"] == "I chased it" and len(fdb.tables["global_trade_journal"]) == 1


def test_a_model_reply_with_made_up_numbers_is_thrown_away(world, monkeypatch):
    fdb, mkt = world
    _, t = _trade_to_stop(fdb, mkt)
    monkeypatch.setattr(journal, "get_settings", lambda: SimpleNamespace(llm_configured=True))
    monkeypatch.setattr(journal, "chat_model", lambda: "gpt-test")
    reply = {"summary": "The stop was hit; gold wins 73% of the time after this.", "went_right": [], "went_wrong": [],
             "lesson": "none", "mistake_tags": ["no mistake"]}
    monkeypatch.setattr(journal, "chat_json", lambda *a, **k: reply)
    assert journal.write(t["id"])["ai_model"] == "template"
    reply["summary"] = "You bought at the London session and the stop was hit, a normal loss."
    good = journal.write(t["id"])
    assert good["ai_model"] == "gpt-test" and good["ai_postmortem"].startswith("You bought")


def test_reviews_count_by_trading_day_and_session(world):
    fdb, mkt = world
    _trade_to_stop(fdb, mkt)
    st = journal.stats("u1", dt.date(2026, 11, 3), dt.date(2026, 11, 3))
    assert st["trades"] == 1 and st["losses"] == 1 and st["exit_reasons"] == {"stop": 1}
    assert st["by_session_at_entry"] == {"New York session": {"trades": 1, "net_dollars": st["net_pnl_dollars"]}}
    r = journal.review("u1", "daily", dt.date(2026, 11, 3))
    assert r["body"]["headline"].startswith("1 trades") and r["model"] is None
    empty = journal.review("u1", "weekly", dt.date(2026, 11, 20))
    assert empty["stats"]["trades"] == 0 and empty["body"]["headline"].startswith("No gold trades")
