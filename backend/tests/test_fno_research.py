"""F&O research: bhavcopy parsing (both formats), the expiry calendar, condor P&L and charges, the Lab path and
the chain recorder's market-hours gating. Everything runs on small fixtures: no network, no real data."""

import datetime as dt
import io
import math
import zipfile

import httpx
import pytest

from app.core.calendar import IST
from app.fno_research import bhavcopy as bc
from app.fno_research import calendar as fcal
from app.fno_research import charges as C
from app.fno_research import condor
from app.fno_research import lab as fno_lab
from app.fno_research import recorder
from app.lab import loop
from app.options import pricing
from tests.fakes import FakeDB

OLD = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,
FUTIDX,BANKNIFTY,31-Jul-2024,0,XX,52998.6,52998.6,52300,52724.1,52724.1,177911,1403441.12,2607810,-185025,05-JUL-2024,
FUTIDX,NIFTY,25-Jul-2024,0,XX,24306.9,24419.2,24240,24379.4,24379.4,230381,1400814.91,14171325,-371200,05-JUL-2024,
OPTIDX,NIFTY,11-Jul-2024,24300,CE,100,120,90,110.5,111.2,5000,100,250000,25,05-JUL-2024,
OPTIDX,NIFTY,11-JUL-2024,24300,PE,80,95,60,70.25,70.3,4000,100,200000,25,05-JUL-2024,
OPTIDX,BANKNIFTY,10-Jul-2024,52000,CE,1,1,1,1,1,1,1,1,1,05-JUL-2024,
OPTSTK,NIFTY,25-Jul-2024,100,CE,1,1,1,1,1,1,1,1,1,05-JUL-2024,
"""

NEW = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
2024-07-08,2024-07-08,FO,NSE,STO,67522,,ABFRL,,2024-07-25,2024-07-25,250.00,PE,ABFRL24JUL250PE,0.00,0.00,0.00,0.15,0.15,0.15,322.25,0.10,26000,0,0,0.00,0,F1,2600,,,,,
2024-07-08,2024-07-08,FO,NSE,IDO,65204,,NIFTY,,2024-07-25,2024-07-25,23350.00,PE,NIFTY24JUL23350PE,34.55,45.60,34.40,39.25,38.45,34.40,24320.55,39.25,91625,12175,2432,1422055136.25,732,F1,25,,,,,
2024-07-08,2024-07-08,FO,NSE,IDO,38337,,NIFTY,,2024-08-08,2024-08-07,23350.00,CE,NIFTY2480823350CE,0.00,0.00,0.00,128.05,0.00,128.05,24320.55,106.70,0,0,0,0.00,0,F1,25,,,,,
2024-07-08,2024-07-08,FO,NSE,IDF,35001,,NIFTY,,2024-07-25,2024-07-25,,,NIFTY24JULFUT,24400.00,24450.00,24300.00,24420.50,24420.50,24390.00,24320.55,24420.50,1000,10,500,1.0,10,F1,25,,,,,
2024-07-08,2024-07-08,FO,NSE,IDO,99999,,BANKNIFTY,,2024-07-10,2024-07-10,52000.00,CE,X,1,1,1,1,1,1,1,1,1,1,1,1,1,F1,15,,,,,
"""


def _zip(name: str, text: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, text)
    return b.getvalue()


# ---------------------------------------------------------------- loader

def test_old_format_keeps_only_nifty_index_options_and_futures():
    rows = bc.parse_old(OLD)
    assert [(r.instrument, r.kind) for r in rows] == [("FUT", "FUT"), ("OPT", "CE"), ("OPT", "PE")]
    fut, ce, pe = rows
    assert fut.expiry == dt.date(2024, 7, 25) and fut.strike == 0 and fut.close == 24379.4
    assert ce.date == dt.date(2024, 7, 5) and ce.expiry == dt.date(2024, 7, 11) and ce.strike == 24300
    assert (ce.open, ce.high, ce.low, ce.close, ce.settle, ce.oi, ce.volume) == (100, 120, 90, 110.5, 111.2, 250000, 5000)
    assert pe.expiry == dt.date(2024, 7, 11)                   # upper-case month parses too
    assert ce.underlying is None


