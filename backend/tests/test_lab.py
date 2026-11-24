import random

import pytest

from app.lab import loop
from app.webhooks import tradingview as tv


def trades(n_sessions: int, per_day: int, mean_r: float, seed: int, sd: float = 1.0) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for d in range(n_sessions):
        for _ in range(per_day):
            out.append({"session": f"2026-08-{d + 1:02d}" if d < 31 else f"2026-09-{d - 30:02d}", "r": rng.gauss(mean_r, sd)})
    return out


class TestEvaluate:
    def test_clear_improvement_passes(self):
        v = loop.evaluate(trades(50, 10, -0.10, 1), trades(50, 10, 0.35, 2), prior_tests=0)   # 150 held-back trades each
        assert v["passed"], v["reasons"]
        assert v["candidate"]["held_back_trades"] >= 30

    def test_still_losing_fails_even_if_better(self):
        v = loop.evaluate(trades(50, 4, -0.40, 1), trades(50, 4, -0.10, 2), prior_tests=0)
        assert not v["passed"] and not v["checks"]["positive_after_costs"]

    def test_too_few_held_back_trades(self):
        v = loop.evaluate(trades(30, 1, -0.10, 1), trades(30, 1, 0.80, 2), prior_tests=0)
        assert not v["checks"]["enough_held_back_trades"]

    def test_more_tests_need_more_evidence(self):
        one = loop.evaluate(trades(50, 3, 0.0, 1), trades(50, 3, 0.15, 2, sd=1.2), prior_tests=0)
        many = loop.evaluate(trades(50, 3, 0.0, 1), trades(50, 3, 0.15, 2, sd=1.2), prior_tests=19)
        assert many["bootstrap_needed"] > one["bootstrap_needed"]
        assert many["bootstrap_needed"] == pytest.approx(1 - 0.10 / 20)

    def test_only_the_held_back_days_decide(self):
        base = trades(50, 4, 0.0, 1)
        cand = [dict(t, r=t["r"] + (3.0 if t["session"] < "2026-09-05" else -0.5)) for t in trades(50, 4, 0.0, 2)]
        v = loop.evaluate(base, cand, 0)
        assert not v["passed"] and v["candidate"]["in_sample"] > v["baseline"]["in_sample"]


class TestValidateChange:
    def test_real_parameters_only(self):
        assert loop.validate_change("orb", {"min_range_pct": 0.5}) == {"min_range_pct": 0.5}
        with pytest.raises(loop.LabError):
            loop.validate_change("orb", {"magic": 1})
        with pytest.raises(loop.LabError):
            loop.validate_change("orb", {"allow_short": 1})          # a flag needs true/false
        with pytest.raises(loop.LabError):
            loop.validate_change("orb", {"or_minutes": 15})          # equals the default: nothing to test
        assert loop.validate_change("orb", {"or_minutes": 30.0}) == {"or_minutes": 30}


class TestWebhookHelpers:
    @pytest.mark.parametrize("tv_ticker, ours", [("NSE:RELIANCE", "RELIANCE"), ("RELIANCE", "RELIANCE"), ("NSE:M&M", "M&M"),
                                                 ("NIFTY1!", "NIFTY-FUT"), ("NSE:BANKNIFTY1!", "BANKNIFTY-FUT"),
                                                 ("NIFTY", "NIFTY-FUT"), ("TCS.NS", "TCS")])
    def test_symbols(self, tv_ticker, ours):
        assert tv.app_symbol(tv_ticker) == ours

    def test_bad_symbol(self):
        with pytest.raises(tv.WebhookError):
            tv.app_symbol("NASDAQ:BTC/USD")

    def test_ip_from_tunnel_headers(self):
        assert tv.client_ip({"cf-connecting-ip": "52.89.214.238"}, "127.0.0.1") == "52.89.214.238"
        assert tv.client_ip({"x-forwarded-for": "34.212.75.30, 10.0.0.1"}, "127.0.0.1") == "34.212.75.30"
        assert tv.client_ip({}, "127.0.0.1") == "127.0.0.1"
        assert "52.89.214.238" in tv.TV_IPS and len(tv.TV_IPS) == 4

    def test_alert_message(self):
        a = tv.Alert(secret="s" * 20, symbol="NSE:TCS", side="long", stop=2500, price=2530)
        assert a.side == "buy"
        with pytest.raises(Exception):
            tv.Alert(secret="s" * 20, symbol="TCS", side="buy")      # no stop, no ticket

    def test_rate_limit_and_dedupe(self):
        sid = "rate-test"
        assert all(tv._rate_ok(sid) for _ in range(tv.RATE_PER_MINUTE))
        assert not tv._rate_ok(sid)
        assert not tv._dupe("k1") and tv._dupe("k1")

    def test_secret_hash_is_not_the_secret(self):
        assert tv.hash_secret("abc") != "abc" and len(tv.hash_secret("abc")) == 64

    def test_template_has_placeholders(self):
        t = tv.message_template("SECRET123456", "My alert")
        assert "{{ticker}}" in t and "{{close}}" in t and '"{{close}}"' not in t and "SECRET123456" in t

    def test_bad_alert_never_echoes_the_secret(self, monkeypatch):
        logged = []
        monkeypatch.setattr(tv, "db", lambda: type("D", (), {"select": lambda *a, **k: [{"id": "0" * 36, "user_id": "u", "enabled": True,
                                                                                            "secret_hash": tv.hash_secret("topsecret-123")}]})())
        monkeypatch.setattr(tv, "_event", lambda src, ip, body, outcome, detail="", **k: logged.append(detail))
        with pytest.raises(tv.WebhookError) as e:
            tv.receive("0" * 36, {"secret": "topsecret-123", "symbol": "TCS", "side": "buy"}, {"cf-connecting-ip": "52.89.214.238"}, None)
        assert "stop" in str(e.value) and "topsecret" not in str(e.value) and "topsecret" not in logged[0]
