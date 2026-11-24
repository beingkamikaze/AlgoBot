"""The three XAUUSD strategies and the wall around held-back days (plan phase X4). All prices here are made up."""

import datetime as dt

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.global_markets import research
from app.global_markets.backtest import from_columns, run
from app.global_markets.clock import LONDON, NEW_YORK, is_trading_day
from app.global_markets.data import backfill, store
from app.global_markets.data.bars import Ohlcv
from app.global_markets.research_window import HELD_BACK_FROM, RESEARCH_START
from app.global_markets.strategies.base import Frame, lookahead_violations
from app.global_markets.strategies.library import STRATEGIES, AsiaBreakout, FailedBreak, NyOpenRange, make, ny_open_range
from app.main import app


def at(y, m, d, hh, mm=0, tz=NEW_YORK):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


def frame(start, rows, step=300):
    """5-minute bars from (open, high, low, close), or one number for a flat bar with a $1 range."""
    rows = [(r, r + 0.5, r - 0.5, r) if isinstance(r, (int, float)) else r for r in rows]
    a = np.array(rows, dtype=float)
    return Frame("XAUUSD", "5m", Ohlcv(start + step * np.arange(len(rows), dtype=np.int64), a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.ones(len(rows))))


def signals(strategy, f):
    return [(i, c) for i in range(len(f)) if (c := strategy.check(f, i))]


def walk_columns(days=10, seed=5, start=dt.date(2024, 10, 7), step=0.9):
    """Random prices around the clock, from the Sunday-evening style open through each trading day's New York morning."""
    rng = np.random.default_rng(seed)
    parts, price = [], 4000.0
    day = start
    for _ in range(days):
        t0 = at(day.year, day.month, day.day, 0)
        n = 780     # 00:00 to 13:00 New York
        c = price + np.cumsum(rng.normal(0, step, n))
        o = np.r_[price, c[:-1]]
        h = np.maximum(o, c) + np.abs(rng.normal(0, 0.3, n))
        lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.3, n))
        price = float(c[-1])
        parts.append({"t": t0 + 60 * np.arange(n, dtype=np.int64), "bo": o - 0.3, "bh": h - 0.3, "bl": lo - 0.3, "bc": c - 0.3,
                      "ao": o + 0.3, "ah": h + 0.3, "al": lo + 0.3, "ac": c + 0.3, "v": np.ones(n)})
        day += dt.timedelta(days=1)
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


# ---------------------------------------------------------------- Asian range breakout

LONDON_OPEN = at(2024, 10, 9, 8, 0, LONDON)     # a Wednesday; trading day from 18:00 New York on Tuesday
ASIA_START = LONDON_OPEN - 300 * 40


def asia_frame(after):
    return frame(ASIA_START, [4000.0] * 40 + after)


def test_asia_breakout_buys_the_first_close_above_the_range():
    f = asia_frame([4000.0, 4001.0, 4002.0])                    # the range is 3999.50 to 4000.50
    got = signals(AsiaBreakout(atr_n=3, stop_atr=2.0), f)
    assert [i for i, _ in got] == [41]
    c = got[0][1]
    assert c.side == "buy" and c.target_r == 2.0 and "4,000.50" in c.rule
    assert c.stop == round(4001.0 - 2 * float(f.ind("atr", 3)[41]), 2)
    assert c.conditions and c.invalidation and c.stop_reason


def test_asia_breakout_sells_a_close_below_the_range():
    got = signals(AsiaBreakout(atr_n=3), asia_frame([4000.0, 3999.0]))
    assert got[0][1].side == "sell" and got[0][1].stop > 3999.0


def test_asia_breakout_never_fires_during_asia():
    f = frame(ASIA_START, [4000.0 + i for i in range(40)])
    assert signals(AsiaBreakout(atr_n=3), f) == []


def test_asia_breakout_fires_once_a_day():
    got = signals(AsiaBreakout(atr_n=3), asia_frame([4001.0, 4000.0, 4001.0, 3990.0]))
    assert [i for i, _ in got] == [40]


