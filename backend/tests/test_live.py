"""Live trading against a fake Zerodha and an in-memory database: the order lifecycle, every gate, and the
rule that a live position is never left without a stop."""

import datetime as dt

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.core.calendar import IST
from app.live import gates, service, session
from app.paper import service as paper

from .fakes import FakeDB, FakeKite

UID = "11111111-1111-1111-1111-111111111111"
NOW = dt.datetime(2026, 10, 27, 10, 30, tzinfo=IST)          # a Tuesday, market open
KEY = Fernet.generate_key().decode()


def settings(**kw):
    base = dict(supabase_url="http://x", supabase_anon_key="a", supabase_service_role_key="s", live_trading_enabled=True,
                kite_api_key="k", kite_api_secret="sec", token_encryption_key=KEY)
    return Settings(**{**base, **kw})


@pytest.fixture
def env(monkeypatch):
    fdb = FakeDB()
    kite = FakeKite({"NSE:RELIANCE": 1190.0, "NFO:NIFTY26OCTFUT": 25800.0}, lot_sizes={"NIFTY26OCTFUT": 65}, funds=200000)
    for mod in (service, gates, session, paper):
        monkeypatch.setattr(mod, "db", lambda: fdb)
    for mod in (session, gates):
        monkeypatch.setattr(mod, "get_settings", lambda: settings())
    monkeypatch.setattr(session, "_factory", lambda *a: kite)
    monkeypatch.setattr(session, "_brokers", {})
    monkeypatch.setattr(service, "_instruments", {})
    monkeypatch.setattr(service, "_last_diffs", {})
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setattr(paper, "_now", lambda: NOW)
    from app.journal import postmortem
    monkeypatch.setattr(postmortem, "schedule", lambda *a, **k: None)
    fdb.insert("risk_limits", {"user_id": UID, "max_loss_per_trade": 2000, "live_max_loss_per_trade": 500,
                               "live_max_daily_loss": 1500, "live_max_trades_per_day": 3, "live_max_order_value": 50000,
                               "live_armed_on": None})
    fdb.insert("broker_accounts", {"user_id": UID, "broker": "paper", "label": "Paper", "starting_capital": 1000000})
    session.store_token(UID, "access-token-xyz", "AB1234", NOW - dt.timedelta(hours=2))
    return fdb, kite


def ticket(fdb, symbol="RELIANCE", side="buy", stop=1178.0, target=1214.0, kind="equity", lot=1, app_symbol=None):
    return fdb.insert("orders", {"user_id": UID, "account_id": "paper", "mode": "paper", "state": "proposed", "params": {
        "symbol": symbol, "app_symbol": app_symbol or symbol, "data_symbol": symbol, "kind": kind, "lot_size": lot,
        "margin_pct": 100.0 if kind == "equity" else 15.0, "side": side, "qty": 50, "type": "LIMIT", "limit": 1191.2,
        "stop": stop, "target": target, "leg": "entry", "source": "manual", "strategy": "orb", "strategy_version": "1"},
        "risk_verdict": {"verdict": "PASS"}})[0]


def arm(fdb):
    fdb.update("risk_limits", {"user_id": f"eq.{UID}"}, {"live_armed_on": NOW.date().isoformat()})


def go_live(fdb, kite, **kw):
    """ticket -> preview -> approve -> entry fill -> sync: an open live trade with its stop."""
    arm(fdb)
    o = ticket(fdb, **kw)
    pv = service.preview(UID, o["id"], NOW)
    service.approve(UID, o["id"], pv["qty"], "aal2", NOW)
    entry = kite.by_type("LIMIT")[0]
    kite.fill(entry.order_id, 1191.0)
    service.sync(UID, NOW)
    return o, pv, fdb.select("trades", {"mode": "eq.live"})[0]


