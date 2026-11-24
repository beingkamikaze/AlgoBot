import datetime as dt

from app.core.calendar import IST
from app.market import backfill, store


class FakeKite:
    def __init__(self):
        self.calls = []

    def instruments(self, exchange):
        return [{"tradingsymbol": "RELIANCE", "instrument_token": 11, "segment": "NSE", "instrument_type": "EQ"},
                {"tradingsymbol": "NIFTY 50", "instrument_token": 22, "segment": "INDICES", "instrument_type": "EQ"},
                {"tradingsymbol": "RELIANCE-BE", "instrument_token": 33, "segment": "NSE", "instrument_type": "BE"}]

    def historical_data(self, token, a, b, interval):
        self.calls.append((token, a, b, interval))
        day = dt.datetime.combine(a, dt.time(9, 15), IST)
        return [{"date": day + dt.timedelta(minutes=5 * k), "open": 100, "high": 101, "low": 99, "close": 100.5,
                 "volume": 10} for k in range(3)]


def test_tokens_map_indices_and_equities_only():
    assert backfill.tokens(FakeKite()) == {"RELIANCE": 11, "NIFTY": 22}


def test_chunks_cover_the_range_within_kites_limit():
    c = backfill.chunks(dt.date(2026, 1, 1), dt.date(2026, 12, 31), 100)
    assert c[0][0] == dt.date(2026, 1, 1) and c[-1][1] == dt.date(2026, 12, 31)
    assert all((b - a).days < 100 for a, b in c)
    assert all(c[k][1] + dt.timedelta(days=1) == c[k + 1][0] for k in range(len(c) - 1))


def test_bar_still_forming_is_dropped():
    now = dt.datetime(2026, 11, 11, 9, 26, tzinfo=IST)
    rows = [{"date": dt.datetime(2026, 11, 11, 9, m, tzinfo=IST), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
            for m in (15, 20, 25)]
    bars = backfill.to_bars(rows, "5m", now)
    assert [dt.datetime.fromtimestamp(b.t, IST).minute for b in bars] == [15, 20]   # 9:25 ends at 9:30


def test_backfill_merges_into_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr(backfill.Throttle, "wait", lambda self: None)
    k = FakeKite()
    r = backfill.backfill(k, ["RELIANCE", "NOPE"], ["5m"], dt.date(2026, 6, 1), log=lambda *_: None)
    assert r["missing"] == ["NOPE"] and r["symbols"] == 1 and not r["errors"]
    assert all(interval == "5minute" for *_, interval in k.calls)
    stored = store.read("RELIANCE", "5m")
    assert len(stored) == 3 * len(k.calls) and stored == sorted(stored, key=lambda b: b.t)
    again = backfill.backfill(k, ["RELIANCE"], ["5m"], dt.date(2026, 6, 1), log=lambda *_: None)
    assert again["bars"] == 0 and len(store.read("RELIANCE", "5m")) == len(stored)      # re-running adds nothing
