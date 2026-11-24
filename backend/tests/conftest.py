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