def test_asia_breakout_stops_at_the_cut_off_hour():
    f = asia_frame([4000.0] * 48 + [4001.0])                    # 48 bars after 08:00 London is 12:00
    assert signals(AsiaBreakout(atr_n=3), f) == []
    f = asia_frame([4000.0] * 47 + [4001.0])                    # 11:55
    assert len(signals(AsiaBreakout(atr_n=3), f)) == 1


def test_asia_cut_off_follows_london_in_summer():
    summer_open = at(2024, 7, 10, 8, 0, LONDON)
    f = frame(summer_open - 300 * 40, [4000.0] * 40 + [4000.0] * 47 + [4001.0])
    assert len(signals(AsiaBreakout(atr_n=3), f)) == 1


# ---------------------------------------------------------------- New York open range

COMEX = at(2024, 10, 9, 8, 20)


def ny_frame(after, before=5):
    return frame(COMEX - 300 * before, [4000.0] * before + [4000.0] * 6 + after)


def test_range_uses_the_six_bars_from_08_20_and_appears_only_after_them():
    f = frame(COMEX, [(4000, 4005, 3995, 4000)] + [4000.0] * 5 + [4000.0])
    up, down = ny_open_range(f)
    assert np.isnan(up[:6]).all() and up[6] == 4005.0 and down[6] == 3995.0


def test_ny_range_with_a_missing_bar_is_not_used():
    t = COMEX + 300 * np.array([0, 1, 2, 3, 4, 6, 7], dtype=np.int64)
    a = np.full(7, 4000.0)
    f = Frame("XAUUSD", "5m", Ohlcv(t, a, a + 1, a - 1, a, a))
    assert np.isnan(ny_open_range(f)[0]).all()


def test_ny_range_follows_new_york_in_summer():
    summer = at(2024, 7, 10, 8, 20)
    f = frame(summer, [4000.0] * 7)
    assert not np.isnan(ny_open_range(f)[0][6])


def test_evening_bars_before_the_range_get_no_levels():
    evening = at(2024, 10, 8, 18, 0)        # the same trading day as the next morning's range
    n = (COMEX - evening) // 300
    f = frame(evening, [4000.0] * (n + 7))
    up = ny_open_range(f)[0]
    assert np.isnan(up[:n + 6]).all() and not np.isnan(up[n + 6])


def test_ny_range_buys_and_sells_the_first_break_once():
    assert [(i, c.side) for i, c in signals(NyOpenRange(atr_n=3), ny_frame([4001.0, 4000.0, 3990.0]))] == [(11, "buy")]
    assert [(i, c.side) for i, c in signals(NyOpenRange(atr_n=3), ny_frame([3999.0]))] == [(11, "sell")]


def test_ny_range_stays_quiet_inside_the_range():
    assert signals(NyOpenRange(atr_n=3), ny_frame([4000.0] * 5)) == []


# ---------------------------------------------------------------- failed break

DAY1 = at(2024, 10, 8, 18, 0)               # the first bar of Wednesday's trading day...
DAY0 = at(2024, 10, 7, 18, 0)               # ...and of Tuesday's, which sets the levels
DAY0_BARS = (at(2024, 10, 8, 17, 0) - DAY0) // 300


def failed_frame(rows):
    before = [(4000.0, 4010.0, 3990.0, 4000.0)] + [4000.0] * (DAY0_BARS - 1)
    t0 = np.r_[DAY0 + 300 * np.arange(DAY0_BARS), DAY1 + 300 * np.arange(len(rows))].astype(np.int64)
    all_rows = [(r, r + 0.5, r - 0.5, r) if isinstance(r, (int, float)) else r for r in before + rows]
    a = np.array(all_rows, dtype=float)
    return Frame("XAUUSD", "5m", Ohlcv(t0, a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.ones(len(all_rows))))


def test_failed_break_of_the_high_sells():
    f = failed_frame([4000.0, (4009, 4014, 4009, 4012), (4012, 4013, 4006, 4008)])
    got = signals(FailedBreak(atr_n=3), f)
    assert len(got) == 1 and got[0][0] == DAY0_BARS + 2
    c = got[0][1]
    assert c.side == "sell" and "4,010.00" in c.rule
    assert c.stop == round(4014.0 + 0.5 * float(f.ind("atr", 3)[DAY0_BARS + 2]), 2)


