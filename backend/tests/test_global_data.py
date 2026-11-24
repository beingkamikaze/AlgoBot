"""The global setup's data: Dukascopy files, the store, resampling, the backfill, live feeds and the chart API (plan phase X2).
Everything here uses made-up answers: no test reaches the network."""

import datetime as dt
import lzma
import struct
import subprocess
import sys
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.global_markets import indicators
from app.global_markets.clock import NEW_YORK
from app.global_markets.data import backfill, bars as bars_mod, check, dukascopy, feed, store, twelvedata, yahoo_futures
from app.global_markets.data.bars import Ohlcv, is_complete, resample, to_bars
from app.main import app
from app.market.models import Bar

DAY = dt.date(2026, 10, 1)      # a Thursday
MIDNIGHT = int(dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp())


def utc(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc).timestamp())


def bi5(rows):
    """rows of (second, open, close, low, high, volume), prices as the file's integers."""
    return lzma.compress(b"".join(struct.pack(">5if", *r) for r in rows), format=lzma.FORMAT_ALONE)


def columns(day=DAY, minutes=3, spread=0.5, price=4000.0):
    t0 = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    t = np.array([t0 + 60 * i for i in range(minutes)], dtype=np.int64)
    base = price + np.arange(minutes, dtype=float)
    bid = {"bo": base, "bh": base + 2, "bl": base - 2, "bc": base + 1}
    ask = {"a" + k[1]: v + spread for k, v in bid.items()}
    return {"t": t, **bid, **ask, "v": np.ones(minutes)}


@pytest.fixture(autouse=True)
def clean_feed_cache():
    feed._cache.clear()
    yield
    feed._cache.clear()


# ---------------------------------------------------------------- Dukascopy files

def test_url_uses_a_zero_based_month():
    assert dukascopy.day_url("XAUUSD", dt.date(2026, 1, 5), "BID").endswith("/XAUUSD/2026/00/05/BID_candles_min_1.bi5")
    assert dukascopy.day_url("XAUUSD", dt.date(2026, 12, 31), "ASK").endswith("/XAUUSD/2026/11/31/ASK_candles_min_1.bi5")


def test_parse_reads_the_field_order_open_close_low_high():
    out = dukascopy.parse(bi5([(36000, 4154195, 4154275, 4153075, 4155505, 0.5)]), DAY, 1000.0)
    assert out == {MIDNIGHT + 36000: (4154.195, 4155.505, 4153.075, 4154.275, 0.5)}


def test_parse_skips_flat_minutes_with_no_volume():
    assert dukascopy.parse(bi5([(0, 1000, 1000, 1000, 1000, 0.0)]), DAY, 1000.0) == {}


def test_empty_file_is_no_data():
    assert dukascopy.parse(b"", DAY, 1000.0) == {}


def test_garbage_file_is_refused():
    with pytest.raises(dukascopy.BadFile, match="decompress"):
        dukascopy.parse(b"this is not compressed data at all", DAY, 1000.0)


def test_oversized_file_is_refused():
    raw = lzma.compress(b"\0" * (dukascopy.MAX_BYTES + 24), format=lzma.FORMAT_ALONE)
    with pytest.raises(dukascopy.BadFile, match="size"):
        dukascopy.parse(raw, DAY, 1000.0)


def test_impossible_candle_is_refused():
    with pytest.raises(dukascopy.BadFile, match="impossible"):
        dukascopy.parse(bi5([(0, 1000, 1000, 1500, 1200, 1.0)]), DAY, 1000.0)      # low above high


def test_sides_join_only_on_shared_minutes():
    bid = {1: (10.0, 11.0, 9.0, 10.5, 1.0), 2: (10.0, 11.0, 9.0, 10.5, 1.0)}
    ask = {2: (10.5, 11.5, 9.5, 11.0, 2.0), 3: (10.5, 11.5, 9.5, 11.0, 2.0)}
    out = dukascopy.join_sides(bid, ask)
    assert list(out["t"]) == [2] and out["bo"][0] == 10.0 and out["ao"][0] == 10.5 and out["v"][0] == 3.0
    assert dukascopy.join_sides(bid, {}) is None


# ---------------------------------------------------------------- the store

