import datetime as dt

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core.calendar import IST
from app.indicators import core
from app.indicators.registry import SpecError, compute, parse_specs
from app.main import app
from app.market import nse, yahoo
from app.market.models import Bar, Candles
from app.market.symbols import normalize, to_yahoo


def ts(text: str) -> int:
    return int(dt.datetime.fromisoformat(text).replace(tzinfo=IST).timestamp())


class TestIndicators:
    def test_rsi_matches_wilders_textbook_example(self):
        # StockCharts' RSI example: the first 14-period RSI is 70.53
        closes = [44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433,
                  46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820]
        r = core.rsi(closes, 14)
        assert np.isnan(r[13])
        assert r[14] == pytest.approx(70.53, abs=0.01)

    def test_ema_seeded_with_sma(self):
        e = core.ema(np.arange(1, 11), 3)
        assert np.isnan(e[1]) and e[2] == 2.0
        assert e[9] == pytest.approx(9.0)

    def test_sma_and_bollinger_on_a_flat_line(self):
        x = np.full(30, 100.0)
        assert core.sma(x, 20)[19] == 100.0
        up, mid, dn = core.bollinger(x, 20, 2)
        assert up[25] == mid[25] == dn[25] == 100.0

    def test_atr_of_constant_range(self):
        h, lo = np.full(20, 11.0), np.full(20, 10.0)
        c = np.full(20, 10.5)
        assert core.atr(h, lo, c, 14)[19] == pytest.approx(1.0)

    def test_supertrend_follows_the_trend(self):
        up = np.linspace(100, 200, 60)
        line, d = core.supertrend(up + 1, up - 1, up, 10, 3)
        assert d[-1] == 1 and line[-1] < up[-1]
        down = up[::-1]
        line, d = core.supertrend(down + 1, down - 1, down, 10, 3)
        assert d[-1] == -1 and line[-1] > down[-1]

    def test_macd_signal_starts_after_slow_plus_signal(self):
        line, sig, hist = core.macd(np.linspace(1, 100, 60), 12, 26, 9)
        assert np.isnan(line[24]) and not np.isnan(line[25])
        assert np.isnan(sig[32]) and not np.isnan(sig[33])

    def test_vwap_resets_each_session(self):
        h = [10, 12, 20, 22]; lo = [10, 12, 20, 22]; c = [10, 12, 20, 22]; v = [1, 1, 1, 3]
        w = core.vwap(h, lo, c, v, ["d1", "d1", "d2", "d2"])
        assert w[1] == 11 and w[2] == 20 and w[3] == pytest.approx(21.5)

    def test_opening_range_only_after_it_closes(self):
        h = [105, 108, 107, 110, 111]; lo = [100, 101, 99, 104, 106]
        hi_, lo_ = core.opening_range(h, lo, ["d"] * 5, [0, 5, 10, 15, 20], 15)
        assert np.isnan(hi_[2])
        assert hi_[3] == 108 and lo_[3] == 99 and hi_[4] == 108

    def test_previous_day_levels(self):
        ph, pl, pc = core.previous_day_levels([5, 7, 9], [1, 2, 3], [4, 6, 8], ["a", "a", "b"])
        assert np.isnan(ph[0]) and ph[2] == 7 and pl[2] == 1 and pc[2] == 6


class TestSpecs:
    def test_parse(self):
        specs = parse_specs("ema:20, vwap, supertrend:10:3, bb")
        assert [s.id for s in specs] == ["ema:20", "vwap", "supertrend:10:3", "bb:20:2"]

    @pytest.mark.parametrize("bad", ["foo", "ema:x", "ema:1", "macd:12:26", "rsi:1000"])
    def test_rejects(self, bad):
        with pytest.raises(SpecError):
            parse_specs(bad)

    def test_intraday_only_indicators_skip_daily(self):
        bars = [Bar(t=ts(f"2026-10-{d:02d} 09:15"), o=1, h=2, l=1, c=1.5, v=10, complete=True) for d in range(1, 30)]
        out = compute(Candles("X", "X.NS", None, "1d", bars), parse_specs("vwap,or,ema:5"))
        assert [s.id for s in out] == ["ema:5"]


    def test_vwap_skipped_without_volume(self):
        bars = [Bar(t=ts("2026-10-23 09:15") + 300 * i, o=1, h=2, l=1, c=1.5, v=0, complete=True) for i in range(5)]
        notes = []
        out = compute(Candles("NIFTY", "^NSEI", None, "5m", bars), parse_specs("vwap"), notes)
        assert out == [] and "volume" in notes[0]


