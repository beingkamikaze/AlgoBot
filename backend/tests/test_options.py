import dataclasses
import datetime as dt

import pytest

from app.core.calendar import IST
from app.options import pricing
from app.options import structures as S
from app.options.backtest import _t, expiry_for, weekly_expiry_on_or_after
from app.options.chain import Chain, Quote
from app.paper.options import plan_exits, value_now
from app.risk.engine import AccountState, Limits, StructureRequest, check_structure

NOW = dt.datetime(2026, 10, 23, 11, 0, tzinfo=IST)


class TestPricing:
    def test_put_call_parity(self):
        s, k, t, v = 22600, 22600, 4 / 365, 0.14
        c, p = pricing.black76("CE", s, k, t, v).price, pricing.black76("PE", s, k, t, v).price
        f = pricing.forward(s, t)
        df = __import__("math").exp(-pricing.RATE * t)
        assert c - p == pytest.approx(df * (f - k), abs=0.01)                 # C - P = DF x (F - K)

    def test_implied_vol_round_trip(self):
        price = pricing.black76("PE", 22600, 22400, 5 / 365, 0.163).price
        assert pricing.implied_vol("PE", price, 22600, 22400, 5 / 365) == pytest.approx(0.163, abs=1e-4)

    def test_greek_signs(self):
        g = pricing.black76("CE", 22600, 22600, 4 / 365, 0.14)
        assert 0.45 < g.delta < 0.55 and g.gamma > 0 and g.theta < 0 and g.vega > 0
        assert -0.55 < pricing.black76("PE", 22600, 22600, 4 / 365, 0.14).delta < -0.45

    def test_parity_forward(self):
        t = 4 / 365
        c, p = pricing.black76("CE", 22600, 22600, t, 0.14, fwd=22594.8).price, pricing.black76("PE", 22600, 22600, t, 0.14, fwd=22594.8).price
        assert pricing.parity_forward(c, p, 22600, t) == pytest.approx(22594.8, abs=0.01)


def toy_chain(spot=22600.0, expiry=dt.date(2026, 10, 27)) -> Chain:
    c = Chain(underlying="NIFTY", expiry=expiry, spot=spot, as_of="test", source="nse", expiries=[expiry])
    t = c.years_left(NOW)
    for i in range(-12, 13):
        k = spot + 50 * i
        for kind in ("CE", "PE"):
            p = pricing.black76(kind, spot, k, t, 0.14).price
            c.rows.setdefault(k, {})[kind] = Quote(kind=kind, strike=k, ltp=round(p, 2), bid=round(p * 0.99, 2),
                                                   ask=round(p * 1.01, 2), iv=14.0, oi=1000)
    return c


class TestStructures:
    def test_bull_call_spread_limits(self):
        s = S.build(toy_chain(), "bull_call_spread", {"width": 4}, NOW)
        debit = -s.net_premium
        assert s.max_loss == pytest.approx(debit * 65, abs=1)
        assert s.max_profit == pytest.approx((200 - debit) * 65, abs=1)
        assert not s.naked and len(s.breakevens) == 1

    def test_iron_condor_is_defined_risk_credit(self):
        s = S.build(toy_chain(), "iron_condor", {"width": 4, "short_delta": 0.2}, NOW)
        assert s.net_premium > 0 and not s.naked and len(s.breakevens) == 2
        assert s.max_loss == pytest.approx((200 - s.net_premium) * 65, abs=1)
        assert abs(s.greeks["delta"]) < 10 and s.greeks["theta"] > 0 and s.greeks["vega"] < 0

    def test_naked_straddle_flagged(self):
        s = S.build(toy_chain(), "short_straddle", {}, NOW)
        assert s.naked and s.max_loss is None and s.margin > 100000

    def test_long_call_unlimited_profit(self):
        s = S.build(toy_chain(), "long_call", {}, NOW)
        assert s.max_profit is None and s.max_loss == pytest.approx(-s.net_premium * 65, abs=1)

    def test_conservative_fills(self):
        c = toy_chain()
        s = S.build(c, "long_put", {}, NOW)
        assert s.legs[0].price == c.quote(s.legs[0].strike, "PE").ask