def test_new_format_uses_the_actual_expiry_and_keeps_the_index_close():
    rows = bc.parse_new(NEW)
    assert len(rows) == 3
    pe, ce, fut = rows
    assert pe.kind == "PE" and pe.strike == 23350 and pe.close == 39.25 and pe.settle == 39.25
    assert pe.oi == 91625 and pe.volume == 2432 and pe.underlying == 24320.55
    assert ce.expiry == dt.date(2024, 8, 7)                    # FininstrmActlXpryDt, not the listed 08-08
    assert fut.instrument == "FUT" and fut.kind == "FUT" and fut.strike == 0 and fut.close == 24420.5


def test_zip_parsing_and_day_files_round_trip(tmp_path):
    rows = bc.parse_zip(_zip("fo05JUL2024bhav.csv", OLD), new_format=False)
    rows2 = bc.parse_zip(_zip("BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv", NEW), new_format=True)
    bc.write_day(dt.date(2024, 7, 5), rows, tmp_path)
    bc.write_day(dt.date(2024, 7, 8), rows2, tmp_path)
    assert bc.read_day(dt.date(2024, 7, 5), tmp_path) == rows
    assert bc.read_day(dt.date(2024, 7, 8), tmp_path) == rows2
    assert bc.read_day(dt.date(2024, 7, 9), tmp_path) is None
    assert bc.stored_days(tmp_path) == [dt.date(2024, 7, 5), dt.date(2024, 7, 8)]


def test_urls_and_which_format_comes_first():
    old = bc.urls_for(dt.date(2021, 1, 1))
    assert old[0] == ("https://archives.nseindia.com/content/historical/DERIVATIVES/2021/JAN/fo01JAN2021bhav.csv.zip", False)
    new = bc.urls_for(dt.date(2024, 7, 8))
    assert new[0] == ("https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv.zip", True)
    assert new[1][1] is False                                  # falls back to the other format


class FakeClient:
    def __init__(self, answers):
        self.answers, self.urls = list(answers), []

    def get(self, url, timeout=None):
        self.urls.append(url)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        code, body = a
        return httpx.Response(code, content=body)


def test_fetch_falls_back_backs_off_and_spots_holidays():
    sleeps = []
    th = bc.Throttle(0, sleep=lambda s: None)
    z = _zip("x.csv", NEW)
    c = FakeClient([(404, b"no"), (429, b""), httpx.ConnectError("down"), (200, _zip("fo.csv", OLD))])
    rows = bc.fetch_day(dt.date(2024, 7, 8), c, th, sleep=sleeps.append)
    assert len(rows) == 3 and "nsearchives" in c.urls[0] and "archives.nseindia" in c.urls[1]
    assert sleeps == [5, 4]                                    # 429 waits longer than a network error
    assert bc.fetch_day(dt.date(2024, 7, 8), FakeClient([(404, b""), (404, b"")]), th) is None   # a holiday
    with pytest.raises(bc.NotPublished):
        bc.fetch_day(dt.date(2024, 7, 8), FakeClient([(500, b"")] * 2), th, retries=2, sleep=lambda s: None)
    assert bc.fetch_day(dt.date(2024, 7, 8), FakeClient([(200, z)]), th)[0].kind == "PE"


def test_pending_skips_weekends_stored_days_and_known_holidays(tmp_path):
    bc.write_day(dt.date(2024, 7, 8), bc.parse_new(NEW), tmp_path)
    todo = bc.pending(dt.date(2024, 7, 5), dt.date(2024, 7, 10), {"2024-07-09": "none"}, tmp_path)
    assert todo == [dt.date(2024, 7, 5), dt.date(2024, 7, 10)]


def test_throttle_spaces_requests():
    t = [0.0]
    slept = []
    th = bc.Throttle(1.0, sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)), clock=lambda: t[0])
    th.wait()
    t[0] += 0.3
    th.wait()
    assert slept == [pytest.approx(0.7)]


# ---------------------------------------------------------------- calendar

def _d(s):
    return dt.date.fromisoformat(s)