class TestToken:
    def test_encrypted_and_expires_at_six(self, env):
        fdb, _ = env
        row = fdb.select("broker_accounts", {"broker": "eq.zerodha"})[0]
        assert "access-token-xyz" not in row["access_token_enc"]
        assert session.decrypt(row["access_token_enc"]) == "access-token-xyz"
        assert session.expiry_after(dt.datetime(2026, 10, 27, 8, 40, tzinfo=IST)) == dt.datetime(2026, 10, 28, 6, 0, tzinfo=IST)
        assert session.expiry_after(dt.datetime(2026, 10, 27, 2, 0, tzinfo=IST)) == dt.datetime(2026, 10, 27, 6, 0, tzinfo=IST)

    def test_yesterdays_login_is_not_a_session(self, env):
        assert session.status(UID, NOW)["logged_in"]
        assert not session.status(UID, NOW + dt.timedelta(days=1))["logged_in"]

    def test_login_state_is_single_use(self, env):
        url = session.login_url(UID)
        assert "api_key=k" in url and "redirect_params=state%3D" in url
        with pytest.raises(session.LiveError):
            session.complete_login("not-a-state", "rt")


class TestGates:
    def test_everything_must_hold(self, env, monkeypatch):
        fdb, _ = env
        assert "arm live trading" in " ".join(gates.blocking(gates.check(UID, "aal2", NOW)))
        arm(fdb)
        assert gates.all_ok(gates.check(UID, "aal2", NOW))
        assert not gates.all_ok(gates.check(UID, "aal1", NOW))                                        # no two-factor
        assert not gates.all_ok(gates.check(UID, "aal2", NOW.replace(hour=16)))                      # market closed
        assert not gates.all_ok(gates.check(UID, "aal2", NOW + dt.timedelta(days=1)))                # armed yesterday
        monkeypatch.setattr(gates, "get_settings", lambda: settings(live_trading_enabled=False))
        assert "LIVE_TRADING_ENABLED" in " ".join(gates.blocking(gates.check(UID, "aal2", NOW)))

    def test_arming_needs_two_factor_and_the_word(self, env):
        with pytest.raises(service.LiveOrderError, match="two-factor"):
            service.arm(UID, "aal1", "LIVE", NOW)
        with pytest.raises(service.LiveOrderError, match="type LIVE"):
            service.arm(UID, "aal2", "yes", NOW)
        assert service.arm(UID, "aal2", "live", NOW)["armed_on"] == "2026-10-27"

    def test_approve_refused_when_not_armed(self, env):
        fdb, kite = env
        o = ticket(fdb)
        with pytest.raises(service.LiveOrderError, match="not allowed"):
            service.approve(UID, o["id"], 10, "aal2", NOW)
        assert kite.calls == []


