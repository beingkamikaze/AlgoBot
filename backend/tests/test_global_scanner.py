"""Live gold signals: fixed settings, closed bars only, the entry window, the confidence rule, no duplicates, and a ticket that
still needs the risk check and your confirmation."""

from __future__ import annotations

import datetime as dt

import pytest

from app.global_markets import paper, scanner
from app.market.models import Bar

from .fakes import FakeDB

UTC = dt.timezone.utc
# Tuesday 3 Nov 2026: the trading day starts Mon 23:00 UTC; London opens 08:00 UTC; entries until 16:00 UTC
START = dt.datetime(2026, 11, 2, 23, 0, tzinfo=UTC)
BREAK = dt.datetime(2026, 11, 3, 8, 5, tzinfo=UTC)          # this 5-minute bar closes at 08:10 above the Asian high


def five_minute(until: dt.datetime, breakout: bool = True) -> list[Bar]:
    out, t = [], START
    while t < until:
        if breakout and t >= BREAK:
            o, h, lo, c = 4001.0, 4012.0, 4000.5, 4010.0
        else:
            o, h, lo, c = 4000.0, 4001.0, 3999.0, 4000.0
        out.append(Bar(int(t.timestamp()), o, h, lo, c, 0, True))
        t += dt.timedelta(minutes=5)
    return out


REPORT = {"summary": {"trades": 105, "expectancy_r": -0.16, "profit_factor": 0.8, "win_rate": 0.3},
          "verdict": {"text": "Does not pass: it loses after costs."}}


@pytest.fixture
def fdb(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(scanner, "db", lambda: d)
    monkeypatch.setattr(scanner.feed, "default_source", lambda: "spot")
    monkeypatch.setattr(scanner.research, "latest", lambda: {"asia-breakout": REPORT})
    return d


def _scan(now: dt.datetime, bars: list[Bar]) -> list[dict]:
    return scanner.scan(now, bars_by_tf={"5m": bars, "1m": []})


def test_a_breakout_after_london_opens_becomes_a_signal_with_its_why(fdb):
    now = dt.datetime(2026, 11, 3, 8, 11, tzinfo=UTC)
    rows = _scan(now, five_minute(BREAK + dt.timedelta(minutes=5)))
    asia = [r for r in rows if r["strategy_id"] == "asia-breakout"]
    assert len(asia) == 1
    s = asia[0]
    assert (s["side"], s["ref_price"], s["target_r"], s["state"]) == ("buy", 4010.0, 2.0, "new")
    assert s["signal_ts"].startswith("2026-11-03T08:10")
    e = s["explanation"]
    assert "above the Asian high" in e["rule"] and e["invalidation"] and e["stop_reason"].startswith("2 x ATR(14)")
    assert e["confidence"] == "low" and e["evidence"][0]["trades"] == 105          # the fixed rule: it loses after costs
    card = e["card"]
    assert card["source"] == "Asian range breakout" and card["confidence"]["grade"] == "low"
    assert "105 trades" in card["confidence"]["why"] and "Does not pass" in card["confidence"]["why"]
    assert card["plan"]["reward_risk"] == 2.0 and card["risks"] == []                # seen 1 minute after the close
    assert _scan(now + dt.timedelta(seconds=30), five_minute(BREAK + dt.timedelta(minutes=5))) == []   # no duplicate


def test_a_late_signal_says_it_is_later_than_tested_and_gives_five_minutes(fdb):
    now = dt.datetime(2026, 11, 3, 8, 21, tzinfo=UTC)                            # 11 minutes after the close (delayed feed)
    bars = five_minute(BREAK + dt.timedelta(minutes=5))                          # the delayed feed has not shown later bars
    s = [r for r in _scan(now, bars) if r["strategy_id"] == "asia-breakout"][0]
    assert "seen 11 minutes after" in s["explanation"]["card"]["risks"][0]
    assert s["expires_at"].startswith("2026-11-03T08:26")
    assert scanner.expire(dt.datetime(2026, 11, 3, 8, 27, tzinfo=UTC)) == 1
    assert fdb.tables["global_signals"][0]["state"] == "expired"


def test_nothing_outside_the_entry_window_or_while_closed_or_from_an_unclosed_bar(fdb):
    assert _scan(dt.datetime(2026, 11, 3, 7, 30, tzinfo=UTC), five_minute(dt.datetime(2026, 11, 3, 7, 30, tzinfo=UTC))) == []
    assert _scan(dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC), five_minute(BREAK)) == []                    # Saturday
    forming = five_minute(BREAK + dt.timedelta(minutes=5))
    forming[-1] = Bar(forming[-1].t, 4001.0, 4012.0, 4000.5, 4010.0, 0, False)
    assert _scan(dt.datetime(2026, 11, 3, 8, 9, tzinfo=UTC), forming) == []


def test_the_scanner_never_trades_and_never_tunes():
    import inspect
    src = inspect.getsource(scanner)
    assert "paper." not in src.replace("paper trade", "") and "propose" not in src
    assert "make(sid)" in src                                                    # each strategy at its fixed settings


# ---------------------------------------------------------------- a ticket from a signal

def test_a_ticket_from_a_signal_keeps_the_strategy_and_sets_the_target_from_its_own_entry(monkeypatch, fdb):
    from .test_global_paper import Market, _flat, at
    pdb, mkt = FakeDB(), Market()
    monkeypatch.setattr(paper, "db", lambda: pdb)
    monkeypatch.setattr(paper, "bars", mkt.bars)
    monkeypatch.setattr(paper, "_now", lambda: mkt.now)
    monkeypatch.setattr(paper.feed, "default_source", lambda: "spot")
    monkeypatch.setattr(paper, "_headlines", lambda symbol: [])
    _flat(mkt, at(13, 50), 10, 4000.0)
    signal = {"id": "sig-1", "strategy_id": "asia-breakout",
              "explanation": {"card": {"source": "Asian range breakout", "confidence": {"grade": "low", "why": "105 trades, -0.16R"}}}}
    o = paper.propose("u1", symbol="XAUUSD", side="buy", stop=3990, target_r=2.0, why="First close above the Asian high",
                      signal=signal)
    p = o["params"]
    assert p["limit"] == 4000.6 and p["target"] == round(4000.6 + 2 * 10.6, 2) and p["signal_id"] == "sig-1"
    assert p["card"]["source"] == "Asian range breakout" and p["card"]["confidence"]["why"] == "105 trades, -0.16R"
    assert o["state"] == "proposed"                                              # still needs your confirmation