def test_failed_break_of_the_low_buys():
    f = failed_frame([4000.0, (3991, 3991, 3986, 3988), (3988, 3994, 3987, 3992)])
    got = signals(FailedBreak(atr_n=3), f)
    assert got[0][1].side == "buy" and got[0][1].stop < 3986.0


def test_a_touch_without_a_close_beyond_is_not_a_break():
    assert signals(FailedBreak(atr_n=3), failed_frame([4000.0, (4000, 4015, 4000, 4005), 4005.0])) == []


def test_staying_outside_too_long_is_not_a_failure():
    assert signals(FailedBreak(atr_n=3), failed_frame([4011.0] * 7 + [4005.0])) == []
    assert len(signals(FailedBreak(atr_n=3), failed_frame([4011.0] * 6 + [4005.0]))) == 1


def test_only_once_a_day_on_each_side():
    got = signals(FailedBreak(atr_n=3), failed_frame([4011.0, 4005.0, 4011.0, 4005.0, 3989.0, 3995.0, 3989.0, 3995.0]))
    assert [c.side for _, c in got] == ["sell", "buy"]


def test_no_levels_on_the_first_day_of_data():
    assert signals(FailedBreak(atr_n=3), frame(DAY0, [4000.0] * 5 + [4020.0, 4000.0])) == []


# ---------------------------------------------------------------- all three

@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_no_strategy_reads_the_future(sid):
    f, _ = from_columns("XAUUSD", "5m", walk_columns())
    assert lookahead_violations(make(sid), f, every=15) == []


@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_every_strategy_trades_through_the_backtester_and_explains_itself(sid):
    f, ex = from_columns("XAUUSD", "5m", walk_columns(days=30))
    trades = run(make(sid), f, ex)
    assert trades, "the random prices should produce at least one trade"
    for t in trades:
        assert t.rule and t.side in ("buy", "sell") and t.entry_t > t.signal_t
    assert all(c.conditions and c.invalidation and c.stop_reason for _, c in signals(make(sid), f))


@pytest.mark.parametrize("sid", list(STRATEGIES))
def test_settings_are_what_was_fixed_on_day_one(sid):
    """A change here is a new strategy version and must go through the research rules, not slip in."""
    fixed = {"asia-breakout": {"atr_n": 14, "stop_atr": 2.0, "target_r": 2.0, "latest_london_hour": 12},
             "ny-open-range": {"atr_n": 14, "stop_atr": 2.0, "target_r": 2.0},
             "failed-break": {"max_bars_outside": 6, "stop_buffer_atr": 0.5, "atr_n": 14, "target_r": 2.0}}[sid]
    s = make(sid)
    assert {k: s.params[k] for k in fixed} == fixed and s.version == "1"


def test_unknown_strategy_is_refused():
    with pytest.raises(ValueError, match="unknown strategy"):
        make("nope")


# ---------------------------------------------------------------- the wall around held-back days

def test_the_line_is_the_first_70_percent_of_sessions():
    d, days = RESEARCH_START, []
    while d <= dt.date(2026, 11, 6):
        if is_trading_day(d):
            days.append(d)
        d += dt.timedelta(days=1)
    assert days[int(len(days) * 0.7)] == HELD_BACK_FROM


def day_columns(day, price=4000.0):
    t0 = at(day.year, day.month, day.day, 3)
    n = 400
    o = np.full(n, price)
    return {"t": t0 + 60 * np.arange(n, dtype=np.int64), "bo": o - 0.3, "bh": o - 0.3, "bl": o - 0.3, "bc": o - 0.3,
            "ao": o + 0.3, "ah": o + 0.3, "al": o + 0.3, "ac": o + 0.3, "v": np.ones(n)}


def test_research_data_stops_before_the_line(tmp_path):
    before = dt.date(2025, 2, 5)
    store.save_day("XAUUSD", before, day_columns(before), root=tmp_path)
    store.save_day("XAUUSD", HELD_BACK_FROM, day_columns(HELD_BACK_FROM), root=tmp_path)
    got = research.research_columns("XAUUSD", tmp_path)
    assert got is not None
    assert got["t"].max() < at(2025, 2, 5, 18)      # the first trading minute of the held-back day