def test_store_round_trip_and_mid(tmp_path):
    store.save_day("XAUUSD", DAY, columns(), root=tmp_path)
    got = store.read_columns("XAUUSD", root=tmp_path)
    assert list(got["t"]) == [MIDNIGHT, MIDNIGHT + 60, MIDNIGHT + 120]
    m = store.mid(got)
    assert m.o[0] == 4000.25 and m.c[0] == 4001.25
    assert store.manifest("XAUUSD", tmp_path) == {"2026-10-01": "ok"}


def test_saving_a_day_again_replaces_it(tmp_path):
    store.save_day("XAUUSD", DAY, columns(minutes=3), root=tmp_path)
    store.save_day("XAUUSD", DAY, columns(minutes=2, price=5000.0), root=tmp_path)
    got = store.read_columns("XAUUSD", root=tmp_path)
    assert len(got["t"]) == 2 and got["bo"][0] == 5000.0


def test_days_may_arrive_newest_first_and_read_sorted(tmp_path):
    store.save_day("XAUUSD", dt.date(2026, 10, 2), columns(dt.date(2026, 10, 2)), root=tmp_path)
    store.save_day("XAUUSD", DAY, columns(), root=tmp_path)
    store.save_day("XAUUSD", dt.date(2026, 9, 30), columns(dt.date(2026, 9, 30)), root=tmp_path)
    t = store.read_columns("XAUUSD", root=tmp_path)["t"]
    assert len(t) == 9 and (np.diff(t) > 0).all()


def test_read_since_cuts_older_bars(tmp_path):
    store.save_day("XAUUSD", dt.date(2026, 9, 30), columns(dt.date(2026, 9, 30)), root=tmp_path)
    store.save_day("XAUUSD", DAY, columns(), root=tmp_path)
    assert store.read_columns("XAUUSD", MIDNIGHT + 60, root=tmp_path)["t"][0] == MIDNIGHT + 60


def test_nothing_stored_reads_as_none(tmp_path):
    assert store.read_columns("XAUUSD", root=tmp_path) is None
    assert store.manifest("XAUUSD", tmp_path) == {}


def test_closed_day_is_recorded_as_empty(tmp_path):
    store.save_day("XAUUSD", DAY, None, root=tmp_path)
    assert store.manifest("XAUUSD", tmp_path) == {"2026-10-01": "empty"}


# ---------------------------------------------------------------- resampling

def ohlcv(t, o=None, h=None, lo=None, c=None, v=None):
    n = len(t)
    arr = lambda x, d: np.array(x if x is not None else [d] * n, dtype=float)  # noqa: E731
    return Ohlcv(np.array(t, dtype=np.int64), arr(o, 10), arr(h, 11), arr(lo, 9), arr(c, 10), arr(v, 1))


def test_five_minute_bar_from_one_minute_bars():
    t0 = utc(2026, 10, 1, 10, 0)
    out = resample(ohlcv([t0 + 60 * i for i in range(7)], o=[1, 2, 3, 4, 5, 6, 7], h=[10, 12, 11, 13, 9, 20, 21],
                         lo=[5, 4, 6, 3, 7, 15, 14], c=[2, 3, 4, 5, 6, 7, 8], v=[1, 1, 1, 1, 1, 2, 3]), "5m")
    assert list(out.t) == [t0, t0 + 300]
    assert (out.o[0], out.h[0], out.l[0], out.c[0], out.v[0]) == (1, 13, 3, 6, 5)
    assert (out.o[1], out.h[1], out.l[1], out.c[1], out.v[1]) == (6, 21, 14, 8, 5)


def test_gaps_do_not_create_empty_bars():
    t0 = utc(2026, 10, 1, 10, 0)
    assert list(resample(ohlcv([t0, t0 + 3600]), "5m").t) == [t0, t0 + 3600]


def test_daily_bar_rolls_at_17_00_new_york():
    before = int(dt.datetime(2026, 11, 11, 16, 59, tzinfo=NEW_YORK).timestamp())
    after = int(dt.datetime(2026, 11, 11, 18, 0, tzinfo=NEW_YORK).timestamp())
    out = resample(ohlcv([before, after]), "1d")
    assert list(out.t) == [utc(2026, 11, 11), utc(2026, 11, 12)]


def test_sunday_evening_counts_as_monday():
    sunday = int(dt.datetime(2026, 11, 8, 18, 0, tzinfo=NEW_YORK).timestamp())
    monday = int(dt.datetime(2026, 11, 9, 9, 0, tzinfo=NEW_YORK).timestamp())
    assert list(resample(ohlcv([sunday, monday]), "1d").t) == [utc(2026, 11, 9)]