def test_calendar_reads_expiries_from_the_data_including_the_weekday_move():
    days, listed = [], {}
    d = _d("2025-07-01")
    while d <= _d("2025-10-31"):
        if d.weekday() < 5 and d != _d("2025-08-15"):
            days.append(d)
        d += dt.timedelta(days=1)
    exps = set()
    for x in days:
        if x < _d("2025-09-01") and x.weekday() == 3:
            exps.add(x)
        if x >= _d("2025-09-01") and x.weekday() == 1:
            exps.add(x)
    exps.discard(_d("2025-08-14"))
    exps.add(_d("2025-08-13"))                                  # one holiday-shifted expiry (Wednesday)
    for x in days:
        listed[x] = sorted(e for e in exps if e >= x)[:3]
    cal = fcal.Calendar.from_listing(listed)
    assert _d("2025-08-13") in cal.expiries and _d("2025-08-14") not in cal.expiries
    assert cal.weekday_changes() == [(_d("2025-07-03"), "Thu"), (_d("2025-09-02"), "Tue")]
    assert cal.next_expiry(_d("2025-08-12")) == _d("2025-08-13")
    assert cal.next_expiry(_d("2025-08-13")) == _d("2025-08-13")
    assert cal.next_expiry(_d("2025-08-13"), after=True) == _d("2025-08-21")
    assert cal.sessions_before(_d("2025-08-18"), 1) == _d("2025-08-14")          # skips the 15 Aug holiday
    assert cal.sessions_between(_d("2025-08-13"), _d("2025-08-19")) == [_d("2025-08-14"), _d("2025-08-18")]
    assert cal.trading_days_to(_d("2025-08-14"), _d("2025-08-19")) == 2
    assert cal.weekday_by_year()[2025]["Tue"] >= 8


def test_calendar_listing_from_files_stops_at_the_last_date(tmp_path):
    for day in ("2024-07-05", "2024-07-08"):
        rows = bc.parse_old(OLD) if day == "2024-07-05" else bc.parse_new(NEW)
        bc.write_day(_d(day), rows, tmp_path)
    full = fcal.build_listing(tmp_path)
    assert full[_d("2024-07-05")] == [_d("2024-07-11")]
    part = fcal.load(tmp_path, last_date=_d("2024-07-05"))
    assert part.trading_days == [_d("2024-07-05")] and part.expiries == []


def test_cutoff_is_pinned():
    assert fcal.CUTOFF == dt.date(2025, 2, 5)


# ---------------------------------------------------------------- charges

def test_order_charges_by_hand():
    legs = [C.Leg("CE", 22500, "sell"), C.Leg("CE", 22700, "buy"), C.Leg("PE", 21500, "sell"), C.Leg("PE", 21300, "buy")]
    # opening: sells 2 x 10 x 65 = 1300 of premium, buys 2 x 2 x 65 = 260
    # brokerage 4 x 20 = 80; STT 0.15% x 1300 = 1.95; exchange 0.03553% x 1560 = 0.55; SEBI ~0; stamp 0.003% x 260 = 0.01
    # GST 18% x (80 + 0.5543 + 0.0016) = 14.50  -> 97.01
    assert C.order_charges(legs, [10, 2, 10, 2], opening=True) == pytest.approx(97.01)
    # closing the same: now the wings are SOLD (STT on 260) and the shorts bought (stamp on 1300)
    assert C.order_charges(legs, [10, 2, 10, 2], opening=False) == pytest.approx(80 + 0.39 + 0.55 + 0.04 + 14.5)


def test_exercise_stt_only_on_long_legs_in_the_money():
    legs = [C.Leg("CE", 22500, "sell"), C.Leg("CE", 22700, "buy"), C.Leg("PE", 21500, "sell"), C.Leg("PE", 21300, "buy")]
    assert C.exercise_charges(legs, 22000) == 0
    assert C.exercise_charges(legs, 22600) == 0                                  # only the short call is in the money
    assert C.exercise_charges(legs, 22800) == pytest.approx(100 * 65 * 0.0015)   # long call 100 points in


def test_slippage_and_margin():
    assert C.fill(10, "buy") == pytest.approx(10.2) and C.fill(10, "sell") == pytest.approx(9.8)
    assert C.fill(50, "sell") == pytest.approx(49.5)
    assert C.fill(0.1, "sell") == 0.05
    # (wing 200 - credit 20) x 65 + exposure 2% x 22000 x 65 on two shorts
    assert C.hedged_margin(180, 2, 22000) == pytest.approx(180 * 65 + 2 * 65 * 22000 * 0.02)


# ---------------------------------------------------------------- condor

F0, VOL = 22000.0, 0.12
DAYS = [_d(x) for x in ("2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11")]
EXP = _d("2024-01-11")


def _rows(d, fwd, settle=None):
    t = max((EXP - d).days, 0) / 365
    out = []
    for i in range(-40, 41):
        k = F0 + 50 * i
        for kind in ("CE", "PE"):
            if settle is not None:
                px, st = max((settle - k) if kind == "CE" else (k - settle), 0.0), settle
            else:
                spot = fwd * math.exp(-(pricing.RATE - pricing.DIV_YIELD) * t)
                px = round(max(pricing.black76(kind, spot, k, t, VOL, fwd=fwd).price, 0.05), 2)
                st = px
            out.append(bc.Row(d, "NIFTY", "OPT", EXP, k, kind, px, px, px, px, st, 1000, 100))
    return out


