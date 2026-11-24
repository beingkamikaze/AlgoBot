import dataclasses
import datetime as dt

import pytest

from app.backtest.engine import BacktestConfig, size
from app.core.calendar import IST
from app.core.costs import futures_charges, index_derivative_tick, options_charges
from app.core.instruments import active_expiry, monthly_expiry, resolve
from app.paper.service import size_for
from app.risk.engine import AccountState, Limits, OrderRequest, check

NOW = dt.datetime(2026, 10, 23, 10, 30, tzinfo=IST)


class TestContracts:
    def test_last_tuesday_and_holiday_shift(self):
        assert monthly_expiry(2026, 10) == dt.date(2026, 10, 27)
        assert monthly_expiry(2026, 11) == dt.date(2026, 11, 23)      # 24 Nov is Guru Nanak Jayanti
        assert monthly_expiry(2026, 12) == dt.date(2026, 12, 29)

    def test_rolls_two_trading_days_before_expiry(self):
        assert active_expiry(dt.date(2026, 10, 23)) == dt.date(2026, 10, 27)
        assert active_expiry(dt.date(2026, 10, 26)) == dt.date(2026, 11, 23)   # one trading day left: roll

    def test_resolve(self):
        f = resolve("nifty-fut", dt.date(2026, 10, 23))
        assert (f.kind, f.tradingsymbol, f.lot_size, f.data_symbol) == ("future", "NIFTY26OCTFUT", 65, "NIFTY")
        assert resolve("BANKNIFTY-FUT", dt.date(2026, 10, 26)).tradingsymbol == "BANKNIFTY26NOVFUT"
        assert resolve("RELIANCE").kind == "equity" and resolve("RELIANCE").lot_size == 1

    def test_stock_futures_use_nse_lot_sizes(self, monkeypatch):
        from app.market import fno
        monkeypatch.setattr(fno, "table", lambda refresh=False: {"TITAN": {"NOV-26": 175, "DEC-26": 175}, "RELIANCE": {"NOV-26": 500}})
        f = resolve("TITAN-FUT", dt.date(2026, 11, 2))
        assert (f.tradingsymbol, f.lot_size, f.data_symbol, f.expiry, f.is_index) == ("TITAN26NOVFUT", 175, "TITAN", dt.date(2026, 11, 23), False)
        assert f.tick(4472) == 0.10 and resolve("NIFTY-FUT", dt.date(2026, 11, 2)).tick(23000) == 0.10
        assert f.tick(4472.05) == 0.10 and resolve("NIFTY-FUT", dt.date(2026, 11, 2)).tick(14000) == 0.05
        with pytest.raises(ValueError, match="no futures or options"):
            resolve("NOTFNO-FUT", dt.date(2026, 11, 2))

    def test_fno_lot_file_parsing(self):
        from app.market import fno
        text = ("UNDERLYING ,SYMBOL ,NOV-26 ,DEC-26 ,JAN-27\n"
                "NIFTY 50 ,NIFTY ,65 ,65 ,65\n"
                "TITAN COMPANY LIMITED ,TITAN ,175 ,175 ,\n"
                "BAJAJ AUTO ,BAJAJ-AUTO ,75 ,75 ,75\n")
        t = fno.parse(text)
        assert "NIFTY" not in t and t["TITAN"] == {"NOV-26": 175, "DEC-26": 175} and t["BAJAJ-AUTO"]["JAN-27"] == 75


class TestCharges:
    def test_futures_round_trip(self):
        c = futures_charges(65 * 22600, 65 * 22630)
        assert c.brokerage == 40.0                                       # capped at ₹20 an order
        assert c.stt == pytest.approx(65 * 22630 * 0.0005, abs=0.01)     # 0.05% on the sell side
        assert 850 < c.total < 900

    def test_options_on_premium(self):
        c = options_charges(65 * 120, 65 * 150)
        assert c.brokerage == 40.0 and c.stt == pytest.approx(65 * 150 * 0.0015, abs=0.01)

    def test_index_tick(self):
        assert index_derivative_tick(22600) == 0.10 and index_derivative_tick(54287) == 0.20


FUT = dict(symbol="NIFTY26OCTFUT", side="buy", qty=65, limit=22600.0, stop=22560.0, target=22680.0,
           kind="future", lot_size=65, margin_pct=12.0)
LIM = dataclasses.replace(Limits(), max_loss_per_trade=5000)
ST = AccountState(capital=1_000_000, cash_free=1_000_000)


class TestRiskFutures:
    def test_one_lot_passes_on_margin_not_full_value(self):
        v = check(OrderRequest(**FUT), LIM, ST, NOW, last_price=22598)
        assert v.verdict == "PASS", v.reasons
        assert v.numbers["value"] == 65 * 22600 and v.numbers["margin"] == pytest.approx(65 * 22600 * 0.12)
        assert any("leveraged" in w for w in v.warnings)

    def test_whole_lots_and_tick(self):
        assert "whole lots" in " ".join(check(OrderRequest(**{**FUT, "qty": 50}), LIM, ST, NOW, 22598).reasons)
        assert "tick" in " ".join(check(OrderRequest(**{**FUT, "limit": 22600.05}), LIM, ST, NOW, 22598).reasons)

    def test_default_two_thousand_cannot_afford_a_lot(self):
        v = check(OrderRequest(**FUT), Limits(), ST, NOW, last_price=22598)   # 40 points x 65 = 2,600 + charges
        assert "per trade" in " ".join(v.reasons)

    def test_margin_against_free_cash(self):
        poor = dataclasses.replace(ST, cash_free=150_000)
        assert "margin" in " ".join(check(OrderRequest(**FUT), LIM, poor, NOW, 22598).reasons)


class TestSizing:
    def test_paper_sizes_in_lots(self):
        inst = resolve("NIFTY-FUT", dt.date(2026, 10, 23))
        assert size_for(22600, 22560, LIM, ST, inst) == 65                  # 1 lot: 2,600 + ~880 charges < 5,000
        assert size_for(22600, 22560, Limits(), ST, inst) == 0              # ₹2,000 max loss: not even one lot
        assert size_for(22600, 22590, LIM, ST, inst) % 65 == 0

    def test_backtest_fixed_lot(self):
        inst = resolve("BANKNIFTY-FUT", dt.date(2026, 10, 23))
        assert size(54000, 200, BacktestConfig(fixed_lots=1), inst) == 30
        assert size(54000, 200, BacktestConfig(fixed_lots=2), inst) == 60