class TestSymbols:
    def test_mapping(self):
        assert to_yahoo("reliance") == "RELIANCE.NS"
        assert to_yahoo("M&M") == "M&M.NS"
        assert to_yahoo("NIFTY") == "^NSEI" and to_yahoo("banknifty") == "^NSEBANK"
        assert normalize("Nifty 50") == "NIFTY" and normalize("TCS.NS") == "TCS"


def chart_doc(stamps, closes):
    return {"chart": {"result": [{
        "meta": {"symbol": "TCS.NS", "longName": "Tata Consultancy Services Limited",
                 "chartPreviousClose": 2500.0, "regularMarketPrice": closes[-1], "regularMarketTime": stamps[-1]},
        "timestamp": stamps,
        "indicators": {"quote": [{"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
                                  "close": closes, "volume": [100] * len(closes)}]}}]}}


class TestYahooParsing:
    def test_drops_live_tick_and_marks_forming_bar(self):
        stamps = [ts("2026-10-23 09:15"), ts("2026-10-23 09:20"), ts("2026-10-23 09:25"),
                  ts("2026-10-23 09:27") + 13]                    # the live tick, off the 5-minute grid
        c = yahoo.parse_chart(chart_doc(stamps, [2501, 2502, 2503, 2504]), "TCS", "5m",
                              dt.datetime(2026, 10, 23, 9, 28, tzinfo=IST))
        assert [b.t for b in c.bars] == stamps[:3]
        assert [b.complete for b in c.bars] == [True, True, False]   # 9:25 bar closes at 9:30
        assert c.symbol == "TCS" and c.prev_close == 2500.0   # one session only: Yahoo's value

    def test_previous_close_is_yesterday_not_the_range_start(self):
        stamps = [ts("2026-10-22 15:20"), ts("2026-10-22 15:25"), ts("2026-10-23 09:15")]
        c = yahoo.parse_chart(chart_doc(stamps, [101, 102, 103]), "TCS", "5m", dt.datetime(2026, 10, 23, 10, 0, tzinfo=IST))
        assert c.prev_close == 102                      # not the meta's 2500

    def test_drops_the_closing_print(self):
        stamps = [ts("2026-10-23 15:25"), ts("2026-10-23 15:30")]
        c = yahoo.parse_chart(chart_doc(stamps, [1, 2]), "TCS", "5m", dt.datetime(2026, 10, 23, 16, 0, tzinfo=IST))
        assert [b.t for b in c.bars] == stamps[:1]

    def test_hourly_bars_align_to_915(self):
        stamps = [ts("2026-10-23 09:15"), ts("2026-10-23 10:15"), ts("2026-10-23 10:00")]
        c = yahoo.parse_chart(chart_doc(stamps, [1, 2, 3]), "TCS", "1h", dt.datetime(2026, 10, 23, 16, 0, tzinfo=IST))
        assert len(c.bars) == 2


class TestNse:
    def test_equity_list_keeps_eq_series(self):
        text = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING\n"
                "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995\nXYZ,Trade Ltd,BE,01-JAN-2020\n")
        assert [r["symbol"] for r in nse.parse_equity_list(text)] == ["RELIANCE"]

    def test_holidays_weekdays_only(self):
        doc = {"CM": [{"tradingDate": "26-Jan-2027", "description": "Republic Day"},
                      {"tradingDate": "14-Feb-2027", "description": "Weekend one*"}]}
        assert nse.parse_holidays(doc) == {"2027-01-26": "Republic Day"}


class TestChartApi:
    def test_chart_endpoint(self, monkeypatch):
        bars = [Bar(t=ts("2026-10-23 09:15") + 300 * i, o=100 + i, h=101 + i, l=99 + i, c=100.5 + i, v=1000,
                    complete=True) for i in range(40)]
        monkeypatch.setattr(yahoo, "candles", lambda s, tf, rng: Candles("TCS", "TCS.NS", "TCS", tf, bars))
        r = TestClient(app).get("/api/market/chart", params={"symbol": "TCS", "tf": "5m",
                                                              "ind": "ema:20,vwap,rsi:14,or:15"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["bars"]) == 40
        ids = [s["id"] for s in body["indicators"]]
        assert ids == ["ema:20", "vwap", "rsi:14", "or:15.high", "or:15.low"]
        assert body["indicators"][0]["values"][18] is None and body["indicators"][0]["values"][19] is not None

    def test_bad_indicator_is_a_422(self):
        r = TestClient(app).get("/api/market/chart", params={"symbol": "TCS", "ind": "magic:3"})
        assert r.status_code == 422
