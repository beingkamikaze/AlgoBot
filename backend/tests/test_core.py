import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.core.calendar import IST, market_status
from app.core.costs import equity_intraday_charges, round_to_tick, tick_for
from app.domain.signal import Confidence, Evidence, Explanation, TradePlan, grade_confidence
from app.main import app


def ist(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=IST)


class TestCalendar:
    def test_open_session(self):
        m = market_status(ist("2026-10-23 10:00"))
        assert m.is_open and m.phase == "open"

    def test_pre_open_and_after_close(self):
        assert market_status(ist("2026-10-23 09:05")).phase == "pre-open"
        after = market_status(ist("2026-10-23 16:00"))
        assert after.phase == "post-close"
        assert after.next_open == ist("2026-10-26 09:15")      # Friday -> Monday

    def test_holiday(self):
        m = market_status(ist("2026-10-20 11:00"))              # Dussehra
        assert not m.is_open and "Dussehra" in m.reason
        assert m.next_open == ist("2026-10-21 09:15")


class TestCosts:
    def test_zerodha_equity_intraday_matches_the_skill(self):
        c = equity_intraday_charges(31360.0, 30894.5)           # 49 BATAINDIA, from the nse-budget run
        assert c.brokerage == pytest.approx(18.68, abs=0.01)
        assert c.stt == pytest.approx(7.72, abs=0.01)
        assert c.total == pytest.approx(33.03, abs=0.01)

    def test_brokerage_cap(self):
        assert equity_intraday_charges(200_000, 200_000).brokerage == 40.0

    def test_ticks(self):
        assert tick_for(167) == 0.01 and tick_for(639.35) == 0.05 and tick_for(2530) == 0.10
        assert round_to_tick(633.72, 0.05, up=False) == 633.70
        assert round_to_tick(639.99, 0.05, up=True) == 640.00


class TestExplanation:
    def test_card_needs_a_real_stop(self):
        with pytest.raises(ValueError):
            Explanation(rule="x", invalidation="y", plan=TradePlan(entry=100, stop=100, stop_reason="z"))

    def test_confidence_is_a_fixed_rule(self):
        assert grade_confidence([]) == Confidence.NONE
        assert grade_confidence([Evidence(source="backtest", trades=20, expectancy_r=0.5)]) == Confidence.NONE
        assert grade_confidence([Evidence(source="backtest", trades=200, expectancy_r=-0.05)]) == Confidence.LOW
        assert grade_confidence([Evidence(source="backtest", trades=60, expectancy_r=0.2)]) == Confidence.MEDIUM
        assert grade_confidence([Evidence(source="backtest", trades=212, expectancy_r=0.18,
                                          profit_factor=1.4)]) == Confidence.HIGH


class TestApi:
    def test_health(self):
        r = TestClient(app).get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["mode"] == "paper"
        assert body["market"]["phase"] in {"closed", "pre-open", "open", "post-close"}

    def test_me_needs_a_token(self):
        r = TestClient(app).get("/api/me")
        assert r.status_code in (401, 503)
