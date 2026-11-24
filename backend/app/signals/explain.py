"""Builds the "why this trade" card (guide section 3.12) from facts only:
the strategy's own reasons, the plan, the backtest evidence for this exact version,
similar past trades, and the risks we can see. The plain-language summary is a
fixed template for now; in Phase 4 GPT-5 rewrites it from these same fields."""

from __future__ import annotations

import dataclasses
import datetime as dt

from app.backtest.engine import BacktestConfig, size
from app.backtest.metrics import time_bucket
from app.backtest.runner import gap_bucket, latest_for, vix_bucket
from app.core import manage
from app.core.calendar import IST
from app.core.instruments import resolve
from app.market import news
from app.domain.signal import (Confidence, Evidence, Explanation, Side, Signal, SimilarTrades, TradePlan,
                               grade_confidence)
from app.strategies.base import Candidate, Frame, Strategy


class _T:   # a tiny stand-in so live signals can use the backtest's time buckets
    def __init__(self, t: int):
        self.entry_t = t


def _evidence(report: dict, vix: str | None) -> list[Evidence]:
    out = []
    s = report.get("summary") or {}
    if s.get("trades"):
        out.append(Evidence(source="backtest", trades=s["trades"], expectancy_r=s.get("expectancy_r"),
                            profit_factor=s.get("profit_factor"), win_rate=s.get("win_rate")))
    o = report.get("out_of_sample") or {}
    if o.get("trades"):
        out.append(Evidence(source="backtest, out-of-sample", trades=o["trades"], expectancy_r=o.get("expectancy_r"),
                            profit_factor=o.get("profit_factor"), win_rate=o.get("win_rate")))
    if vix and (report.get("by_vix") or {}).get(vix):
        r = report["by_vix"][vix]
        out.append(Evidence(source="backtest", regime=vix, trades=r["trades"], expectancy_r=r.get("expectancy_r"),
                            profit_factor=r.get("profit_factor"), win_rate=r.get("win_rate")))
    return out


def _similar(trades: list[dict], side: str, bucket: str, vix: str | None) -> SimilarTrades | None:
    pool = [t for t in trades if t["side"] == side and time_bucket(_T(t["entry_t"])) == bucket
            and (vix is None or t.get("regime", {}).get("vix") == vix)]
    if not pool:
        return None
    wins = sum(1 for t in pool if t["net"] > 0)
    avg = sum(t["r"] for t in pool) / len(pool)
    stops = sum(1 for t in pool if t["exit_reason"] == "stop")
    return SimilarTrades(count=len(pool), wins=wins, losses=len(pool) - wins, average_r=round(avg, 3),
                         note=f"Same side, entry {bucket[3:]}, {vix or 'any VIX'}: {stops} of {len(pool)} hit the stop.")


def _management(strategy: Strategy, f: Frame, cand: Candidate) -> dict | None:
    rules = manage.rules_from(strategy.params)
    if not rules:
        return None
    a = float(f.ind("atr", manage.ATR_PERIOD)[cand.signal_index])
    return {"rules": rules, "atr": None if a != a else round(a, 4), "target_r": cand.target_r,
            "description": manage.describe(rules)}


