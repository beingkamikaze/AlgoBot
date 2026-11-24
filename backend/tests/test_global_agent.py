"""The gold agent: the watcher's facts and triggers, the strategist / rules / reviewer chain, the look budget, tickets that
still need the risk check and your confirmation, and the scorekeeper. GPT-5 is replaced by a scripted fake."""

from __future__ import annotations

import datetime as dt
import inspect
from types import SimpleNamespace

import pytest

from app.global_markets import paper
from app.global_markets.agent import brain, desk, snapshot
from app.market.models import Bar

from .fakes import FakeDB

UTC = dt.timezone.utc
SUN = dt.datetime(2026, 11, 1, 23, 0, tzinfo=UTC)      # Monday's trading day opens (winter: New York is UTC-5)
TUE_OPEN = dt.datetime(2026, 11, 2, 23, 0, tzinfo=UTC)  # Tuesday's trading day opens
LONDON = dt.datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
NOW = dt.datetime(2026, 11, 3, 8, 30, tzinfo=UTC)        # inside the entry window


def path(t: dt.datetime) -> float:
    """Flat near 4000 until London opens on Tuesday, then rising 0.10 a minute."""
    if t < LONDON:
        return 4000.0
    return round(4000.0 + 0.1 * (t - LONDON).total_seconds() / 60, 2)


def one_minute(until: dt.datetime) -> list[Bar]:
    out, t = [], SUN
    while t < until:
        p = path(t)
        if not (dt.datetime(2026, 11, 2, 22, 0, tzinfo=UTC) <= t < TUE_OPEN):   # the daily break
            out.append(Bar(int(t.timestamp()), p, p + 1.0, p - 1.0, p, 0, True))
        t += dt.timedelta(minutes=1)
    return out


def group(m1: list[Bar], secs: int) -> list[Bar]:
    out: dict[int, list] = {}
    for b in m1:
        k = b.t - b.t % secs
        if k not in out:
            out[k] = [b.o, b.h, b.l, b.c]
        else:
            g = out[k]
            g[1], g[2], g[3] = max(g[1], b.h), min(g[2], b.l), b.c
    return [Bar(k, *v, 0, True) for k, v in sorted(out.items())]


def market(until: dt.datetime) -> dict:
    m1 = one_minute(until)
    return {"1m": m1, "5m": group(m1, 300), "15m": group(m1, 900), "1h": group(m1, 3600)}


class FakeModel:
    """Scripted GPT-5: the strategist's and reviewer's answers, and a record of the calls."""

    def __init__(self, strategist: dict | None = None, reviewer: dict | None = None):
        self.answers = {"global_agent_strategist": strategist or PASS, "global_agent_reviewer": reviewer or OK}
        self.calls: list[str] = []

    def __call__(self, system, user, schema, *, name, purpose, **kw):
        self.calls.append(name)
        return dict(self.answers[name])


PASS = {"decision": "pass", "side": "none", "setup": "other", "stop": 0, "target": 0, "why": [], "wrong_if": "",
        "pass_reason": "Nothing clear yet: price is drifting."}
OK = {"verdict": "ok", "concerns": [], "summary": "No serious problem found."}


def buy(price: float, stop_off: float = 4.0, target_off: float = 8.0, why=None) -> dict:
    return {"decision": "trade", "side": "buy", "setup": "breakout", "stop": price - stop_off, "target": price + target_off,
            "why": why or ["Price has risen above the Asian high since London opened", "The 5m trend is up"],
            "wrong_if": "A close back inside the Asian range", "pass_reason": ""}


