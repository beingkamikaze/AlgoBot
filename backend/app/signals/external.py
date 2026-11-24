"""The "why this trade" card for a signal that came from outside the app (a TradingView alert).

The app cannot see the alert's rules, so the card says so plainly. Its evidence is
the app's own record of paper trades from that same alert source; until there is
one, confidence is "no track record" and the card says: paper only.
"""

from __future__ import annotations

import datetime as dt

from app.core.calendar import IST
from app.core.instruments import resolve
from app.db import db, eq
from app.domain.signal import Condition, Evidence, Explanation, Side, Signal, TradePlan, grade_confidence
from app.market import news


def strategy_key(source: dict) -> str:
    return f"tv:{source['name']}"


def evidence_for(user_id: str, key: str) -> list[Evidence]:
    trades = db().select("trades", {"user_id": eq(user_id), "exit_ts": "not.is.null", "entry_context->>strategy": eq(key)},
                         columns="net_pnl,r_multiple")
    if not trades:
        return []
    rs = [float(t["r_multiple"]) for t in trades if t["r_multiple"] is not None]
    wins = sum(1 for t in trades if float(t["net_pnl"] or 0) > 0)
    gains = sum(float(t["net_pnl"]) for t in trades if float(t["net_pnl"] or 0) > 0)
    losses = -sum(float(t["net_pnl"]) for t in trades if float(t["net_pnl"] or 0) <= 0)
    return [Evidence(source="paper", trades=len(trades), expectancy_r=round(sum(rs) / len(rs), 4) if rs else None,
                     profit_factor=round(gains / losses, 3) if losses else None, win_rate=round(wins / len(trades), 4))]


def card_for_alert(source: dict, alert, symbol: str) -> dict:
    inst = resolve(symbol, dt.datetime.now(IST).date())
    long = alert.side == "buy"
    entry = alert.price or alert.stop
    risk = abs(entry - alert.stop)
    key = strategy_key(source)
    evidence = evidence_for(source["user_id"], key)
    rule = f"TradingView alert \"{source['name']}\" ({alert.strategy}): {alert.side.upper()} {symbol}" + \
           (f" at {alert.price:,.2f}" if alert.price else "") + "."
    if alert.note:
        rule += f" Its note: \"{alert.note}\""
    risks = ["This rule lives in TradingView: this app has not backtested it and cannot see its logic.",
             "Prices are Yahoo's free data and can lag; check the broker's price before acting."]
    if (long and alert.stop >= entry) or (not long and alert.stop <= entry):
        risks.insert(0, "The alert's stop is on the wrong side of its price: the risk check will block it.")
    if inst.kind == "equity":
        try:
            risks.extend(news.risks_for(symbol, alert.side))
        except Exception:  # noqa: BLE001
            risks.append("News could not be checked right now.")
    else:
        risks.append(f"Futures are priced from the {inst.data_symbol} index until Kite data is connected.")
    confidence = grade_confidence(evidence)
    rec = evidence[0] if evidence else None
    track = (f"Your paper trades from this alert: {rec.trades}, averaging {rec.expectancy_r:+.2f}R after costs."
             if rec and rec.expectancy_r is not None else "No paper trades from this alert yet: treat it as an experiment.")
    exp = Explanation(
        rule=rule,
        conditions=[Condition(name="Source", met=True, detail=f"TradingView webhook \"{source['name']}\", received {dt.datetime.now(IST):%H:%M} IST"),
                    Condition(name="Stop given", met=True, detail=f"{alert.stop:,.2f} ({risk:,.2f} from the alert price)")],
        plan=TradePlan(entry=round(entry, 2), stop=round(alert.stop, 2), stop_reason="set by the TradingView alert",
                       target=round(alert.target, 2) if alert.target else None,
                       target_reason="set by the TradingView alert" if alert.target else None, exit_by=dt.time(15, 15),
                       instrument=inst.kind, contract=inst.tradingsymbol if inst.kind == "future" else None),
        evidence=evidence, similar=None, risks=risks,
        invalidation=f"The alert's stop at {alert.stop:,.2f}.",
        confidence=confidence,
        summary=f"TradingView alert \"{source['name']}\": {alert.side} {symbol}, stop {alert.stop:,.2f}"
                + (f", target {alert.target:,.2f}" if alert.target else "") + f". {track} Confidence: {confidence.value}.",
    )
    sig = Signal(strategy_id=key, strategy_version="external", symbol=symbol, side=Side.BUY if long else Side.SELL,
                 created_at=dt.datetime.now(IST), explanation=exp)
    return sig.model_dump(mode="json")