def build(strategy: Strategy, f: Frame, cand: Candidate, regime: dict, cfg: BacktestConfig | None = None) -> Signal:
    cfg = cfg or BacktestConfig()
    long = cand.side == "buy"
    entry = cand.reference
    risk = entry - cand.stop if long else cand.stop - entry
    target = entry + cand.target_r * risk if long else entry - cand.target_r * risk
    inst = resolve(f.symbol, dt.datetime.fromtimestamp(int(f.t[cand.signal_index]), IST).date())
    if inst.kind == "future" and cfg.fixed_lots is None:
        cfg = dataclasses.replace(cfg, fixed_lots=1)
    qty = size(entry, risk, cfg, inst) if risk > 0 else 0
    exit_value = cand.stop * qty
    charges = inst.charges(entry * qty, exit_value) if long else inst.charges(exit_value, entry * qty)
    unit = "a unit" if inst.kind == "future" else "a share"
    plan = TradePlan(entry=round(entry, 2), stop=round(cand.stop, 2), stop_reason=cand.stop_reason,
                     target=round(target, 2), target_reason=f"{cand.target_r:g}R (risk {risk:,.2f} {unit})",
                     exit_by=dt.time(15, 15), quantity=qty,
                     max_loss_with_charges=round(qty * risk + charges.total, 2),
                     instrument=inst.kind, contract=inst.tradingsymbol if inst.kind == "future" else None,
                     lot_size=inst.lot_size if inst.kind == "future" else None,
                     lots=qty // inst.lot_size if inst.kind == "future" else None,
                     notional=round(entry * qty, 2), margin_estimate=round(inst.margin(entry, qty), 2),
                     management=_management(strategy, f, cand))

    vix = regime.get("vix")
    bt = latest_for(strategy.id, strategy.version_key, futures=inst.kind == "future")
    evidence = _evidence(bt["report"], vix) if bt else []
    bucket = time_bucket(_T(int(f.t[cand.signal_index])))
    similar = _similar(bt["trades"], cand.side, bucket, vix) if bt else None

    risks = ["Prices are Yahoo's free data and can lag; check the broker's price before acting."]
    if vix and "high" in vix:
        risks.append(f"Volatility is high ({vix}): stops get hit more often.")
    gap = regime.get("nifty_gap_pct")
    if gap is not None and ((long and gap < -0.3) or (not long and gap > 0.3)):
        risks.append(f"This trade goes against the market's opening gap (Nifty {gap:+.2f}%).")
    if inst.kind == "future":
        signal_day = dt.datetime.fromtimestamp(int(f.t[cand.signal_index]), IST).date()
        risks.append(f"Priced from the {inst.data_symbol} index until Kite data is connected: the {inst.tradingsymbol} "
                     "contract trades at a premium or discount to it, so real fills will differ.")
        risks.append(f"Leverage: one lot is about ₹{entry * inst.lot_size / 1e5:,.1f} lakh of exposure on roughly "
                     f"₹{inst.margin(entry, inst.lot_size) / 1e5:,.1f} lakh of margin (an estimate until Kite's margin data).")
        if inst.expiry == signal_day:
            risks.append(f"Expiry day for {inst.tradingsymbol}: moves near the close can be sharp.")
        if inst.expiry_note:
            risks.append(inst.expiry_note)
    else:
        try:
            risks.extend(news.risks_for(f.symbol, cand.side))
        except Exception:  # noqa: BLE001 - a news outage must not hide the signal
            risks.append("News could not be checked right now; check results dates before trading.")
    confidence = grade_confidence(evidence)
    if bt is None:
        risks.insert(0, "This exact strategy version has no backtest yet: run one on the Backtests page.")

    head = evidence[0] if evidence else None
    oos = next((e for e in evidence if "out-of-sample" in e.source), None)
    if head is None:
        track = "It has no track record yet, so treat it as an experiment: paper only."
    else:
        track = (f"In {head.trades} backtest trades this exact version averaged {head.expectancy_r:+.2f}R after costs"
                 + (f" ({oos.expectancy_r:+.2f}R on the held-back period)" if oos and oos.expectancy_r is not None else "")
                 + ".")
        if (head.expectancy_r or 0) <= 0:
            track += " That is a losing record: the rule fired, but the evidence says not to take it."
    sim = (f" Similar setups: {similar.wins} won, {similar.losses} lost." if similar else "")
    managed = f" (managed: {plan.management['description']})" if plan.management else ""
    summary = (f"{strategy.name}, {'buy' if long else 'sell'} {f.symbol} near {entry:,.2f} with a stop at "
               f"{cand.stop:,.2f} and a target at {target:,.2f}"
               f"{managed}. {track}{sim} Confidence: {confidence.value}.")

    exp = Explanation(rule=cand.rule, conditions=cand.conditions, plan=plan, evidence=evidence, similar=similar,
                      risks=risks, invalidation=cand.invalidation, confidence=confidence, summary=summary)
    return Signal(strategy_id=strategy.id, strategy_version=strategy.version_key, symbol=f.symbol,
                  side=Side.BUY if long else Side.SELL,
                  created_at=dt.datetime.fromtimestamp(int(f.t[cand.signal_index]) + f.bar_minutes * 60, IST),
                  explanation=exp)


def regime_now(session: str, vix_prev: float | None, nifty_gap: float | None) -> dict:
    return {"vix": vix_bucket(vix_prev), "vix_prev_close": vix_prev, "nifty_gap": gap_bucket(nifty_gap),
            "nifty_gap_pct": round(nifty_gap, 2) if nifty_gap is not None else None, "session": session}


__all__ = ["build", "regime_now", "Confidence"]
