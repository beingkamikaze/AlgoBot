"""The strategies table: one row per strategy in the code, and one `strategy_versions` row per
version (the defaults as "1", and every variant the learning loop creates)."""

from __future__ import annotations

from app.db import db
from app.options.backtest import OPTION_STRATEGIES
from app.strategies.library import STRATEGIES


def all_strategy_classes() -> dict:
    return {**STRATEGIES, **OPTION_STRATEGIES}


def register_strategies() -> None:
    d = db()
    rows = [{"id": c.id, "name": c.name, "description": c.description, "market": c.market}
            for c in all_strategy_classes().values()]
    d.insert("strategies", rows, upsert_on="id")
    d.insert("strategy_versions", [{"strategy_id": c.id, "version": c.version, "params": c.defaults, "status": "paper"}
                                   for c in all_strategy_classes().values()],
             upsert_on="strategy_id,version")