def _setup(path_fwd: dict):
    listing = {d: ([EXP] if d != _d("2024-01-04") else [_d("2024-01-04"), EXP]) for d in DAYS}
    cal = fcal.Calendar.from_listing(listing)

    def reader(d, root=None):
        if d == EXP:
            return _rows(d, path_fwd[d], settle=path_fwd[d])
        return _rows(d, path_fwd.get(d, F0))
    return cal, condor.Days(reader=reader)


def test_condor_held_to_expiry_keeps_the_credit_less_charges():
    cal, days = _setup({_d("2024-01-09"): F0, _d("2024-01-10"): F0, EXP: F0})
    r = condor.run({"days_before": 2, "short_delta": 0.15, "wing": 200, "stop_k": 2.0}, cal, days)
    assert r.skipped == {"entry day is not after the previous expiry": 1}      # 01-04's entry would be before the data
    (t,) = r.trades
    assert t.entry == _d("2024-01-09") and t.exit_reason == "expiry" and t.settle_index == F0
    assert t.forward == pytest.approx(F0, abs=0.5)
    s = t.strikes
    assert s["long_call"] - s["short_call"] == 200 and s["short_put"] - s["long_put"] == 200
    assert s["short_call"] > F0 > s["short_put"]
    assert abs(t.deltas["call"] - 0.15) < 0.05 and abs(t.deltas["put"] + 0.15) < 0.05
    opts = days.options(_d("2024-01-09"), EXP)
    legs = [C.Leg("CE", s["short_call"], "sell"), C.Leg("CE", s["long_call"], "buy"),
            C.Leg("PE", s["short_put"], "sell"), C.Leg("PE", s["long_put"], "buy")]
    fills = [C.fill(opts[(lg.kind, lg.strike)].close, lg.side) for lg in legs]
    credit = fills[0] - fills[1] + fills[2] - fills[3]
    assert t.credit == pytest.approx(credit, abs=0.01)
    assert t.exit_cost == 0
    assert t.pnl == pytest.approx(credit * 65 - C.order_charges(legs, fills, True), abs=0.02)
    assert t.margin == C.hedged_margin(200 - credit, 2, t.forward)
    assert t.rom_pct == pytest.approx(t.pnl / t.margin * 100, abs=1e-3)


def test_condor_stop_closes_at_the_first_bad_close_and_expiry_loss_is_capped():
    cal, days = _setup({_d("2024-01-10"): F0 + 400, EXP: F0 + 1000})
    (t,) = condor.run({"days_before": 2, "short_delta": 0.15, "wing": 200, "stop_k": 2.0}, cal, days).trades
    assert t.exit_reason == "stop" and t.exit == _d("2024-01-10")
    assert t.exit_cost - t.credit >= 2 * t.credit
    assert t.pnl < 0 and t.charges > 2 * 80                                      # two sets of four orders
    # without a stop (huge K) it is held to expiry: loss = wing - credit, plus exercise STT on the long call
    cal, days = _setup({_d("2024-01-10"): F0 + 400, EXP: F0 + 1000})
    (u,) = condor.run({"days_before": 2, "short_delta": 0.15, "wing": 200, "stop_k": 100.0}, cal, days).trades
    assert u.exit_reason == "expiry" and u.exit_cost == pytest.approx(200)
    assert u.pnl == pytest.approx((u.credit - 200) * 65 - u.charges, abs=0.02)
    assert u.charges > 4 * 20


def test_research_cannot_read_held_back_days():
    days = condor.Days(reader=lambda d, root=None: [], last_date=_d("2025-02-04"))
    days.options(_d("2025-02-04"), _d("2025-02-06"))
    with pytest.raises(fcal.HeldBackError):
        days.options(_d("2025-02-05"), _d("2025-02-06"))