def test_bad_timeframe_is_refused():
    with pytest.raises(ValueError, match="timeframe"):
        resample(ohlcv([1]), "7m")


def test_forming_bars_are_flagged():
    t0 = utc(2026, 10, 1, 10, 0)
    assert is_complete(t0, "5m", t0 + 300) and not is_complete(t0, "5m", t0 + 299)
    close = dt.datetime(2026, 11, 11, 17, 0, tzinfo=NEW_YORK).timestamp()
    assert is_complete(utc(2026, 11, 11), "1d", close) and not is_complete(utc(2026, 11, 11), "1d", close - 1)


def test_to_bars_rounds_to_cents():
    t0 = utc(2026, 10, 1, 10, 0)
    b = to_bars(ohlcv([t0], o=[4000.1234]), "1m", t0 + 60)[0]
    assert b.o == 4000.12 and b.complete


# ---------------------------------------------------------------- the backfill

def test_pending_days_are_newest_first_without_saturdays_or_today(tmp_path):
    today = dt.date(2026, 11, 9)    # a Monday
    days = backfill.pending_days("XAUUSD", dt.date(2026, 11, 5), today, tmp_path)
    assert days == [dt.date(2026, 11, 8), dt.date(2026, 11, 6), dt.date(2026, 11, 5)]


def test_pending_days_skip_what_is_done(tmp_path):
    store.save_day("XAUUSD", dt.date(2026, 11, 6), columns(dt.date(2026, 11, 6)), root=tmp_path)
    days = backfill.pending_days("XAUUSD", dt.date(2026, 11, 5), dt.date(2026, 11, 9), tmp_path)
    assert dt.date(2026, 11, 6) not in days


def fast(monkeypatch):
    monkeypatch.setattr(backfill, "PAUSE", 0)
    monkeypatch.setattr(backfill, "FIRST_BACKOFF", 0)
    backfill.state.update(last_error=None, backoff_seconds=0, current_day=None)


def test_run_saves_days_and_a_second_run_resumes(tmp_path, monkeypatch):
    fast(monkeypatch)
    asked = []
    def fetch(symbol, day):
        asked.append(day)
        return columns(day)
    today = dt.date(2026, 11, 7)
    assert backfill.run("XAUUSD", dt.date(2026, 11, 5), threading.Event(), fetch, today, max_days=1, root=tmp_path) == 1
    assert asked == [dt.date(2026, 11, 6)]
    assert backfill.run("XAUUSD", dt.date(2026, 11, 5), threading.Event(), fetch, today, root=tmp_path) == 1
    assert asked == [dt.date(2026, 11, 6), dt.date(2026, 11, 5)]
    assert backfill.run("XAUUSD", dt.date(2026, 11, 5), threading.Event(), fetch, today, root=tmp_path) == 0


def test_busy_server_is_retried_and_loses_nothing(tmp_path, monkeypatch):
    fast(monkeypatch)
    answers = [dukascopy.Busy("HTTP 503"), dukascopy.Busy("HTTP 503"), columns(dt.date(2026, 11, 6))]
    def fetch(symbol, day):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    n = backfill.run("XAUUSD", dt.date(2026, 11, 6), threading.Event(), fetch, dt.date(2026, 11, 7), root=tmp_path)
    assert n == 1 and store.manifest("XAUUSD", tmp_path) == {"2026-11-06": "ok"}
    assert backfill.state["last_error"] is None


def test_stop_during_a_busy_wait_marks_nothing_done(tmp_path, monkeypatch):
    fast(monkeypatch)
    stop = threading.Event()
    def fetch(symbol, day):
        stop.set()
        raise dukascopy.Busy("HTTP 503")
    assert backfill.run("XAUUSD", dt.date(2026, 11, 6), stop, fetch, dt.date(2026, 11, 7), root=tmp_path) == 0
    assert store.manifest("XAUUSD", tmp_path) == {}


def test_bad_file_is_recorded_and_the_run_goes_on(tmp_path, monkeypatch):
    fast(monkeypatch)
    def fetch(symbol, day):
        if day == dt.date(2026, 11, 6):
            raise dukascopy.BadFile("unexpected size")
        return columns(day)
    backfill.run("XAUUSD", dt.date(2026, 11, 5), threading.Event(), fetch, dt.date(2026, 11, 7), root=tmp_path)
    assert store.manifest("XAUUSD", tmp_path) == {"2026-11-05": "ok", "2026-11-06": "bad"}


