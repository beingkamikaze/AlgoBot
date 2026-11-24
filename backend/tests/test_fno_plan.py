import datetime as dt

from app.core.calendar import IST
from app.options.chain import Chain, Quote
from app.risk.engine import Limits
from app.signals import fno_plan as F

NOW = dt.datetime(2026, 11, 2, 11, 0, tzinfo=IST)


def fake_chain(u="TITAN", spot=4436.5, lot=175, expiry=dt.date(2026, 11, 23)):
    c = Chain(underlying=u, expiry=expiry, spot=spot, as_of="02-Nov-2026 11:00:00", source="nse", expiries=[expiry], lot=lot)
    for k, ce, pe in ((4400.0, 144.0, 75.0), (4450.0, 115.5, 95.8), (4500.0, 90.5, 121.0)):
        c.rows[k] = {"CE": Quote("CE", k, ce, ce - 0.6, ce + 0.6, 24.0, 1500), "PE": Quote("PE", k, pe, pe - 0.6, pe + 0.6, 24.0, 1500)}
    return c


def setup(monkeypatch, c):
    monkeypatch.setattr(F.fno, "is_fno", lambda s: s == "TITAN")
    monkeypatch.setattr(F.fno, "lot_size", lambda s, e: 175)
    monkeypatch.setattr(F.ch, "nearest_expiry", lambda u, d, min_days=0: c.expiry)
    monkeypatch.setattr(F.ch, "chain", lambda u, e=None, allow_modeled=True: c)


def test_buy_signal_buys_the_atm_call_and_sizes_one_lot(monkeypatch):
    c = fake_chain()
    setup(monkeypatch, c)
    p = F.plan("TITAN", "buy", 4472.0, 4423.49, 4569.02, Limits(), NOW)
    o, f = p["option"], p["future"]
    assert p["available"] and not p["is_index"]
    assert o["contract"] == "TITAN26NOV4450CE" and o["kind"] == "CE" and o["ask"] == 116.1 and o["qty"] == 175
    assert o["cost"] == round(116.1 * 175, 2) and o["pnl_at_stop"] < 0 < o["pnl_at_target"]
    assert o["planned_loss"] == round(o["cost"] * 0.5, 2) and o["fits_limit"] is False        # ₹2,000 default F&O limit
    assert o["ticket"]["template"] == "long_call" and o["ticket"]["exits"]["underlying_stop"] == 4423.49
    assert f["contract"] == "TITAN26NOVFUT" and f["lot_size"] == 175 and f["loss_at_stop"] > abs(4472 - 4423.49) * 175
    assert f["ticket"] == {"symbol": "TITAN-FUT", "side": "buy", "stop": 4423.49, "target": 4569.02}


def test_sell_signal_buys_the_put_and_flags_a_failed_setup(monkeypatch):
    c = fake_chain(spot=4480.0)
    setup(monkeypatch, c)
    o = F.plan("TITAN", "sell", 4440.0, 4470.0, 4380.0, None, NOW)["option"]
    assert o["kind"] == "PE" and o["ticket"]["template"] == "long_put" and o["fits_limit"] is None
    assert any("beyond the signal's stop" in w for w in o["warnings"])


def test_no_fno_means_shares_only(monkeypatch):
    monkeypatch.setattr(F.fno, "is_fno", lambda s: False)
    p = F.plan("IRCTCX", "buy", 100, 98, 104)
    assert not p["available"] and "only the shares" in p["reason"]


def test_a_failed_setup_offers_nothing_misleading(monkeypatch):
    c = fake_chain(spot=4400.0)                    # a buy signal whose stop (4423.49) is already broken
    setup(monkeypatch, c)
    p = F.plan("TITAN", "buy", 4472.0, 4423.49, 4569.02, None, NOW)
    assert p["setup_failed"] and p["option"]["pnl_at_stop"] is None and p["option"]["est_at_stop"] is None