def test_held_back_days_alone_give_no_research_data(tmp_path):
    store.save_day("XAUUSD", HELD_BACK_FROM, day_columns(HELD_BACK_FROM), root=tmp_path)
    assert research.research_columns("XAUUSD", tmp_path) is None
    with pytest.raises(research.NoResearchData):
        research.run_one("asia-breakout", "XAUUSD", tmp_path)


def test_a_run_cannot_report_a_held_back_trade(tmp_path):
    start = dt.date(2025, 1, 6)
    cols = walk_columns(days=40, start=start, seed=8)
    # save each day as the store would hold it, on both sides of the line
    for day_key in sorted({dt.datetime.fromtimestamp(int(t), dt.timezone.utc).date() for t in cols["t"]}):
        keep = (cols["t"] >= int(dt.datetime(day_key.year, day_key.month, day_key.day, tzinfo=dt.timezone.utc).timestamp())) & \
               (cols["t"] < int(dt.datetime(day_key.year, day_key.month, day_key.day, tzinfo=dt.timezone.utc).timestamp()) + 86400)
        store.save_day("XAUUSD", day_key, {k: v[keep] for k, v in cols.items()}, root=tmp_path)
    r = research.run_one("ny-open-range", "XAUUSD", tmp_path)
    assert r["last_session"] < HELD_BACK_FROM.isoformat()
    assert r["held_back"] == "hidden: settings are still being chosen" and r["held_back_from"] == HELD_BACK_FROM.isoformat()
    assert all(k <= HELD_BACK_FROM.isoformat()[:7] for k in r["by_month"])
    assert r["summary"]["trades"] > 0


def test_every_run_is_kept_and_the_latest_is_shown(tmp_path, monkeypatch):
    d = dt.date(2025, 1, 8)
    for i in range(3):
        day = d + dt.timedelta(days=i)
        store.save_day("XAUUSD", day, day_columns(day), root=tmp_path)
    clock = iter([1000, 1001, 1002, 2000, 2001, 2002])
    monkeypatch.setattr(research.time, "time", lambda: next(clock))
    research.run_all("XAUUSD", tmp_path)
    research.run_all("XAUUSD", tmp_path)
    assert len(list(research.reports_dir(tmp_path).glob("*.json"))) == 6
    assert research.latest(tmp_path)["asia-breakout"]["ran_at"] == 2000


def test_overview_counts_only_research_days(tmp_path):
    store.save_day("XAUUSD", dt.date(2025, 1, 8), day_columns(dt.date(2025, 1, 8)), root=tmp_path)
    store.save_day("XAUUSD", dt.date(2026, 10, 7), day_columns(dt.date(2026, 10, 7)), root=tmp_path)
    o = research.overview("XAUUSD", tmp_path)
    assert o["research_days_stored"] == 1 and o["research_days_possible"] == 1057
    assert [s["id"] for s in o["strategies"]] == list(STRATEGIES) and o["reports"] == {}


def test_research_days_download_first(tmp_path):
    days = backfill.pending_days("XAUUSD", dt.date(2025, 2, 3), dt.date(2025, 2, 8), tmp_path)
    assert days == [dt.date(2025, 2, 5), dt.date(2025, 2, 4), dt.date(2025, 2, 3), dt.date(2025, 2, 7), dt.date(2025, 2, 6)]


# ---------------------------------------------------------------- the API

def test_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    client = TestClient(app)
    assert [s["id"] for s in client.get("/api/global/strategies").json()] == list(STRATEGIES)
    assert client.get("/api/global/backtests").json()["research_days_stored"] == 0
    r = client.post("/api/global/backtests/run")
    assert r.status_code == 409 and "No research days" in r.json()["detail"]
    store.save_day("XAUUSD", dt.date(2025, 1, 8), day_columns(dt.date(2025, 1, 8)))
    out = client.post("/api/global/backtests/run").json()
    assert out["reports"]["asia-breakout"]["summary"] == {"trades": 0}
    assert "Too few" in out["reports"]["asia-breakout"]["verdict"]["text"]