@pytest.fixture
def fdb(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(desk, "db", lambda: d)
    monkeypatch.setattr(desk, "get_settings", lambda: SimpleNamespace(
        global_agent_enabled=True, llm_configured=True, global_agent_looks_per_day=16, global_agent_cooldown_minutes=15))
    monkeypatch.setattr(desk, "chat_model", lambda: "gpt-5")
    monkeypatch.setattr(desk.feed, "default_source", lambda: "spot")
    desk.state.update(last_facts=None, last_look=None, status="", error=None)
    return d


def look(now: dt.datetime, model: FakeModel, force: bool = False):
    return desk.think(now, force=force, bars_by_tf=market(now), headlines=[], call=model)


# ---------------------------------------------------------------- the watcher

def test_the_snapshot_uses_only_closed_bars_and_measures_levels_and_trends():
    bars = market(NOW + dt.timedelta(minutes=30))                     # bars beyond "now" must be ignored
    f = snapshot.build(NOW, bars, source="spot", headlines=[{"ist": "13:55 IST", "title": "Gold firms"}])
    assert f["price"]["last"] == path(NOW - dt.timedelta(minutes=1)) == 4002.9
    assert f["price"]["bar_closed_ny"] == "03:30" and f["now"]["session"] == "london"
    assert f["levels"]["asia_high"] == 4001.0 and f["levels"]["yesterday_high"] == 4001.0
    assert f["trend"]["5m"]["direction"] == "up" and f["typical_5m_move"] > 0
    assert f["recent_5m_bars"][-1]["closed_ny"] == "03:30" and len(f["recent_5m_bars"]) == 12
    assert f["window"]["flat_by_ny"] == "13:00" and f["headlines"][0]["title"] == "Gold firms"


def test_triggers_fire_on_news_in_the_market_and_stay_quiet_otherwise():
    a = snapshot.build(NOW, market(NOW), source="spot", headlines=[])
    assert snapshot.triggers(None, a, None, NOW) == ["first look of the trading day"]
    assert snapshot.triggers(a, a, NOW, NOW + dt.timedelta(minutes=5)) == []
    assert "routine check" in snapshot.triggers(a, a, NOW, NOW + dt.timedelta(minutes=61))[0]
    b = {**a, "price": {**a["price"], "last": 4010.0}, "levels": {**a["levels"], "asia_high": 4005.0}}
    assert "price rose through the Asian high (4005.0)" in snapshot.triggers(a, b, NOW, NOW)
    c = {**a, "headlines": [{"ist": "x", "title": "Fed surprises markets"}]}
    assert snapshot.triggers(a, c, NOW, NOW) == ["new headline: Fed surprises markets"]


# ---------------------------------------------------------------- the agents and the rules

def test_a_pass_is_stored_with_its_reason_and_the_reviewer_is_not_called(fdb):
    model = FakeModel()
    idea = look(NOW, model)
    assert model.calls == ["global_agent_strategist"]
    assert (idea["kind"], idea["state"], idea["triggers"]) == ("pass", "passed", ["first look of the trading day"])
    assert idea["proposal"]["pass_reason"].startswith("Nothing clear")


def test_a_good_idea_is_reviewed_and_lives_for_ten_minutes(fdb):
    model = FakeModel(strategist=buy(4002.9))
    idea = look(NOW, model)
    assert model.calls == ["global_agent_strategist", "global_agent_reviewer"]
    assert (idea["kind"], idea["state"], idea["side"], idea["stop"], idea["target"]) == ("trade", "new", "buy", 3998.9, 4010.9)
    assert idea["expires_at"].startswith("2026-11-03T08:40")
    assert idea["proposal"]["plan"] == {"entry": 4002.9, "stop": 3998.9, "target": 4010.9, "risk": 4.0, "reward": 8.0,
                                        "reward_risk": 2.0}
    assert idea["review"]["by"] == "reviewer" and idea["facts"]["price"]["last"] == 4002.9
    assert desk.expire(NOW + dt.timedelta(minutes=11)) == 1


def test_the_rules_veto_bad_stops_thin_rewards_and_made_up_numbers_without_asking_the_reviewer(fdb):
    f = snapshot.build(NOW, market(NOW), source="spot", headlines=[])
    assert brain.rule_check(f, buy(4002.9, stop_off=-1)) == ["for a buy the stop must be below the price and the target above it"]
    assert "inside ordinary noise" in brain.rule_check(f, buy(4002.9, stop_off=0.5, target_off=2))[0]
    assert "reward is only 1.00" in brain.rule_check(f, buy(4002.9, stop_off=4, target_off=4))[0]
    made_up = buy(4002.9, why=["Gold usually bounces from 4321.5"])
    assert "not in the facts: 4321.5" in brain.rule_check(f, made_up)[0]
    model = FakeModel(strategist=made_up)
    idea = look(NOW, model)
    assert model.calls == ["global_agent_strategist"]
    assert idea["state"] == "vetoed" and idea["review"]["by"] == "rules"


def test_the_reviewer_can_veto_but_never_approve(fdb):
    idea = look(NOW, FakeModel(strategist=buy(4002.9), reviewer={"verdict": "reject", "concerns": ["chasing"],
                                                                   "summary": "Too late."}))
    assert idea["state"] == "vetoed"
    with pytest.raises(desk.AgentError, match="turned down"):
        desk.ticket("u1", idea["id"], NOW)
    assert "approve" not in REVIEWER_WORDS


REVIEWER_WORDS = brain.REVIEWER_SCHEMA["properties"]["verdict"]["enum"]


def test_the_agent_looks_only_when_something_happened_and_keeps_to_its_budget(fdb, monkeypatch):
    model = FakeModel()
    assert look(NOW, model) is not None                                        # first look of the day
    assert look(NOW + dt.timedelta(minutes=5), model) is None                  # nothing new
    assert look(NOW + dt.timedelta(minutes=5), model, force=True) is not None  # you asked
    assert len(model.calls) == 2
    assert look(dt.datetime(2026, 11, 3, 7, 30, tzinfo=UTC), FakeModel()) is None   # before London: outside the window
    with pytest.raises(desk.AgentError, match="closed"):
        look(dt.datetime(2026, 11, 7, 12, 0, tzinfo=UTC), FakeModel(), force=True)  # Saturday
    monkeypatch.setattr(desk, "looks_today", lambda now: 16)
    with pytest.raises(desk.AgentError, match="looks are used"):
        look(NOW + dt.timedelta(minutes=6), FakeModel(), force=True)


def test_the_agent_code_never_sizes_or_places_a_trade():
    src = inspect.getsource(desk) + inspect.getsource(brain)
    assert "approve(" not in src and "max_lots" not in src and "lots=" not in src
    assert "paper.propose(" in inspect.getsource(desk.ticket)                 # a ticket only: you still confirm it


# ---------------------------------------------------------------- tickets

@pytest.fixture
def both(fdb, monkeypatch):
    clock = {"now": NOW}
    bars = market(NOW + dt.timedelta(minutes=30))
    monkeypatch.setattr(paper, "db", lambda: fdb)
    monkeypatch.setattr(paper, "bars", lambda s, src: [b for b in bars["1m"] if b.t + 60 <= clock["now"].timestamp()])
    monkeypatch.setattr(paper, "_now", lambda: clock["now"])
    monkeypatch.setattr(paper.feed, "default_source", lambda: "spot")
    monkeypatch.setattr(paper, "_headlines", lambda symbol: [])
    return fdb, clock


def test_an_idea_becomes_an_ordinary_ticket_that_says_it_is_untested(both):
    fdb, clock = both
    idea = look(NOW, FakeModel(strategist=buy(4002.9)))
    o = desk.ticket("u1", idea["id"], NOW + dt.timedelta(minutes=1))
    p = o["params"]
    assert o["state"] == "proposed" and p["idea_id"] == idea["id"]           # still needs your confirmation
    assert (p["stop"], p["target"]) == (3998.9, 4010.9) and p["lots"] > 0     # sized by the risk engine, not the agent
    assert p["card"]["source"] == "Gold agent idea (breakout, untested)" and p["card"]["confidence"]["grade"] == "low"


def test_a_ticket_is_refused_once_price_has_moved_on(both):
    fdb, clock = both
    idea = look(NOW, FakeModel(strategist=buy(4002.9, stop_off=2.5, target_off=5)))
    clock["now"] = NOW + dt.timedelta(minutes=9)                              # price now 4003.8: moved 0.9, over 0.3 of 2.5
    with pytest.raises(desk.AgentError, match="moved \\+0.90"):
        desk.ticket("u1", idea["id"], clock["now"])
    with pytest.raises(desk.AgentError, match="expired"):
        desk.ticket("u1", idea["id"], NOW + dt.timedelta(minutes=11))


# ---------------------------------------------------------------- the scorekeeper

def _bar(t: dt.datetime, o, h, lo, c) -> Bar:
    return Bar(int(t.timestamp()), o, h, lo, c, 0, True)


def test_outcomes_count_costs_and_take_the_stop_first():
    made = NOW
    idea = {"created_at": made.isoformat(), "side": "buy", "ref_price": 4000.0, "stop": 3996.0, "target": 4008.0}
    up = [_bar(made + dt.timedelta(minutes=i), 4000, 4000.5 + i, 3999.5, 4000 + i) for i in range(12)]
    o = desk.outcome(idea, up, made + dt.timedelta(minutes=15))
    assert o["reason"] == "target" and o["r"] == round((4008.0 - 4000.4) / 4.0, 2)      # entry pays half spread + slippage
    both_ways = [_bar(made, 4000, 4010, 3990, 4000)]
    assert desk.outcome(idea, both_ways, made)["reason"] == "stop"
    assert desk.outcome(idea, [_bar(made, 4000, 4001, 3999, 4000)], made) is None         # still running
    flat = dt.datetime(2026, 11, 3, 18, 0, tzinfo=UTC)                                   # 13:00 New York
    o = desk.outcome(idea, [_bar(flat, 4002, 4003, 4001, 4002)], flat + dt.timedelta(minutes=5))
    assert o["reason"] == "flat-by" and o["exit"] == 4001.6


def test_the_record_groups_ideas_by_what_the_checks_said_and_what_you_did(fdb):
    rows = [("a", "new", 2.0), ("b", "vetoed", -1.0), ("c", "expired", -1.0)]
    for i, st, r in rows:
        fdb.tables.setdefault("global_agent_ideas", []).append(
            {"id": i, "symbol": "XAUUSD", "kind": "trade", "state": st, "created_at": NOW.isoformat(),
             "outcome": {"status": "done", "r": r}})
    rec = desk.record({"a"})
    assert rec["all"] == {"ideas": 3, "judged": 3, "avg_r": 0.0, "win_rate": 0.33, "total_r": 0.0}
    assert rec["passed_checks"]["avg_r"] == 0.5 and rec["vetoed"]["avg_r"] == -1.0 and rec["you_took"]["total_r"] == 2.0