def test_settlement_index_both_ways():
    rows = {("CE", 100.0): bc.Row(EXP, "NIFTY", "OPT", EXP, 100, "CE", 0, 0, 0, 5, 105, 0, 0),
            ("PE", 110.0): bc.Row(EXP, "NIFTY", "OPT", EXP, 110, "PE", 0, 0, 0, 5, 105, 0, 0)}
    assert condor.settlement_index(rows) == 105                                  # every option settles at the index
    rows = {("CE", 100.0): bc.Row(EXP, "NIFTY", "OPT", EXP, 100, "CE", 0, 0, 0, 5, 5, 0, 0),
            ("PE", 110.0): bc.Row(EXP, "NIFTY", "OPT", EXP, 110, "PE", 0, 0, 0, 5, 5, 0, 0),
            ("CE", 120.0): bc.Row(EXP, "NIFTY", "OPT", EXP, 120, "CE", 0, 0, 0, 0, 0, 0, 0)}
    assert condor.settlement_index(rows) == 105                                  # read back from K + C and K - P


def test_summary_and_drawdown():
    assert condor.max_drawdown([100, -50, -80, 40, -200, 500]) == -290
    mk = lambda d, p: condor.Trade(_d(d), _d(d), _d(d), "expiry", 1, {}, {}, 1, 0, 1, 1, p, 1000, 2)  # noqa: E731
    s = condor.summary([mk("2023-01-05", 100), mk("2023-02-02", -300), mk("2024-01-04", 50)])
    assert s["trades"] == 3 and s["win_rate"] == pytest.approx(0.667) and s["total_pnl"] == -150
    assert s["worst_week"] == -300 and s["by_year"][2023]["total"] == -200 and s["by_year"][2024]["trades"] == 1
    assert len(condor.GRID) == 12


# ---------------------------------------------------------------- the Lab path