class TestExitsAndMarking:
    def test_debit_exits(self):
        s = S.build(toy_chain(), "long_call", {}, NOW)
        e = plan_exits(s)
        assert e["type"] == "debit" and e["planned_loss"] == pytest.approx(-s.net_premium * 65 * 0.5, abs=0.5)

    def test_credit_exits_capped_by_max_loss(self):
        s = S.build(toy_chain(), "iron_fly", {"width": 1}, NOW)                # narrow wings: max loss < credit x 1
        e = plan_exits(s, {"stop_mult": 5})
        assert e["planned_loss"] == pytest.approx(s.max_loss, abs=0.5)

    def test_value_now_closes_at_the_far_side(self):
        c = toy_chain()
        legs = [{"kind": "CE", "strike": 22600.0, "side": "buy", "lots": 1}, {"kind": "CE", "strike": 22800.0, "side": "sell", "lots": 1}]
        v = value_now(legs, c)
        assert v == pytest.approx(c.quote(22600, "CE").bid - c.quote(22800, "CE").ask)


def req(**kw) -> StructureRequest:
    base = dict(label="NIFTY IC", underlying="NIFTY", expiry=dt.date(2026, 10, 27), lot_size=65,
                legs=[{"kind": "CE", "strike": 22900, "side": "sell", "lots": 1, "price": 36, "spread_pct": 2, "oi": 1000}],
                max_loss=10000, planned_loss=2800, charges=200, margin=60000, naked=False, source="nse")
    base.update(kw)
    return StructureRequest(**base)


ST = AccountState(capital=1_000_000, cash_free=1_000_000)
LIM = dataclasses.replace(Limits(), max_loss_per_trade_fno=5000)


class TestOptionRisk:
    def test_passes_inside_limits(self):
        v = check_structure(req(), LIM, ST, NOW)
        assert v.verdict == "PASS", v.reasons

    def test_fno_limit_is_separate(self):
        assert "per trade" in " ".join(check_structure(req(), Limits(), ST, NOW).reasons)        # ₹2,000 default
        assert check_structure(req(), LIM, ST, NOW).verdict == "PASS"                            # ₹5,000 F&O

    def test_naked_blocked_unless_allowed(self):
        assert "switched off" in " ".join(check_structure(req(naked=True, max_loss=None), LIM, ST, NOW).reasons)
        allowed = dataclasses.replace(LIM, allow_naked_short_options=True)
        assert check_structure(req(naked=True, max_loss=None), allowed, ST, NOW).verdict == "PASS"

    def test_illiquid_leg_and_expiry_cutoff(self):
        bad = req(legs=[{"kind": "CE", "strike": 24000, "side": "sell", "lots": 1, "price": 1, "spread_pct": 40, "oi": 5}])
        assert "illiquid" in " ".join(check_structure(bad, LIM, ST, NOW).reasons)
        late = dt.datetime(2026, 10, 27, 14, 45, tzinfo=IST)
        assert "expiry day" in " ".join(check_structure(req(), LIM, ST, late).reasons)

    def test_margin_against_cash(self):
        assert "margin" in " ".join(check_structure(req(margin=2_000_000), LIM, ST, NOW).reasons)


class TestExpiries:
    def test_weekly_and_holiday_shift(self):
        assert weekly_expiry_on_or_after(dt.date(2026, 10, 23)) == dt.date(2026, 10, 27)
        assert weekly_expiry_on_or_after(dt.date(2026, 11, 4)) == dt.date(2026, 11, 9)     # 10 Nov is a holiday
        assert expiry_for("NIFTY", dt.date(2026, 10, 27), 1) == dt.date(2026, 11, 3)
        assert expiry_for("BANKNIFTY", dt.date(2026, 10, 28), 0) == dt.date(2026, 11, 23)

    def test_trading_time_decays_only_in_session(self):
        fri_close = int(dt.datetime(2026, 10, 23, 15, 30, tzinfo=IST).timestamp())
        mon_open = int(dt.datetime(2026, 10, 26, 9, 15, tzinfo=IST).timestamp())
        assert _t(fri_close, dt.date(2026, 10, 27)) == pytest.approx(_t(mon_open, dt.date(2026, 10, 27)))
        assert _t(int(dt.datetime(2026, 10, 27, 15, 30, tzinfo=IST).timestamp()), dt.date(2026, 10, 27)) == 0