def test_coverage_counts_gaps_only_on_trading_days(tmp_path):
    store.save_day("XAUUSD", dt.date(2026, 11, 5), columns(dt.date(2026, 11, 5)), root=tmp_path)
    store.save_day("XAUUSD", dt.date(2026, 11, 4), None, root=tmp_path)         # a Wednesday with nothing: a gap
    store.save_day("XAUUSD", dt.date(2026, 11, 8), None, root=tmp_path)         # a Sunday with nothing: fine
    c = backfill.coverage("XAUUSD", dt.date(2026, 11, 4), dt.date(2026, 11, 9), tmp_path)
    assert c["days_with_bars"] == 1 and c["first_day"] == c["last_day"] == "2026-11-05"
    assert c["gap_days"] == 1 and c["gap_examples"] == ["2026-11-04"] and c["days_left"] == 1


def test_only_one_process_downloads(tmp_path):
    held = backfill._acquire(tmp_path)
    assert held is not None
    code = ("import sys; from pathlib import Path; from app.global_markets.data.backfill import _acquire; "
            f"print(_acquire(Path({str(tmp_path)!r})) is None)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    held.close()
    assert out == "True"


# ---------------------------------------------------------------- live feeds

def test_twelvedata_parse():
    doc = {"status": "ok", "values": [{"datetime": "2026-11-06 10:05:00", "open": "4001", "high": "4002", "low": "4000", "close": "4001.5"},
                                      {"datetime": "2026-11-06 10:00:00", "open": "4000", "high": "4001", "low": "3999", "close": "4000.5"}]}
    t = utc(2026, 11, 6, 10, 0)
    out = twelvedata.parse(doc, "5m", t + 301)
    assert [b.t for b in out] == [t, t + 300] and out[0].complete and not out[1].complete
    assert out[0].c == 4000.5 and out[0].v == 0


def test_twelvedata_daily_date_format():
    doc = {"status": "ok", "values": [{"datetime": "2026-11-06", "open": "1", "high": "2", "low": "0.5", "close": "1.5"}]}
    assert twelvedata.parse(doc, "1d", utc(2026, 11, 9))[0].t == utc(2026, 11, 6)


def test_twelvedata_error_answer():
    with pytest.raises(twelvedata.TwelveDataError, match="apikey is invalid"):
        twelvedata.parse({"status": "error", "code": 401, "message": "apikey is invalid"}, "5m", 0)


def test_twelvedata_stops_at_the_daily_allowance(monkeypatch):
    monkeypatch.setattr(twelvedata, "_calls", {"day": dt.datetime.now(dt.timezone.utc).date(), "count": twelvedata.DAILY_CALLS})
    def never(*a, **k):
        raise AssertionError("must not call the service")
    monkeypatch.setattr(twelvedata.httpx, "get", never)
    with pytest.raises(twelvedata.TwelveDataError, match="used up"):
        twelvedata.fetch("XAUUSD", "5m", "a-key")


def test_twelvedata_key_goes_in_a_header_never_the_url(monkeypatch):
    seen = {}
    class Answer:
        def json(self):
            return {"status": "ok", "values": []}
    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(url=url, params=params, headers=headers)
        return Answer()
    monkeypatch.setattr(twelvedata.httpx, "get", fake_get)
    monkeypatch.setattr(twelvedata, "_calls", {"day": None, "count": 0})
    twelvedata.fetch("XAUUSD", "5m", "secret-key")
    assert seen["headers"]["Authorization"] == "apikey secret-key"
    assert "secret-key" not in seen["url"] and "secret-key" not in str(seen["params"])
    assert seen["params"]["symbol"] == "XAU/USD" and seen["params"]["timezone"] == "UTC"


def yahoo_doc(ts, closes):
    n = len(ts)
    return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"open": closes, "high": closes, "low": closes, "close": closes, "volume": [5] * n}]}}]}}


def test_futures_parse_drops_empty_bars_and_live_ticks():
    t = utc(2026, 11, 6, 10, 0)
    out = yahoo_futures.parse(yahoo_doc([t, t + 300, t + 417], [4000.0, None, 4001.0]), "5m", t + 1000)
    assert [b.t for b in out] == [t] and out[0].v == 5