class TestOrders:
    def test_preview_uses_live_limits(self, env):
        fdb, _ = env
        pv = service.preview(UID, ticket(fdb)["id"], NOW)
        assert pv["verdict"]["verdict"] == "PASS", pv["verdict"]
        assert 0 < pv["qty"] < 42                                # ₹500 live max loss, not the ₹2,000 paper one
        assert pv["verdict"]["numbers"]["max_loss"] <= 500
        assert pv["limit"] == 1191.2                             # Kite's price + 0.10%, on the tick

    def test_size_change_needs_a_new_confirmation(self, env):
        fdb, kite = env
        arm(fdb)
        o = ticket(fdb)
        pv = service.preview(UID, o["id"], NOW)
        with pytest.raises(service.LiveOrderError, match="size changed"):
            service.approve(UID, o["id"], pv["qty"] + 1, "aal2", NOW)
        assert kite.calls == []

    def test_entry_is_a_tagged_limit_order(self, env):
        fdb, kite = env
        arm(fdb)
        o = ticket(fdb)
        pv = service.preview(UID, o["id"], NOW)
        out = service.approve(UID, o["id"], pv["qty"], "aal2", NOW)
        assert out["state"] == "working" and out["mode"] == "live" and out["broker_order_id"]
        _, exch, sym, side, qty, typ, price, trig, tag = kite.calls[0]
        assert (exch, sym, side, qty, typ, price, trig) == ("NSE", "RELIANCE", "buy", pv["qty"], "LIMIT", 1191.2, None)
        assert tag == "tb" + o["id"].replace("-", "")[:8] and len(tag) <= 20

    def test_fill_places_the_stop_at_once(self, env):
        fdb, kite = env
        _, pv, t = go_live(fdb, kite)
        stop = kite.by_type("SL")[0]
        assert (stop.side, stop.qty, stop.trigger_price) == ("sell", pv["qty"], 1178.0)
        assert stop.price < stop.trigger_price                   # SL sell: limit below the trigger so it fills
        assert t["entry_context"]["protected"] and t["entry"] == 1191.0

    def test_target_turns_the_stop_into_the_exit(self, env):
        fdb, kite = env
        _, pv, t = go_live(fdb, kite)
        kite.prices["NSE:RELIANCE"] = 1215.0
        service.sync(UID, NOW)
        exits = [o for o in kite.book.values() if o.side == "sell"]
        assert len(exits) == 1 and exits[0].order_type == "LIMIT"      # one exit order, never two sells
        assert exits[0].price < 1215.0
        kite.fill(exits[0].order_id, 1213.5)
        service.sync(UID, NOW)
        t = fdb.select("trades", {"id": f"eq.{t['id']}"})[0]
        assert t["exit_reason"] == "target" and t["exit"] == 1213.5 and t["net_pnl"] > 0

    def test_stop_hit(self, env):
        fdb, kite = env
        _, _, t = go_live(fdb, kite)
        kite.fill(kite.by_type("SL")[0].order_id, 1177.4)
        service.sync(UID, NOW)
        t = fdb.select("trades", {"id": f"eq.{t['id']}"})[0]
        assert t["exit_reason"] == "stop" and t["net_pnl"] < 0 and t["r_multiple"] < -0.9

    def test_refused_stop_means_get_out(self, env):
        fdb, kite = env
        kite.fail_place_type = "SL"
        _, _, t = go_live(fdb, kite)
        t = fdb.select("trades", {"id": f"eq.{t['id']}"})[0]
        assert t["entry_context"]["protected"] is False and t["entry_context"]["exiting"] == "stop could not be placed"
        assert [o.order_type for o in kite.book.values() if o.side == "sell"] == ["LIMIT"]
        assert fdb.select("risk_events", {"kind": "eq.live_unprotected"})

    def test_lost_stop_is_replaced(self, env):
        fdb, kite = env
        go_live(fdb, kite)
        kite.book[kite.by_type("SL")[0].order_id].status = "CANCELLED"        # e.g. cancelled by hand in Kite
        service.sync(UID, NOW)
        assert len([o for o in kite.by_type("SL") if o.status == "TRIGGER PENDING"]) == 1
        assert fdb.select("risk_events", {"kind": "eq.live_stop_lost"})

    def test_flat_by_1515(self, env):
        fdb, kite = env
        go_live(fdb, kite)
        service.sync(UID, NOW.replace(hour=15, minute=15))
        assert kite.by_type("LIMIT")[-1].side == "sell"
        assert fdb.select("trades", {"mode": "eq.live"})[0]["entry_context"]["exiting"] == "flat-by"

    def test_unfilled_exit_is_repriced(self, env):
        fdb, kite = env
        go_live(fdb, kite)
        service.sync(UID, NOW.replace(hour=15, minute=15))
        kite.prices["NSE:RELIANCE"] = 1180.0
        service.sync(UID, NOW.replace(hour=15, minute=16))
        assert kite.calls[-1][0] == "modify" and kite.calls[-1][2] < 1180.0

    def test_rejected_entry(self, env):
        fdb, kite = env
        arm(fdb)
        kite.fail_next = "reject"
        o = ticket(fdb)
        pv = service.preview(UID, o["id"], NOW)
        with pytest.raises(service.LiveOrderError, match="refused"):
            service.approve(UID, o["id"], pv["qty"], "aal2", NOW)
        assert fdb.select("orders", {"id": f"eq.{o['id']}"})[0]["state"] == "rejected"

    def test_unconfirmed_send_is_found_by_tag_never_resent(self, env):
        fdb, kite = env
        arm(fdb)
        kite.fail_next = "ambiguous"
        o = ticket(fdb)
        pv = service.preview(UID, o["id"], NOW)
        assert service.approve(UID, o["id"], pv["qty"], "aal2", NOW)["state"] == "sending"
        service.sync(UID, NOW)
        row = fdb.select("orders", {"id": f"eq.{o['id']}"})[0]
        assert row["state"] == "working" and row["broker_order_id"] == kite.by_type("LIMIT")[0].order_id
        assert sum(1 for c in kite.calls if c[0] == "place") == 1

    def test_stale_entry_cancelled_partial_fill_kept(self, env):
        fdb, kite = env
        arm(fdb)
        o = ticket(fdb)
        pv = service.preview(UID, o["id"], NOW)
        service.approve(UID, o["id"], pv["qty"], "aal2", NOW)
        entry = kite.by_type("LIMIT")[0]
        kite.fill(entry.order_id, 1191.0, qty=5)
        later = NOW + dt.timedelta(minutes=11)
        service.sync(UID, later)
        assert ("cancel", entry.order_id) in kite.calls
        service.sync(UID, later)
        t = fdb.select("trades", {"mode": "eq.live"})[0]
        assert t["qty"] == 5 and kite.by_type("SL")[0].qty == 5