@pytest.fixture
def fake_db(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(loop, "db", lambda: d)
    monkeypatch.setattr(fno_lab, "db", lambda: d)
    return d


def test_condor_proposal_is_tested_once_counts_toward_the_month_and_cannot_be_approved(fake_db, monkeypatch):
    seen = {}

    def fake_verdict(params, prior):
        seen["prior"], seen["params"] = prior, params
        return {"passed": False, "reasons": ["x"], "checks": {}, "unit": "%"}
    monkeypatch.setattr(fno_lab, "verdict", fake_verdict)
    fake_db.tables["proposals"] = [{"id": "old", "user_id": "u", "test": {"started_at": dt.datetime.now().isoformat()},
                                    "change": {}, "status": "failed"}]
    p = fno_lab.propose("u", {"days_before": 3, "wing": 200}, "condor beats cash")
    assert p["change"]["kind"] == "fno_condor" and p["change"]["params"]["short_delta"] == 0.15
    out = loop.start_test("u", p["id"])                                          # the Lab's own entry point
    assert out["status"] == "testing"
    for th in __import__("threading").enumerate():
        if th.name.startswith("fno-lab-"):
            th.join(5)
    row = loop._get("u", p["id"])
    assert row["status"] == "failed" and seen["prior"] == 1 and seen["params"]["days_before"] == 3
    assert loop.tests_this_month("u") == 2
    with pytest.raises(loop.LabError, match="already tested"):
        fno_lab.start_test("u", p["id"])
    fake_db.update("proposals", {"id": f"eq.{p['id']}"}, {"status": "passed"})
    with pytest.raises(loop.LabError, match="no automatic paper path"):
        loop.approve("u", p["id"])


def test_condor_params_are_checked():
    with pytest.raises(loop.LabError, match="no parameter"):
        fno_lab.check_params({"nope": 1})
    with pytest.raises(loop.LabError, match="multiple of 50"):
        fno_lab.check_params({"wing": 0})
    with pytest.raises(loop.LabError, match="whole number"):
        fno_lab.check_params({"days_before": 1.5})


def test_lab_scores_on_margin_against_cash_with_the_fixed_cutoff():
    def mk(entry, pnl):
        e = _d(entry)
        return condor.Trade(e, e + dt.timedelta(days=2), e + dt.timedelta(days=2), "expiry", 22000, {}, {}, 20, 0, 22000,
                            100, pnl, 50000, 2)
    early = [mk(f"2023-{m:02d}-0{w}", 500) for m in range(1, 13) for w in (2, 5)]
    late = [mk(f"2025-{m:02d}-0{w}", 100) for m in range(3, 13) for w in (2, 4, 6, 8)]
    cand, base = fno_lab.scored(early[:1])
    assert cand[0]["r"] == 1.0 and base[0]["r"] == pytest.approx(0.065 * 2 / 365 * 100, abs=1e-4)
    v = fno_lab.verdict({}, 0, trades=early + late)
    assert v["held_back_from"] == "2025-02-05" and v["candidate"]["held_back_trades"] == 40
    assert v["candidate"]["held_back"] == 0.2 and v["passed"] is True
    assert v["held_back_rupees_per_lot"]["trades"] == 40
    v2 = fno_lab.verdict({}, 0, trades=early + [mk(t.entry.isoformat(), 20) for t in late])
    assert v2["passed"] is False and not v2["checks"]["improves_by_enough"]


# ---------------------------------------------------------------- the recorder

def _ist(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=IST)


def test_recorder_runs_only_in_market_hours():
    assert recorder.should_snapshot(_ist("2026-11-13T09:15:00"))
    assert recorder.should_snapshot(_ist("2026-11-13T15:29:59"))
    assert not recorder.should_snapshot(_ist("2026-11-13T09:10:00"))              # pre-open
    assert not recorder.should_snapshot(_ist("2026-11-13T15:30:00"))              # closed
    assert not recorder.should_snapshot(_ist("2026-11-14T11:00:00"))              # Saturday
    assert not recorder.should_snapshot(_ist("2026-11-24T11:00:00"))              # Guru Nanak Jayanti
    assert recorder.next_slot(_ist("2026-11-13T09:15:00")) == _ist("2026-11-13T09:20:00")
    assert recorder.next_slot(_ist("2026-11-13T09:17:31")) == _ist("2026-11-13T09:20:00")


def test_recorder_tick_skips_when_closed_stores_when_open_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setitem(recorder.state, "backoff", 0)
    called = []

    def snap(now):
        called.append(now)
        return {"ts": now.isoformat(), "spot": 1, "expiries": []}
    assert recorder.tick(_ist("2026-11-14T11:00:00"), tmp_path, snap) == "closed" and not called
    assert recorder.tick(_ist("2026-11-13T11:00:00"), tmp_path, snap) == "stored"
    assert recorder.tick(_ist("2026-11-13T11:05:00"), tmp_path, snap) == "stored"
    assert [s["ts"] for s in recorder.read_day(_d("2026-11-13"), tmp_path)] == ["2026-11-13T11:00:00+05:30",
                                                                                 "2026-11-13T11:05:00+05:30"]

    def refused(now):
        raise RuntimeError("NSE answered HTTP 403")
    assert recorder.tick(_ist("2026-11-13T11:10:00"), tmp_path, refused) == "failed"
    assert recorder.state["backoff"] == 600 and "403" in recorder.state["last_error"]
    recorder.tick(_ist("2026-11-13T11:20:00"), tmp_path, refused)
    assert recorder.state["backoff"] == 1200
    recorder.tick(_ist("2026-11-13T11:40:00"), tmp_path, snap)
    assert recorder.state["backoff"] == 0 and recorder.state["last_error"] is None
    st = recorder.status(_ist("2026-11-13T12:00:00"), tmp_path)
    assert st["today"] == 3 and st["last_snapshot"] == "2026-11-13T11:40:00+05:30" and st["market_open"]


def test_snapshot_keeps_strikes_near_spot_two_expiries_and_rejects_modeled_chains():
    from app.options import chain as ch
    exps = [_d("2026-11-12"), _d("2026-11-17"), _d("2026-11-24"), _d("2026-12-01")]

    def make(source):
        def fetch(u, e):
            c = ch.Chain(underlying=u, expiry=e, spot=25000, as_of="13-Nov-2026 11:00:00", source=source, expiries=exps)
            for k in (22000, 22500, 25000, 27500, 28000):
                for kind in ("CE", "PE"):
                    c.rows.setdefault(float(k), {})[kind] = ch.Quote(kind, float(k), 10, 9.5, 10.5, 12.0, 100, 0, 5)
            return c
        return fetch
    s = recorder.snapshot(_ist("2026-11-13T11:00:00"), lambda u: exps, make("nse"), future=lambda: None)
    assert [e["expiry"] for e in s["expiries"]] == ["2026-11-17", "2026-11-24"]
    assert sorted({r["k"] for r in s["expiries"][0]["rows"]}) == [22500, 25000, 27500]
    assert s["spot"] == 25000 and s["expiries"][0]["rows"][0]["bid"] == 9.5
    assert s["expiries"][0]["source"] == "nse"
    k = recorder.snapshot(_ist("2026-11-13T11:00:00"), lambda u: exps, make("kite"), future=lambda: None)
    assert k["expiries"][1]["source"] == "kite"                                  # a Kite chain is recorded too
    with pytest.raises(ch.ChainError, match="modeled"):
        recorder.snapshot(_ist("2026-11-13T11:00:00"), lambda u: exps, make("modeled"), future=lambda: None)