def test_futures_daily_stamp_is_the_new_york_date():
    ts = int(dt.datetime(2026, 11, 6, 0, 0, tzinfo=NEW_YORK).timestamp())
    assert yahoo_futures.parse(yahoo_doc([ts], [4000.0]), "1d", ts + 10)[0].t == utc(2026, 11, 6)


def test_futures_bad_answer():
    with pytest.raises(yahoo_futures.FuturesError):
        yahoo_futures.parse({"chart": {"result": None}}, "5m", 0)


# ---------------------------------------------------------------- choosing and joining sources

def bar(t, c=4000.0, v=1, complete=True):
    return Bar(t=t, o=c, h=c + 1, l=c - 1, c=c, v=v, complete=complete)


def settings_with_key(monkeypatch, key):
    get_settings.cache_clear()
    monkeypatch.setenv("TWELVEDATA_API_KEY", key)
    yield_settings = get_settings()
    monkeypatch.setattr(feed, "get_settings", lambda: yield_settings)


def test_default_source_follows_the_key(monkeypatch):
    settings_with_key(monkeypatch, "")
    assert feed.default_source() == "futures"
    settings_with_key(monkeypatch, "k")
    assert feed.default_source() == "spot"
    get_settings.cache_clear()


def test_spot_joins_history_and_live_keeping_stored_volume(monkeypatch):
    settings_with_key(monkeypatch, "k")
    t = utc(2026, 11, 6, 10, 0)
    monkeypatch.setattr(feed, "_stored", lambda *a: [bar(t, 4000.0, v=7), bar(t + 300, 4001.0, v=7)])
    monkeypatch.setattr(feed, "_live", lambda *a: ([bar(t + 300, 4009.0, v=0), bar(t + 600, 4002.0, v=0, complete=False)], None))
    s = feed.spot("XAUUSD", "5m", t + 700)
    assert [b.t for b in s.bars] == [t, t + 300, t + 600]
    assert s.bars[1].c == 4001.0 and s.bars[1].v == 7
    assert s.provider == "Dukascopy history + Twelve Data" and s.source == "spot"
    get_settings.cache_clear()


def test_spot_without_a_key_says_where_history_ends(monkeypatch):
    settings_with_key(monkeypatch, "")
    wed_noon = utc(2026, 11, 11, 17, 0)
    monkeypatch.setattr(feed, "_stored", lambda *a: [bar(wed_noon - 86400)])
    s = feed.spot("XAUUSD", "5m", wed_noon)
    assert any("TWELVEDATA_API_KEY" in n and "ends at" in n for n in s.notes)
    get_settings.cache_clear()


def test_spot_with_nothing_explains_itself(monkeypatch):
    settings_with_key(monkeypatch, "")
    monkeypatch.setattr(feed, "_stored", lambda *a: [])
    s = feed.spot("XAUUSD", "5m", utc(2026, 11, 11, 17, 0))
    assert s.bars == [] and s.last_price is None and "No spot data" in s.notes[0]
    get_settings.cache_clear()


def test_live_failure_is_shown_and_keeps_history(monkeypatch):
    settings_with_key(monkeypatch, "k")
    t = utc(2026, 11, 6, 10, 0)
    monkeypatch.setattr(feed, "_stored", lambda *a: [bar(t)])
    monkeypatch.setattr(feed, "_live", lambda *a: ([], "Twelve Data: apikey is invalid"))
    s = feed.spot("XAUUSD", "5m", t + 60)
    assert len(s.bars) == 1 and "Twelve Data: apikey is invalid" in s.notes
    get_settings.cache_clear()


def test_a_failed_live_call_is_not_repeated_on_every_refresh(monkeypatch):
    calls = []
    def fail(*a):
        calls.append(1)
        raise twelvedata.TwelveDataError("no answer")
    monkeypatch.setattr(twelvedata, "fetch", fail)
    assert feed._live("XAUUSD", "5m", "k", True)[1] == "Twelve Data: no answer"
    assert feed._live("XAUUSD", "5m", "k", True)[1] == "Twelve Data: no answer"
    assert len(calls) == 1


def test_futures_are_labelled_and_never_joined_to_spot(monkeypatch):
    monkeypatch.setattr(yahoo_futures, "fetch", lambda s, tf: [bar(1000, 4030.0)])
    monkeypatch.setattr(feed, "_stored", lambda *a: pytest.fail("futures must not read spot history"))
    s = feed.futures("XAUUSD", "5m")
    assert s.source == "futures" and "not spot" in s.notes[0] and s.bars[0].c == 4030.0


