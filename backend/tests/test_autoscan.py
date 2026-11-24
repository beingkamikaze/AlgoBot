from app.signals import autoscan


def sig(strategy, symbol, side, conf, t, fresh=False, r=None, trades=200, version="1"):
    return {"strategy_id": strategy, "strategy_version": version, "symbol": symbol, "side": side, "created_at": t, "fresh": fresh,
            "explanation": {"confidence": conf, "evidence": [] if r is None else [{"expectancy_r": r, "trades": trades}]}}


def test_repeats_collapse_to_the_latest():
    out = autoscan.rank([sig("vwap", "TCS", "buy", "low", "2026-10-30T10:00"), sig("vwap", "TCS", "buy", "low", "2026-10-30T11:00"),
                         sig("vwap", "TCS", "sell", "low", "2026-10-30T10:30")])
    buy = next(i for i in out if i["side"] == "buy")
    assert len(out) == 2 and len(buy["signals"]) == 1 and buy["best"]["created_at"] == "2026-10-30T11:00"
    assert buy["conflict"] and next(i for i in out if i["side"] == "sell")["conflict"]


def test_ideas_ranked_by_confidence_then_agreement_then_fresh_then_evidence():
    out = autoscan.rank([
        sig("orb", "A", "buy", "low", "2026-10-30T10:00", fresh=True, r=0.5),
        sig("orb", "B", "buy", "medium", "2026-10-30T09:45", r=0.05),
        sig("orb", "C", "sell", "low", "2026-10-30T10:00", r=0.2),
        sig("supertrend", "C", "sell", "low", "2026-10-30T10:15", r=-0.1),
        sig("orb", "D", "buy", "low", "2026-10-30T10:00", r=0.9),
        sig("orb", "E", "buy", "no track record", "2026-10-30T11:00", fresh=True),
    ])
    assert [i["symbol"] for i in out] == ["B", "C", "A", "D", "E"]       # medium; 2 agree; fresh; better evidence; none
    c = out[1]
    assert c["strategies"] == ["orb", "supertrend"] and c["best"]["strategy_id"] == "orb" and len(c["signals"]) == 2
    assert out[0]["rank"] == 1 and not c["conflict"]


def test_lab_versions_are_scanned_but_never_tuned(monkeypatch):
    class D:
        def select(self, *a, **k):
            return [{"strategy_id": "orb", "version": "1+min_range_pct=0.5", "params": {"min_range_pct": 0.5, "or_minutes": 15}, "status": "paper"},
                    {"strategy_id": "orb", "version": "1+target_r=1.5", "params": {"target_r": 1.5}, "status": "retired"}]
    monkeypatch.setattr(autoscan, "db", lambda: D())
    p = autoscan.plan("u1")
    orb = [x for x in p if x["strategy"] == "orb"]
    assert {(x["version"], x["universe"]) for x in orb} == {("1", "NIFTY100"), ("1", "INDEXFUT"),
                                                            ("1+min_range_pct=0.5", "NIFTY100"), ("1+min_range_pct=0.5", "INDEXFUT")}
    assert [x["params"] for x in orb if x["label"] == "lab"][0] == {"min_range_pct": 0.5}   # only what differs from defaults
    assert not any(x["strategy"] in ("orb_option_buy", "iron_fly_intraday") for x in p)
    assert len(autoscan.plan(None)) == 10                               # signed out: 5 scanned strategies x 2 universes, defaults only
    assert not any(x["strategy"] == "intraday_momentum" for x in autoscan.plan(None))   # research only


def test_ideas_say_whether_futures_and_options_exist(monkeypatch):
    import app.signals.fno_plan as F
    monkeypatch.setattr(F.fno, "is_fno", lambda s: s == "TITAN")
    out = autoscan.rank([sig("orb", "TITAN", "buy", "low", "t1"), sig("orb", "IRCTCX", "buy", "low", "t1"), sig("orb", "NIFTY-FUT", "sell", "low", "t1")])
    by = {i["symbol"]: i for i in out}
    assert by["TITAN"]["fno"] and not by["IRCTCX"]["fno"] and by["NIFTY-FUT"]["fno"]
    assert by["NIFTY-FUT"]["segment"] == "index" and by["TITAN"]["segment"] == "stock"