class TestSafety:
    def test_kill_switch(self, env):
        fdb, kite = env
        go_live(fdb, kite)
        r = service.kill_switch(UID)
        assert r["closing_live_positions"] == 1
        assert fdb.select("trades", {"mode": "eq.live"})[0]["entry_context"]["exiting"] == "kill switch"
        assert fdb.select("risk_limits", {})[0]["live_armed_on"] is None
        fdb.insert("risk_events", {"user_id": UID, "kind": "kill_switch", "detail": {}})
        with pytest.raises(service.LiveOrderError, match="kill switch"):
            service.arm(UID, "aal2", "LIVE", NOW)

    def test_squared_off_in_kite_is_closed_here(self, env):
        fdb, kite = env
        _, pv, t = go_live(fdb, kite)
        kite.extra_positions["RELIANCE"] = -pv["qty"]                         # you sold it in the Kite app
        service.sync(UID, NOW)
        t = fdb.select("trades", {"id": f"eq.{t['id']}"})[0]
        assert t["exit_reason"] == "closed outside the app"
        assert fdb.select("risk_events", {"kind": "eq.reconcile_mismatch"})

    def test_unknown_position_is_an_alert_not_a_trade(self, env):
        fdb, kite = env
        kite.extra_positions["TCS"] = 10
        assert service.reconcile(UID, kite, NOW) == [{"symbol": "TCS", "app": 0, "kite": 10}]
        service.reconcile(UID, kite, NOW)
        assert len(fdb.select("risk_events", {"kind": "eq.reconcile_mismatch"})) == 1           # once, not every pass
        assert kite.calls == []

    def test_options_stay_paper(self, env):
        fdb, _ = env
        o = ticket(fdb, kind="option")
        with pytest.raises(service.LiveOrderError, match="paper only"):
            service.preview(UID, o["id"], NOW)

    def test_futures_lot_size_checked_against_kite(self, env):
        fdb, kite = env
        kite.lot_sizes["NIFTY26OCTFUT"] = 75
        fdb.update("risk_limits", {"user_id": f"eq.{UID}"}, {"live_max_loss_per_trade": 50000, "live_max_order_value": 2000000})
        o = ticket(fdb, symbol="NIFTY26OCTFUT", app_symbol="NIFTY-FUT", kind="future", lot=65, stop=25750.0, target=25900.0)
        pv = service.preview(UID, o["id"], NOW)
        assert pv["verdict"]["verdict"] == "BLOCK" and any("lot size changed" in r for r in pv["verdict"]["reasons"])

    def test_futures_one_lot_too_big_for_live_limit(self, env):
        fdb, _ = env
        o = ticket(fdb, symbol="NIFTY26OCTFUT", app_symbol="NIFTY-FUT", kind="future", lot=65, stop=25750.0, target=25900.0)
        pv = service.preview(UID, o["id"], NOW)
        assert pv["qty"] == 0 and "does not fit" in pv["verdict"]["reasons"][0]

    def test_paper_close_refuses_live_positions(self, env):
        fdb, kite = env
        _, _, t = go_live(fdb, kite)
        with pytest.raises(paper.PaperError, match="live position"):
            paper.close_position(UID, t["id"])

    def test_live_limits_never_exceed_paper_ones(self):
        lim = service.live_limits({"max_loss_per_trade": 300, "live_max_loss_per_trade": 500, "max_trades_per_day": 2,
                                   "live_max_trades_per_day": 3, "allow_naked_short_options": True})
        assert lim.max_loss_per_trade == 300 and lim.max_trades_per_day == 2 and not lim.allow_naked_short_options


