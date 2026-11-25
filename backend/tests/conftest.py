"""Tests never reach the real Kite: no Kite login unless a test installs a fake (tests/fakes.py FakeKiteConnect)."""

import pytest

from app.market import feed, kite, ticks


@pytest.fixture(autouse=True)
def no_real_kite(monkeypatch):
    monkeypatch.setattr(kite, "resolver", lambda: None)
    kite.forget()
    feed.clear_caches()
    monkeypatch.setattr(ticks, "builder", ticks.BarBuilder())
    yield
    kite.forget()


@pytest.fixture(autouse=True)
def no_live_gold_price(monkeypatch):
    """No test asks Twelve Data for a price: a paper close waits for the next bar unless a test supplies a quote."""
    from app.global_markets.data import feed as gold_feed
    monkeypatch.setattr(gold_feed, "quote", lambda symbol, source=None: None)
