"""The two GPT-5 agents and the code checks between them.

  strategist: reads the snapshot and proposes one trade (side, stop, target, why) or passes. Passing is the normal answer.
  rules:      plain code. The stop must be on the right side, 0.8 to 4 typical 5-minute moves away; reward at least 1.5x
              risk; every number in the reasons must come from the facts. A failure vetoes the idea.
  reviewer:   argues against the idea and can veto it. It can never approve: only you can, on the ticket.

Neither agent sizes a trade, sees your account, or places anything. The risk engine sizes the ticket from your max loss and
can block it; you confirm it.
"""

from __future__ import annotations

import json

from app.ai.client import chat_json, strict_object
from app.ai.grounding import ungrounded

SETUPS = ["breakout", "pullback", "reversal", "range edge", "other"]
MIN_REWARD_RISK = 1.5
# numbers that name how the facts were measured, not prices: "last 12 bars", "EMA 20 / 50", "RSI 14", "5m / 15m / 1h"
DEFINITIONS = {"recent_bars": 12, "ema": [20, 50], "rsi": 14, "atr": 14, "timeframes": ["1m", "5m", "15m", "1h"],
               "timeframe_minutes": [1, 5, 15, 60]}
STOP_ATR = (0.8, 4.0)

STRATEGIST = """You are the strategist on a paper-trading desk for gold (XAUUSD). You are shown a snapshot of the live market
and either propose ONE intraday trade or pass.

Rules:
- Passing is the normal answer. Most looks should pass. Propose a trade only when the facts show a clear setup AND a clear
  price where the idea is proven wrong.
- Use only the facts given. Every number you write must appear in the facts, except the stop and target you choose.
- Put the stop where the idea is proven wrong, beyond a level from the facts, between 1 and 3 typical 5-minute moves
  (typical_5m_move) from the last price.
- The target must be at least 1.5 times as far from the last price as the stop, and reachable before the flat-by time.
- Be careful from 30 minutes before to 15 minutes after a US release in the facts; say so if it affects the idea.
- If the prices are COMEX futures about 10 minutes late, a fast setup may already be gone: prefer to pass.
- You never choose the size and never place orders. Code sizes every trade from the user's max loss, and the user decides.
- Write for someone who is not an expert: short plain sentences, and explain any trading word you use.

Answer with: decision ("trade" or "pass"); side ("buy", "sell", or "none" when passing); setup (one of: breakout, pullback,
reversal, range edge, other; "other" when passing); stop and target (prices; 0 when passing); why (2 to 5 short reasons,
each tied to a fact); wrong_if (what would show the idea is wrong; "" when passing); pass_reason (why there is no trade now;
"" when trading)."""

REVIEWER = """You are the risk reviewer on a paper-trading desk for gold (XAUUSD). The strategist has proposed a trade. Your job
is to find reasons NOT to take it. You cannot approve anything: only the user can.

Look for: chasing a move that has already run (price far from where the setup started); a stop inside ordinary noise; a
target that needs more time than is left before flat-by; a US release about to come out; going against the 1h trend without a
good reason; reasons that the facts do not support; late futures prices that make the setup stale.

Verdict: "ok" when you found no serious problem, "caution" when there are problems the user should weigh, "reject" when there
is a clear reason it should not be taken. Use only numbers from the facts and the proposal. Write plain short sentences.

Answer with: verdict, concerns (0 to 4 short points), summary (one sentence for the user)."""

_num = {"type": "number"}
_txt = {"type": "string"}
_list = {"type": "array", "items": _txt}
STRATEGIST_SCHEMA = strict_object({
    "decision": {"type": "string", "enum": ["trade", "pass"]},
    "side": {"type": "string", "enum": ["buy", "sell", "none"]},
    "setup": {"type": "string", "enum": SETUPS},
    "stop": _num, "target": _num, "why": _list, "wrong_if": _txt, "pass_reason": _txt,
})
REVIEWER_SCHEMA = strict_object({
    "verdict": {"type": "string", "enum": ["ok", "caution", "reject"]},
    "concerns": _list, "summary": _txt,
})


def _dump(x) -> str:
    return json.dumps(x, default=str, ensure_ascii=False)


def strategist(facts: dict, call=chat_json) -> dict:
    return call(STRATEGIST, _dump(facts), STRATEGIST_SCHEMA, name="global_agent_strategist", purpose="global_agent",
                effort="medium", max_tokens=8000)


def reviewer(facts: dict, proposal: dict, call=chat_json) -> dict:
    return call(REVIEWER, _dump({"facts": facts, "proposal": proposal}), REVIEWER_SCHEMA, name="global_agent_reviewer",
                purpose="global_agent", effort="low", max_tokens=5000)


def plan(facts: dict, p: dict) -> dict:
    """The idea's numbers, measured from the last price the agent saw."""
    price = facts["price"]["last"]
    risk = abs(price - p["stop"])
    reward = abs(p["target"] - price)
    return {"entry": price, "stop": round(p["stop"], 2), "target": round(p["target"], 2), "risk": round(risk, 2),
            "reward": round(reward, 2), "reward_risk": round(reward / risk, 2) if risk else None}


def rule_check(facts: dict, p: dict) -> list[str]:
    """Plain-code checks on a proposed trade. Each failure is a reason in plain words; empty means it passes."""
    price = facts["price"]["last"]
    side, stop, target = p["side"], p["stop"], p["target"]
    if side not in ("buy", "sell"):
        return ["a trade must be a buy or a sell"]
    long = side == "buy"
    bad: list[str] = []
    if not (stop < price < target if long else target < price < stop):
        bad.append(f"for a {side} the stop must be {'below' if long else 'above'} the price and the target "
                   f"{'above' if long else 'below'} it")
        return bad
    n = plan(facts, p)
    atr = facts.get("typical_5m_move")
    if atr:
        lo, hi = STOP_ATR
        if n["risk"] < lo * atr:
            bad.append(f"the stop is {n['risk']:.2f} away, inside ordinary noise (a typical 5-minute move is {atr:.2f})")
        elif n["risk"] > hi * atr:
            bad.append(f"the stop is {n['risk']:.2f} away, more than {hi:g} typical 5-minute moves ({atr:.2f})")
    if n["reward_risk"] is not None and n["reward_risk"] < MIN_REWARD_RISK:
        bad.append(f"reward is only {n['reward_risk']:.2f} times the risk; at least {MIN_REWARD_RISK:g} is needed")
    allowed = {"facts": facts, "plan": n, "definitions": DEFINITIONS}
    wrong = ungrounded(" ".join([*p["why"], p["wrong_if"]]), allowed)
    if wrong:
        bad.append("the reasons quote numbers that are not in the facts: " + ", ".join(f"{x:g}" for x in wrong[:4]))
    return bad