def test_prev_close_is_the_last_close_of_the_previous_trading_day():
    before_roll = int(dt.datetime(2026, 11, 11, 16, 55, tzinfo=NEW_YORK).timestamp())
    after_roll = int(dt.datetime(2026, 11, 11, 18, 0, tzinfo=NEW_YORK).timestamp())
    s = feed.Series("XAUUSD", "5m", "spot", "x", bars=[bar(before_roll - 300, 1.0), bar(before_roll, 2.0), bar(after_roll, 3.0), bar(after_roll + 300, 4.0)])
    assert s.prev_close == 2.0 and s.last_price == 4.0


# ---------------------------------------------------------------- the check against a second source

def walk(n, seed=1, offset=0.0, seconds=300):
    rng = np.random.default_rng(seed)
    t0 = utc(2026, 9, 1)
    closes = 4000 + np.cumsum(rng.normal(0, 3, n)) + offset
    return [bar(t0 + i * seconds, float(c)) for i, c in enumerate(closes)]


def test_check_passes_when_sources_move_together_at_a_steady_gap():
    out = check.compare(walk(1000), walk(1000, offset=30.0))
    assert out["ok"] is True and out["median_gap"] == 30.0 and out["move_correlation"] == 1.0


def test_check_fails_when_sources_do_not_move_together():
    out = check.compare(walk(1000, seed=1), walk(1000, seed=2))
    assert out["ok"] is False and out["move_correlation"] < 0.5


def test_check_fails_when_the_gap_wanders():
    a = walk(1000)
    b = [bar(x.t, x.c + i * 0.2) for i, x in enumerate(a)]
    out = check.compare(a, b)
    assert out["ok"] is False and out["gap_swing"] > check.MAX_GAP_SWING


def test_check_will_not_judge_too_little():
    out = check.compare(walk(100), walk(100))
    assert out["ok"] is None and "too few" in out["detail"]


# ---------------------------------------------------------------- indicators

def test_indicator_specs():
    assert indicators.parse_specs("ema:20, asia,pdl") == [("ema", [20.0]), ("asia", []), ("pdl", [])]
    with pytest.raises(indicators.SpecError, match="unknown"):
        indicators.parse_specs("or:15")          # NSE's opening range does not exist here
    with pytest.raises(indicators.SpecError, match="between"):
        indicators.parse_specs("ema:1")


def day_bars():
    """One trading day, 5-minute bars, from the 18:00 open to 12:00 New York next day, rising steadily."""
    start = int(dt.datetime(2026, 11, 10, 18, 0, tzinfo=NEW_YORK).timestamp())
    return [bar(start + i * 300, 4000.0 + i, v=1) for i in range(18 * 12)]


def test_asia_range_only_appears_after_asia_ends_and_never_changes():
    b = day_bars()
    s = {x["id"]: x for x in indicators.compute(b, "5m", [("asia", [])], [])}
    high = s["asia.high"]["values"]
    london = int(dt.datetime(2026, 11, 11, 8, 0, tzinfo=dt.timezone.utc).timestamp())
    first_after = next(i for i, x in enumerate(b) if x.t >= london)
    assert all(v is None for v in high[:first_after])
    assert set(high[first_after:]) == {b[first_after - 1].h}
    assert set(s["asia.low"]["values"][first_after:]) == {b[0].l}


def test_asia_range_does_not_use_later_bars():
    b = day_bars()
    full = indicators.compute(b, "5m", [("asia", [])], [])[0]["values"]
    cut = indicators.compute(b[:150], "5m", [("asia", [])], [])[0]["values"]
    assert cut == full[:150]


def test_previous_day_levels_use_the_trading_day():
    first = day_bars()
    next_open = int(dt.datetime(2026, 11, 11, 18, 0, tzinfo=NEW_YORK).timestamp())
    b = first + [bar(next_open, 5000.0)]
    s = {x["id"]: x["values"] for x in indicators.compute(b, "5m", [("pdl", [])], [])}
    assert s["pdl.high"][-1] == first[-1].h and s["pdl.close"][-1] == first[-1].c
    assert s["pdl.high"][0] is None


