import pytest

from app.ai import explain
from app.ai.client import AIError, AIUnavailable
from app.ai.grounding import ungrounded
from app.ai.knowledge import INDICATORS, as_text
from app.journal import postmortem

CARD = {
    "symbol": "RELIANCE", "side": "sell", "strategy_id": "orb", "created_at": "2026-10-23T10:15:00+05:30",
    "explanation": {
        "rule": "ORB short: the 10:10 bar closed at 1,178.30, below the range low of 1,183.00 (-0.40%).",
        "conditions": [{"name": "Range width", "met": True, "detail": "1.56% (allowed 0.3–2.5%)"}],
        "plan": {"entry": 1178.3, "stop": 1192.3, "target": 1150.3, "quantity": 142, "max_loss_with_charges": 2094.69},
        "evidence": [{"source": "backtest", "trades": 2087, "expectancy_r": -0.1764, "win_rate": 0.3776}],
        "similar": None, "risks": ["Prices can lag."], "invalidation": "A close back above 1,183.00.",
        "confidence": "low", "summary": "TEMPLATE",
    },
}


class TestGrounding:
    def test_accepts_numbers_from_the_facts_rounded_or_as_percent(self):
        f = explain.facts(CARD)
        assert ungrounded("Sell near 1,178.30, stop 1192.3; -0.18R over 2087 trades, win rate 38%.", f) == []

    def test_unicode_dash_ranges_are_read(self):
        assert ungrounded("allowed up to 2.5%", {"d": "1.56% (allowed 0.3–2.5%)"}) == []

    def test_catches_invented_numbers(self):
        assert ungrounded("This setup wins 73% of the time and targets 1,250.", explain.facts(CARD)) == [73.0, 1250.0]

    def test_small_counting_numbers_are_fine(self):
        assert ungrounded("Two things: 1 and 2.", {}) == []


class TestSummaryFallbacks:
    def test_no_model_keeps_the_template(self, monkeypatch):
        def boom(*a, **k):
            raise AIUnavailable("no key")
        monkeypatch.setattr(explain, "chat_json", boom)
        out = explain.summarize(CARD)
        assert out["source"] == "template" and out["summary"] == "TEMPLATE"

    def test_model_error_keeps_the_template(self, monkeypatch):
        def boom(*a, **k):
            raise AIError("timeout")
        monkeypatch.setattr(explain, "chat_json", boom)
        assert explain.summarize(CARD)["source"] == "template"

    def test_invented_number_is_rejected(self, monkeypatch):
        monkeypatch.setattr(explain, "chat_json", lambda *a, **k: {
            "summary": "Strong short with a 65% win rate.", "verdict_in_words": "Take it."})
        out = explain.summarize(CARD)
        assert out["source"] == "template" and "65" in out["note"]

    def test_grounded_text_is_used(self, monkeypatch):
        monkeypatch.setattr(explain, "chat_json", lambda *a, **k: {
            "summary": "ORB short below 1,183.00; stop 1,192.30. Full backtest: -0.18R over 2,087 trades.",
            "verdict_in_words": "The evidence does not support taking it."})
        monkeypatch.setattr(explain, "chat_model", lambda: "gpt-5")
        assert explain.summarize(CARD)["source"] == "gpt-5"


class TestPostmortem:
    FACTS = {"symbol": "RELIANCE", "side": "buy", "quantity": 177, "entry_time": "10:30", "exit_time": "11:40",
             "minutes_held": 70, "entry_price": 1175.74, "exit_price": 1165.87, "exit_reason": "stop",
             "net_pnl_rupees": -1867.51, "charges_rupees": 120.52, "result_in_R": -1.094, "rules_followed": True}

    def test_template_is_grounded(self):
        t = postmortem._template(self.FACTS)
        text = " ".join([t["summary"], *t["went_right"], *t["went_wrong"]])
        assert ungrounded(text, self.FACTS) == []
        assert t["mistake_tags"] == ["no mistake"]

    def test_tags_are_a_fixed_list(self):
        enum = postmortem.SCHEMA["properties"]["mistake_tags"]["items"]["enum"]
        assert enum == postmortem.MISTAKE_TAGS and "no mistake" in enum


def test_indicator_guide_covers_the_charted_indicators():
    for name in ("EMA", "VWAP", "RSI", "MACD", "Supertrend", "Bollinger Bands", "ATR", "Opening range"):
        assert name in INDICATORS
    assert "Wilder" in as_text()


def test_no_proposals_from_a_tiny_sample(monkeypatch):
    from app.journal import reviews
    saved = {}
    monkeypatch.setattr(reviews, "stats", lambda *a: {"trades": 2, "wins": 1, "losses": 1, "net_pnl_rupees": 10.0,
                                                      "charges_rupees": 1.0, "exit_reasons": {}, "rules_followed_pct": 100.0})
    monkeypatch.setattr(reviews, "chat_json", lambda *a, **k: {"headline": "h", "what_happened": "w", "discipline": "d",
                                                               "patterns": [], "proposals": [{"hypothesis": "x", "strategy_id": "orb",
                                                               "change": "c", "evidence": "e"}]})
    monkeypatch.setattr(reviews, "chat_model", lambda: "gpt-5")
    monkeypatch.setattr(reviews, "_save", lambda u, k, a, b, st, body, m: saved.setdefault("body", body) or {"id": "r"})
    monkeypatch.setattr(reviews, "db", lambda: (_ for _ in ()).throw(AssertionError("no proposal may be stored")))
    import datetime as dt
    reviews.weekly("u", dt.date(2026, 10, 23))
    assert saved["body"]["proposals"] == [] and "too few" in saved["body"]["proposals_note"]