class TestServerAuth:
    def test_require_auth_protects_every_route_but_the_public_ones(self, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main
        monkeypatch.setattr(main, "get_settings", lambda: settings(require_auth=True))
        c = TestClient(main.create_app())
        assert c.get("/api/strategies").status_code == 401
        assert c.post("/api/backtests", json={"strategy": "orb"}).status_code == 401
        r = c.get("/api/live/kite/callback?status=cancelled", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/live?kite=cancelled")
        assert c.post("/api/webhooks/tradingview/x", json={}).status_code != 401


class TestDatabaseConnection:
    def test_reads_retry_a_dropped_connection_inserts_do_not(self, monkeypatch):
        import httpx
        from app import db as dbmod
        d = dbmod.DB.__new__(dbmod.DB)
        d.base = "http://x/rest/v1"
        calls = []

        class Client:
            def request(self, method, url, **kw):
                calls.append(method)
                if len([c for c in calls if c == method]) == 1:
                    raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
                return httpx.Response(200, json=[{"ok": 1}], request=httpx.Request(method, url))
        d.client = Client()
        assert d.select("t") == [{"ok": 1}] and calls == ["GET", "GET"]
        with pytest.raises(dbmod.DBError, match="connection to Supabase failed"):
            d.insert("t", {"a": 1})
        assert calls.count("POST") == 1                                   # never resent: no duplicate rows

    def test_database_errors_reach_the_browser_as_503(self, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main
        from app.api import live as live_api
        from app.auth import User, current_user
        a = main.create_app()
        a.dependency_overrides[current_user] = lambda: User(id=UID, email="t@x")
        monkeypatch.setattr(live_api.service, "overview", lambda *_: (_ for _ in ()).throw(__import__("app.db", fromlist=["DBError"]).DBError("boom")))
        r = TestClient(a).get("/api/live", headers={"Origin": "http://localhost:3100"})
        assert r.status_code == 503 and r.headers.get("access-control-allow-origin") == "http://localhost:3100"


class TestTickets:
    def test_same_ticket_twice_returns_the_waiting_one_and_old_ones_expire(self, env, monkeypatch):
        fdb, _ = env
        monkeypatch.setattr(paper, "last_price", lambda s: 1190.0)
        a = paper.propose(UID, None, symbol="RELIANCE", side="buy", stop=1178.0, target=1214.0)
        b = paper.propose(UID, None, symbol="RELIANCE", side="buy", stop=1178.0, target=1214.0)
        assert a["id"] == b["id"] and len(fdb.select("orders", {"state": "eq.proposed"})) == 1
        c = paper.propose(UID, None, symbol="RELIANCE", side="buy", stop=1170.0, target=1214.0)
        assert c["id"] != a["id"]
        fdb.update("orders", {"id": f"eq.{a['id']}"}, {"created_at": "2026-10-27T04:00:00+00:00"})
        assert paper.expire_stale_proposals(NOW) == 1
        assert fdb.select("orders", {"id": f"eq.{a['id']}"})[0]["state"] == "expired"
        assert fdb.select("orders", {"id": f"eq.{c['id']}"})[0]["state"] == "proposed"


class TestPaperOverride:
    def ticket(self, fdb, monkeypatch, **kw):
        monkeypatch.setattr(paper, "last_price", lambda s: 1190.0)
        fdb.update("risk_limits", {"user_id": f"eq.{UID}"}, {"max_loss_per_trade": 100})       # every RELIANCE ticket is over it
        return paper.propose(UID, None, symbol="RELIANCE", side="buy", stop=1178.0, target=1214.0, qty=50, **kw)

    def test_your_limits_can_be_overridden_on_paper(self, env, monkeypatch):
        fdb, _ = env
        o = self.ticket(fdb, monkeypatch)
        assert o["risk_verdict"]["verdict"] == "BLOCK" and o["risk_verdict"]["overridable"] and not o["risk_verdict"]["hard"]
        out = paper.approve(UID, None, o["id"], override=True, reason="testing a bigger size")
        assert out["state"] == "working" and out["risk_verdict"]["overridden"]
        assert out["params"]["override"]["reasons"] and out["params"]["override"]["note"] == "testing a bigger size"
        assert any(a["action"] == "risk_override" for a in fdb.select("audit_log", {}))

    def test_without_override_a_block_is_still_rejected(self, env, monkeypatch):
        fdb, _ = env
        o = self.ticket(fdb, monkeypatch)
        assert paper.approve(UID, None, o["id"])["state"] == "rejected"

    def test_hard_blocks_are_never_lifted(self, env, monkeypatch):
        fdb, _ = env
        o = self.ticket(fdb, monkeypatch)
        monkeypatch.setattr(paper, "_now", lambda: NOW.replace(hour=16))                     # market closed
        with pytest.raises(paper.PaperError, match="cannot take this even on paper: market closed"):
            paper.approve(UID, None, o["id"], override=True)
        assert fdb.select("orders", {"id": f"eq.{o['id']}"})[0]["state"] == "proposed"

    def test_hard_reasons_are_tagged(self):
        from app.risk.engine import is_hard
        assert is_hard("market closed (weekend)") and is_hard("the kill switch is on for today")
        assert is_hard("the last price 50 is already beyond the stop 52")
        assert not is_hard("max loss ₹13,846 (incl. ₹95 charges) is over your ₹2,000 per trade")
        assert not is_hard("today's loss ₹2,152 + open risk ₹0 + this position ₹13,846 would be over your daily limit ₹15,000")


def managed(fdb, kite, monkeypatch, **rules):
    """go_live with trade-management rules on the ticket (confirmed with the entry)."""
    orig = ticket

    def with_rules(fdb_, **kw):
        o = orig(fdb_, **kw)
        p = {**o["params"], "manage": {"rules": {"breakeven_at_r": 0.0, "trail_atr": 0.0, "partial_at_r": 0.0,
                                                 "partial_pct": 50, "time_stop_minutes": 0, **rules}, "atr": 4.0}}
        return fdb_.update("orders", {"id": f"eq.{o['id']}"}, {"params": p})[0]
    monkeypatch.setitem(globals(), "ticket", with_rules)
    return go_live(fdb, kite)


def at(seconds):
    return NOW + dt.timedelta(seconds=seconds)


def ctx_of(fdb):
    return fdb.select("trades", {"mode": "eq.live"})[0]["entry_context"]


class TestManagement:
    def test_preview_shows_the_rules_you_confirm(self, env, monkeypatch):
        fdb, kite = env
        o = ticket(fdb)
        fdb.update("orders", {"id": f"eq.{o['id']}"}, {"params": {**o["params"], "manage": {"rules": {"breakeven_at_r": 1.0}}}})
        assert "stop to entry once at +1R" in service.preview(UID, o["id"], NOW)["management"]
        assert service.preview(UID, ticket(fdb)["id"], NOW)["management"] is None

    def test_breakeven_modifies_the_resting_stop(self, env, monkeypatch):
        fdb, kite = env
        _, pv, t = managed(fdb, kite, monkeypatch, breakeven_at_r=1.0)
        sl = kite.by_type("SL")[0]
        kite.prices["NSE:RELIANCE"] = 1205.0                      # +1.08R inside the 10:30 minute
        service.sync(UID, at(10))
        assert sl.trigger_price == 1178.0                        # a bar moves the stop only once it has finished
        kite.prices["NSE:RELIANCE"] = 1203.0
        service.sync(UID, at(61))
        assert len(kite.by_type("SL")) == 1 and sl.status == "TRIGGER PENDING"       # modified, never re-placed
        assert (sl.trigger_price, sl.qty) == (1191.0, pv["qty"]) and sl.price < 1191.0
        ctx = ctx_of(fdb)
        assert ctx["stop"] == 1191.0 and ctx["initial_stop"] == 1178.0
        assert [(m["from"], m["to"]) for m in ctx["manage"]["moves"]] == [(1178.0, 1191.0)]
        assert "breakeven" in ctx["manage"]["moves"][0]["why"]
        assert fdb.select("audit_log", {"action": "eq.live_stop_moved"})
        kite.fill(sl.order_id, 1190.9)
        service.sync(UID, at(70))
        t = fdb.select("trades", {"id": f"eq.{t['id']}"})[0]
        assert t["exit_reason"] == "stop" and t["exit"] == 1190.9

    def test_refused_modify_keeps_the_old_stop_and_retries(self, env, monkeypatch):
        fdb, kite = env
        managed(fdb, kite, monkeypatch, breakeven_at_r=1.0)
        sl = kite.by_type("SL")[0]
        kite.prices["NSE:RELIANCE"] = 1205.0
        service.sync(UID, at(10))
        kite.fail_modify = True
        service.sync(UID, at(61))
        assert sl.trigger_price == 1178.0 and sl.status == "TRIGGER PENDING"         # still protected
        assert fdb.select("risk_events", {"kind": "eq.live_stop_move_failed"})
        service.sync(UID, at(65))
        assert sl.trigger_price == 1191.0

    def test_moved_stop_already_crossed_exits(self, env, monkeypatch):
        fdb, kite = env
        managed(fdb, kite, monkeypatch, trail_atr=1.0)            # trail 4.00 behind the best price from the start
        kite.prices["NSE:RELIANCE"] = 1200.0
        service.sync(UID, at(10))
        kite.prices["NSE:RELIANCE"] = 1195.0                      # the new stop (1196) is above the price already
        service.sync(UID, at(61))
        sells = [o for o in kite.book.values() if o.side == "sell"]
        assert len(sells) == 1 and sells[0].order_type == "LIMIT" and ctx_of(fdb)["exiting"] == "stop"

    def test_trail_follows_the_best_price(self, env, monkeypatch):
        fdb, kite = env
        managed(fdb, kite, monkeypatch, trail_atr=2.0)            # 8.00 behind the best price
        sl = kite.by_type("SL")[0]
        for i, px in enumerate([1200.0, 1199.0, 1210.0, 1206.0]):
            kite.prices["NSE:RELIANCE"] = px
            service.sync(UID, at(10 + 60 * i))
        assert sl.trigger_price == 1202.0                         # best 1210 (finished minute) - 8
        assert [m["to"] for m in ctx_of(fdb)["manage"]["moves"]] == [1192.0, 1202.0]

    def test_partial_shrinks_the_stop_first_then_books(self, env, monkeypatch):
        fdb, kite = env
        _, pv, t = managed(fdb, kite, monkeypatch, partial_at_r=1.0, partial_pct=50)
        qty, half = pv["qty"], pv["qty"] // 2
        sl = kite.by_type("SL")[0]
        kite.prices["NSE:RELIANCE"] = 1205.0                      # through the +1R level (1204)
        service.sync(UID, at(10))
        part = [o for o in kite.by_type("LIMIT") if o.side == "sell"]
        assert len(part) == 1 and part[0].qty == half and sl.qty == qty - half
        mods = [c for c in kite.calls if c[0] == "modify"]
        places = [c for c in kite.calls if c[0] == "place"]
        assert kite.calls.index(mods[0]) < kite.calls.index(places[-1])                # stop shrunk before the sell
        assert sum(o.qty for o in kite.book.values() if o.side == "sell" and o.is_open) == qty   # never more than held
        assert service.reconcile(UID, kite, at(10)) == []
        service.sync(UID, at(15))
        assert len([o for o in kite.by_type("LIMIT") if o.side == "sell"]) == 1        # not sent twice
        kite.fill(part[0].order_id, 1204.6)
        service.sync(UID, at(20))
        ctx = ctx_of(fdb)
        assert ctx["manage"]["partial"]["qty"] == half and ctx["manage"]["partial"]["price"] == 1204.6
        assert "partial_order" not in ctx and sl.qty == qty - half
        assert [f for f in fdb.select("fills", {}) if "partial_exit" in (f["costs"] or {})]
        assert service.reconcile(UID, kite, at(20)) == []
        kite.fill(sl.order_id, 1177.5)
        service.sync(UID, at(30))
        t = fdb.select("trades", {"id": f"eq.{t['id']}"})[0]
        gross = (1204.6 - 1191.0) * half + (1177.5 - 1191.0) * (qty - half)
        assert t["exit_reason"] == "stop" and t["gross_pnl"] == round(gross, 2)

    def test_refused_partial_gives_the_stop_its_quantity_back(self, env, monkeypatch):
        fdb, kite = env
        _, pv, _ = managed(fdb, kite, monkeypatch, partial_at_r=1.0)
        sl = kite.by_type("SL")[0]
        kite.prices["NSE:RELIANCE"] = 1205.0
        kite.fail_next = "reject"
        service.sync(UID, at(10))
        assert sl.qty == pv["qty"] and sl.status == "TRIGGER PENDING"
        assert fdb.select("risk_events", {"kind": "eq.live_partial_failed"})
        service.sync(UID, at(15))
        assert not [o for o in kite.by_type("LIMIT") if o.side == "sell"]             # not retried every pass

    def test_partial_cancelled_at_kite_restores_the_stop(self, env, monkeypatch):
        fdb, kite = env
        _, pv, _ = managed(fdb, kite, monkeypatch, partial_at_r=1.0)
        sl = kite.by_type("SL")[0]
        kite.prices["NSE:RELIANCE"] = 1205.0
        service.sync(UID, at(10))
        part = [o for o in kite.by_type("LIMIT") if o.side == "sell"][0]
        kite.cancel(part.order_id)                                # e.g. cancelled by hand in Kite
        service.sync(UID, at(15))
        assert sl.qty == pv["qty"] and "partial_order" not in ctx_of(fdb)

    def test_time_stop_exits_a_trade_going_nowhere(self, env, monkeypatch):
        fdb, kite = env
        _, _, t = managed(fdb, kite, monkeypatch, time_stop_minutes=5)
        kite.prices["NSE:RELIANCE"] = 1192.0                      # +0.08R
        service.sync(UID, at(270))
        assert "exiting" not in ctx_of(fdb)
        service.sync(UID, at(301))                                # the 10:34 minute closed 5 minutes after entry
        sells = [o for o in kite.book.values() if o.side == "sell"]
        assert len(sells) == 1 and sells[0].order_type == "LIMIT" and ctx_of(fdb)["exiting"] == "time-stop"
        kite.fill(sells[0].order_id, 1191.5)
        service.sync(UID, at(310))
        assert fdb.select("trades", {"id": f"eq.{t['id']}"})[0]["exit_reason"] == "time-stop"

    def test_monitor_alerts_on_a_live_stop_move(self, env, monkeypatch):
        from app.monitor import service as monitor
        fdb, kite = env
        monkeypatch.setattr(monitor, "db", lambda: fdb)
        monkeypatch.setattr(monitor, "_state", {})
        _, _, t = managed(fdb, kite, monkeypatch, breakeven_at_r=1.0)
        monitor._management_alerts(t["id"], UID, "RELIANCE", ctx_of(fdb)["manage"], NOW)
        kite.prices["NSE:RELIANCE"] = 1205.0
        service.sync(UID, at(10))
        service.sync(UID, at(61))
        monitor._management_alerts(t["id"], UID, "RELIANCE", ctx_of(fdb)["manage"], at(61))
        alerts = monitor._state[t["id"]]["alerts"]
        assert [a["kind"] for a in alerts] == ["stop_moved"] and "1,178.00 → 1,191.00" in alerts[0]["title"]

    def test_no_rules_no_management(self, env):
        fdb, kite = env
        _, _, t = go_live(fdb, kite)
        kite.prices["NSE:RELIANCE"] = 1205.0
        service.sync(UID, at(10))
        service.sync(UID, at(61))
        assert kite.by_type("SL")[0].trigger_price == 1178.0 and ctx_of(fdb)["manage"] is None
