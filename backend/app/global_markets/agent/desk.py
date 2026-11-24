"""The agent desk: when to look, the two agents, storing what they said, turning an idea into a paper ticket, and keeping
score.

You chose this on 22 Nov 2026: an agent that makes up trades from the live market, on paper, with your confirmation. Its
ideas cannot be backtested (GPT-5 has read about past gold prices, so a test on history would be a test it has seen the
answers to). So every idea grades low, its only evidence is its own forward record kept here, and nothing it does is used
to choose settings for the tested strategies.

Flow: watcher (code) -> trigger -> strategist (GPT-5) -> rule check (code) -> reviewer (GPT-5) -> idea on the Agent desk
-> you press "Make paper ticket" -> risk check sizes it -> you confirm. Nothing trades by itself.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from app.ai.client import AIError, chat_json, chat_model
from app.config import get_settings
from app.db import db, eq
from app.global_markets import news, paper
from app.global_markets.agent import brain, snapshot
from app.global_markets.clock import day_window, market_status, session_bounds, trading_day
from app.global_markets.costs import DEFAULT_SPREAD, CostModel
from app.global_markets.data import feed

log = logging.getLogger("global.agent")
TABLE = "global_agent_ideas"
SYMBOL = "XAUUSD"
IDEA_MINUTES = 10                  # an idea not turned into a ticket by then is gone: its moment has passed
MAX_DRIFT_R = 0.3                  # a ticket is refused if price moved more than this share of the idea's risk
HALF_SPREAD = DEFAULT_SPREAD / 2
SLIPPAGE = CostModel().slippage
CONFIDENCE = {"grade": "low", "why": "An idea the agent made up from the live market. It has no backtest and cannot have "
                                     "one (the model has read about past gold prices), so it grades low until its own paper "
                                     "record on the Agent desk says otherwise."}

_lock = threading.Lock()
state: dict = {"last_facts": None, "last_look": None, "status": "not started", "error": None, "looks_today": 0}
listeners: list = []               # called with each stored idea (Telegram registers here); a failing one never stops a look


class AgentError(Exception):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat()


def _day_start(day: dt.date) -> dt.datetime:
    b = session_bounds(day)
    return b[0] if b else dt.datetime.combine(day, dt.time(0, 0), dt.timezone.utc)


def looks_today(now: dt.datetime) -> int:
    start = _day_start(trading_day(now))
    return len(db().select(TABLE, {"symbol": eq(SYMBOL), "created_at": f"gte.{_iso(start)}"}, columns="id"))


def _market(symbol: str, source: str) -> dict:
    return {tf: feed.series(symbol, tf, source).bars for tf in ("1m", "5m", "15m", "1h")}


def _headlines(symbol: str) -> list[dict]:
    try:
        return news.headlines(symbol)["headlines"]
    except Exception:  # noqa: BLE001 - a look without headlines is still a look
        return []


# ---------------------------------------------------------------- one look

def think(now: dt.datetime | None = None, *, force: bool = False, bars_by_tf: dict | None = None,
          headlines: list[dict] | None = None, call=chat_json) -> dict | None:
    """Look at the market if something happened (or if `force`). Returns the stored idea, or None if it did not look."""
    now = now or _now()
    s = get_settings()
    with _lock:
        if not s.global_agent_enabled:
            state.update(status="off: GLOBAL_AGENT_ENABLED is false in backend/.env")
            return None
        if not s.llm_configured:
            state.update(status="off: no language model is set up in backend/.env")
            return None
        m = market_status(now)
        w = day_window(m.trading_day) if m.is_open else None
        in_window = bool(w and w.entry_start <= now < w.entry_end)
        if not m.is_open:
            state.update(status=f"waiting: {m.reason or 'the market is closed'}")
            if force:
                raise AgentError(f"the market is closed ({m.reason or 'closed'}): there is nothing to trade")
            return None
        if not in_window and not force:
            state.update(status="waiting: outside the entry window (08:00 London to 11:00 New York)")
            return None
        source = feed.default_source()
        market = bars_by_tf if bars_by_tf is not None else _market(SYMBOL, source)
        facts = snapshot.build(now, market, source=source,
                               headlines=headlines if headlines is not None else _headlines(SYMBOL), symbol=SYMBOL)
        if facts is None:
            state.update(status="waiting: no closed bars yet")
            if force:
                raise AgentError("there are no closed price bars to look at yet")
            return None
        why = snapshot.triggers(state["last_facts"], facts, state["last_look"], now)
        if force:
            why = ["you asked the agent to look now"] + why
        if not why:
            state.update(status="watching: nothing new since the last look")
            return None
        if not force and state["last_look"] and now - state["last_look"] < dt.timedelta(minutes=s.global_agent_cooldown_minutes):
            state.update(status=f"watching: cooling down (one look every {s.global_agent_cooldown_minutes} minutes)")
            return None
        used = looks_today(now)
        if used >= s.global_agent_looks_per_day:
            state.update(status=f"resting: today's {s.global_agent_looks_per_day} looks are used", looks_today=used)
            if force:
                raise AgentError(f"today's {s.global_agent_looks_per_day} looks are used; the count resets with the "
                                 "trading day (17:00 New York)")
            return None
        state.update(last_facts=facts, last_look=now)
        try:
            idea = _decide(now, facts, why, source, call)
        except AIError as e:
            state.update(status="error", error=str(e)[:300])
            log.warning("agent look failed: %s", e)
            if force:
                raise AgentError(str(e))
            return None
        state.update(status="watching", error=None, looks_today=used + 1)
        return idea


def _decide(now: dt.datetime, facts: dict, why: list[str], source: str, call) -> dict:
    p = brain.strategist(facts, call)
    row = {"symbol": SYMBOL, "price_source": source, "ref_price": facts["price"]["last"], "triggers": why, "facts": facts,
           "proposal": p, "model": chat_model()}
    if p["decision"] != "trade" or p["side"] == "none":
        row.update(kind="pass", state="passed")
        return db().insert(TABLE, row)[0]
    row.update(kind="trade", side=p["side"], setup=p["setup"], stop=round(p["stop"], 2), target=round(p["target"], 2),
               expires_at=_iso(now + dt.timedelta(minutes=IDEA_MINUTES)))
    bad = brain.rule_check(facts, p)
    if bad:
        row.update(state="vetoed", review={"by": "rules", "verdict": "reject", "concerns": bad,
                                           "summary": "The app's own checks turned this idea down."})
    else:
        p["plan"] = brain.plan(facts, p)
        r = brain.reviewer(facts, p, call)
        row.update(review={"by": "reviewer", **r}, state="vetoed" if r["verdict"] == "reject" else "new")
    row["proposal"] = {**p, "plan": brain.plan(facts, p)}
    stored = db().insert(TABLE, row)[0]
    log.info("agent idea %s %s (%s)", p["side"], SYMBOL, stored["state"])
    _tell(stored)
    return stored


def _tell(idea: dict) -> None:
    for fn in listeners:
        try:
            fn(idea)
        except Exception as e:  # noqa: BLE001 - the idea is stored and on the page whatever happens here
            log.warning("idea listener failed: %s", e)


# ---------------------------------------------------------------- ideas

def expire(now: dt.datetime | None = None) -> int:
    now = now or _now()
    d = db()
    old = d.select(TABLE, {"state": eq("new"), "expires_at": f"lt.{_iso(now)}"})
    for r in old:
        d.update(TABLE, {"id": eq(r["id"])}, {"state": "expired"})
    return len(old)


def recent(limit: int = 40) -> list[dict]:
    rows = db().select(TABLE, {"symbol": eq(SYMBOL)}, order="created_at.desc", limit=limit)
    for r in rows:
        r.pop("facts", None)                 # large; the page asks for one idea's facts when you open it
    return rows


def get(idea_id: str) -> dict | None:
    rows = db().select(TABLE, {"id": eq(idea_id)})
    return rows[0] if rows else None


def card_source(idea: dict) -> dict:
    """What a ticket made from this idea says about where it came from."""
    return {"strategy": f"Gold agent idea ({idea.get('setup') or 'other'}, untested)", "confidence": CONFIDENCE}


def ticket(user_id: str, idea_id: str, now: dt.datetime | None = None) -> dict:
    """An idea -> an ordinary paper ticket, priced now. Refused if the idea is not live or price has moved on."""
    now = now or _now()
    idea = get(idea_id)
    if not idea or idea["kind"] != "trade":
        raise AgentError("no such trade idea")
    if idea["state"] == "vetoed":
        raise AgentError("this idea was turned down by the checks: it cannot become a ticket")
    if idea["state"] != "new" or (idea.get("expires_at") and idea["expires_at"] < _iso(now)):
        raise AgentError(f"this idea has expired: ideas last {IDEA_MINUTES} minutes")
    px = paper.price_info(idea["symbol"], idea["price_source"], now)
    if px.last is None:
        raise AgentError("no price right now")
    ref, stop = float(idea["ref_price"]), float(idea["stop"])
    risk = abs(ref - stop)
    moved = px.last - ref
    if risk and abs(moved) > MAX_DRIFT_R * risk:
        raise AgentError(f"price has moved {moved:+.2f} since the idea ({px.last:.2f} now against {ref:.2f}), more than "
                         f"{MAX_DRIFT_R:g} of its {risk:.2f} risk: the idea no longer fits")
    why = "; ".join((idea.get("proposal") or {}).get("why") or [])[:300]
    return paper.propose(user_id, symbol=idea["symbol"], side=idea["side"], stop=stop, target=float(idea["target"]),
                         why=why, now=now, idea=idea)


# ---------------------------------------------------------------- scorekeeper

def outcome(idea: dict, bars: list, now: dt.datetime) -> dict | None:
    """What the idea would have done if taken at its price when it was made: the stop, the target or the flat-by exit, with
    the same assumed spread and slippage as paper trading. None while it is still running."""
    made = dt.datetime.fromisoformat(idea["created_at"])
    w = day_window(trading_day(made))
    if w is None:
        return {"status": "no trading day"}
    long = idea["side"] == "buy"
    ref, stop, target = float(idea["ref_price"]), float(idea["stop"]), float(idea["target"])
    risk = abs(ref - stop)
    entry = ref + (HALF_SPREAD + SLIPPAGE) * (1 if long else -1)
    sign = 1 if long else -1
    exit_px = reason = at = None
    for b in bars:
        if not b.complete or b.t < made.timestamp():
            continue
        if b.t + 60 > w.flat_by.timestamp():
            exit_px, reason, at = b.o - sign * (HALF_SPREAD + SLIPPAGE), "flat-by", b.t
            break
        lo, hi = (b.l - HALF_SPREAD, b.h - HALF_SPREAD) if long else (b.l + HALF_SPREAD, b.h + HALF_SPREAD)
        if (lo <= stop) if long else (hi >= stop):
            exit_px, reason, at = stop - sign * SLIPPAGE, "stop", b.t
            break
        if (hi >= target) if long else (lo <= target):
            exit_px, reason, at = target, "target", b.t
            break
    if exit_px is None:
        if now > w.flat_by + dt.timedelta(hours=36):
            return {"status": "no prices", "note": "no bars came in to judge it"}
        return None
    r = sign * (exit_px - entry) / risk if risk else 0.0
    return {"status": "done", "exit": round(exit_px, 2), "reason": reason, "r": round(r, 2),
            "exit_at": dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()}


def score(now: dt.datetime | None = None, bars_for=None) -> int:
    """Fill in outcomes for trade ideas that have finished. Returns how many it filled."""
    now = now or _now()
    d = db()
    rows = [r for r in d.select(TABLE, {"kind": eq("trade"), "outcome": "is.null"}) if r.get("stop") is not None]
    done = 0
    cache: dict = {}
    for r in rows:
        src = r["price_source"]
        if src not in cache:
            cache[src] = (bars_for or (lambda s: feed.series(SYMBOL, "1m", s).bars))(src)
        o = outcome(r, cache[src], now)
        if o is not None:
            d.update(TABLE, {"id": eq(r["id"])}, {"outcome": o})
            done += 1
    return done


def _group(rows: list[dict]) -> dict:
    rs = [r["outcome"]["r"] for r in rows if (r.get("outcome") or {}).get("status") == "done"]
    if not rs:
        return {"ideas": len(rows), "judged": 0}
    return {"ideas": len(rows), "judged": len(rs), "avg_r": round(sum(rs) / len(rs), 2),
            "win_rate": round(sum(1 for x in rs if x > 0) / len(rs), 2), "total_r": round(sum(rs), 2)}


def record(taken_ids: set[str] | None = None) -> dict:
    """The agent's forward record, the only evidence it has. Grouped by what the checks said, and by what you did."""
    rows = db().select(TABLE, {"symbol": eq(SYMBOL), "kind": eq("trade")}, order="created_at.desc", limit=1000)
    looks = db().select(TABLE, {"symbol": eq(SYMBOL)}, columns="id", limit=5000)
    taken_ids = taken_ids or set()
    return {
        "looks": len(looks),
        "all": _group(rows),
        "passed_checks": _group([r for r in rows if r["state"] != "vetoed"]),
        "vetoed": _group([r for r in rows if r["state"] == "vetoed"]),
        "you_took": _group([r for r in rows if r["id"] in taken_ids]),
        "note": "R is measured as if each idea were taken at its price when made, with the paper spread and slippage. "
                "A few dozen ideas say little: luck alone moves the average by several tenths of an R.",
    }


# ---------------------------------------------------------------- background loop

class AgentLoop:
    """Every 30 s: expire old ideas, score finished ones, and look at the market if something happened."""

    def __init__(self, every: float = 30.0):
        self.every, self._stop = every, threading.Event()
        self.thread: threading.Thread | None = None
        self._last_score = 0.0

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="global-agent-loop")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                expire()
                if time.monotonic() - self._last_score > 300:
                    score()
                    self._last_score = time.monotonic()
                think()
            except Exception as e:  # noqa: BLE001 - keep watching; say why on the page
                msg = str(e)[:300]
                if msg != state.get("error"):
                    log.warning("agent loop failed: %s", msg)
                state.update(status="error", error=msg)
            self._stop.wait(self.every)