def test_daily_bars_skip_intraday_indicators_and_say_so():
    notes = []
    assert indicators.compute([bar(utc(2026, 11, 9)), bar(utc(2026, 11, 10))], "1d", [("vwap", []), ("asia", [])], notes) == []
    assert len(notes) == 2


def test_vwap_without_volume_explains_itself():
    notes = []
    assert indicators.compute([bar(utc(2026, 11, 9, 10), v=0), bar(utc(2026, 11, 9, 11), v=0)], "1h", [("vwap", [])], notes) == []
    assert "volume" in notes[0]


def test_vwap_with_a_live_day_without_volume_stays_finite():
    t = utc(2026, 11, 9, 10)
    notes = []
    out = indicators.compute([bar(t, v=5), bar(t + 86400, v=0), bar(t + 86700, v=0)], "5m", [("vwap", [])], notes)
    assert out[0]["values"][0] is not None and "no volume" in notes[0]
    assert all(v is None or np.isfinite(v) for v in out[0]["values"])


def test_no_bars_gives_no_series():
    assert indicators.compute([], "5m", [("ema", [20.0]), ("asia", [])], []) == []


# ---------------------------------------------------------------- the API

client = TestClient(app)


def test_status_route():
    m = client.get("/api/global/status").json()
    assert m["phase"] in ("open", "daily-break", "early-close", "weekend", "holiday")
    assert "next_open" in m and "trading_day" in m


def test_chart_route_has_the_shape_the_chart_expects(monkeypatch):
    t = utc(2026, 11, 6, 10, 0)
    monkeypatch.setattr(yahoo_futures, "fetch", lambda s, tf: [bar(t + i * 300, 4000.0 + i) for i in range(30)])
    d = client.get("/api/global/chart", params={"symbol": "xau/usd", "tf": "5m", "source": "futures", "ind": "ema:5"}).json()
    assert d["symbol"] == "XAUUSD" and d["price_source"] == "futures" and d["source"] == "Yahoo GC=F"
    assert len(d["bars"]) == 30 and d["last_price"] == 4029.0
    assert d["indicators"][0]["id"] == "ema:5" and len(d["indicators"][0]["values"]) == 30


@pytest.mark.parametrize("params,code", [
    ({"symbol": "RELIANCE"}, 404), ({"tf": "7m"}, 422), ({"source": "cfd"}, 422), ({"ind": "or:15"}, 422),
])
def test_chart_route_refuses_bad_input(params, code):
    assert client.get("/api/global/chart", params=params).status_code == code


def test_chart_route_reports_a_failed_source(monkeypatch):
    def fail(s, tf):
        raise yahoo_futures.FuturesError("Yahoo did not answer (HTTP 500)")
    monkeypatch.setattr(yahoo_futures, "fetch", fail)
    r = client.get("/api/global/chart", params={"source": "futures", "tf": "15m"})
    assert r.status_code == 502 and "Yahoo" in r.json()["detail"]


def test_data_status_never_shows_the_key(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("TWELVEDATA_API_KEY", "secret-key-value")
    r = client.get("/api/global/data/status")
    assert r.status_code == 200 and "secret-key-value" not in r.text
    assert r.json()["live"]["twelvedata_configured"] is True
    get_settings.cache_clear()


# ---------------------------------------------------------------- kept apart from NSE

def test_the_data_layer_does_not_load_nse_market_code():
    code = ("import sys; import app.global_markets.api; "
            "bad = ['app.core.calendar', 'app.market.yahoo', 'app.market.symbols', 'app.market.nse', 'app.market.store', "
            "'app.indicators.registry', 'app.strategies.base', 'app.backtest.engine', 'app.risk.engine', 'app.paper.service']; "
            "print([m for m in bad if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


def test_bars_module_is_importable_alone():
    assert bars_mod.TF_SECONDS["1h"] == 3600


def test_download_thread_waits_for_the_lock_and_can_be_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    held = backfill._acquire(tmp_path)
    worker = backfill.BackfillThread("XAUUSD", dt.date(2026, 11, 6))
    worker.start_thread()
    for _ in range(100):
        if "already downloading" in (backfill.state["last_error"] or ""):
            break
        threading.Event().wait(0.01)
    assert "checking again" in backfill.state["last_error"] and worker._thread.is_alive()
    worker.stop()
    worker._thread.join(2)
    assert not worker._thread.is_alive() and backfill.state["running"] is False
    held.close()
    backfill.state["last_error"] = None
