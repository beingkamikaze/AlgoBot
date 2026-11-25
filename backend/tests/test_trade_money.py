"""Every gold idea and ticket says what it loses at the stop and makes at the target, in dollars, after costs."""

from __future__ import annotations

import pytest

from app.global_markets import paper
from app.global_markets.agent import brain
from app.global_markets.instruments import resolve
from app.global_markets.risk import money
from app.telegram import service as tg


def idea(side="buy", ref=4000.0, stop=3990.0, target=4020.0):
    return {"kind": "trade", "symbol": "XAUUSD", "side": side, "ref_price": ref, "stop": stop, "target": target}


def test_money_counts_costs_on_both_sides():
    m = money(resolve("XAUUSD"), 0.02, 4000.0, 3990.0, 4020.0)           # 2 oz
    assert m["units"] == 2.0 and m["costs"] == round(2 * paper.COSTS.round_trip_per_unit(100), 2)
    assert m["loss_at_stop"] == round(2 * 10 + m["costs"], 2) and m["gain_at_target"] == round(2 * 20 - m["costs"], 2)


def test_an_idea_is_sized_from_your_max_loss_like_its_ticket_would_be():
    e = paper.idea_estimate(idea(), {"max_loss_per_trade": 20})
    assert e["lots"] > 0 and e["loss_at_stop"] <= 20 and e["gain_at_target"] > e["loss_at_stop"]
    assert e["entry"] > 4000.0                                            # a buy pays the half spread and the buffer
    bigger = paper.idea_estimate(idea(), {"max_loss_per_trade": 60})
    assert bigger["lots"] > e["lots"] and bigger["loss_at_stop"] <= 60


def test_an_idea_too_wide_for_your_limit_says_so_instead_of_a_size():
    e = paper.idea_estimate(idea(stop=3900.0, target=4200.0), {"max_loss_per_trade": 5})
    assert e["lots"] == 0.0 and "too far for your max loss of $5.00" in e["note"]
    assert paper.idea_estimate({"kind": "pass"}, None) is None


def test_the_telegram_idea_and_ticket_show_both_amounts():
    i = {**idea(), "id": "i1", "setup": "pullback", "price_source": "spot", "expires_at": None,
         "proposal": {"plan": {"entry": 4000.0, "stop": 3990.0, "target": 4020.0, "reward_risk": 2.0}, "why": ["x"]}, "review": {}}
    e = paper.idea_estimate(i, {"max_loss_per_trade": 20})
    text = tg.idea_text(i, e)
    assert f"lose ${e['loss_at_stop']:,.2f}</b> at the stop" in text and f"make ${e['gain_at_target']:,.2f}</b> at the target" in text
    o = {"id": "o1", "side": "buy", "lots": 0.01, "symbol": "XAUUSD", "params": {"limit": 1, "stop": 0.5, "target": 2},
         "risk_verdict": {"verdict": "PASS", "numbers": {"max_loss": 12.5, "gain_at_target": 24.1}}}
    assert "Lose $12.50</b> at the stop · <b>make $24.10</b> at the target" in tg.ticket_text(o)


@pytest.mark.parametrize("phrase", ["the last-12-bar low 3526.3", "under the 5m EMA20 and EMA 50", "RSI 14 is low on the 15m"])
def test_bar_counts_and_indicator_periods_are_not_made_up_numbers(phrase):
    facts = {"price": {"last": 3527.55}, "typical_5m_move": 5.0, "trend": {"5m": {"last_12_low": 3526.3}}}
    p = {"side": "sell", "stop": 3537.0, "target": 3510.0, "why": [phrase], "wrong_if": ""}
    assert not any("not in the facts" in x for x in brain.rule_check(facts, p))
