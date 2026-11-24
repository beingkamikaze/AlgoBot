"""GPT-5's plain-language summary for a signal card (guide section 3.12).

The model gets the card's facts and nothing else, and must not add a reason. The
grounding check rejects any number it invents; the card then keeps its template.
"""

from __future__ import annotations

import json

from app.ai.client import AIError, AIUnavailable, chat_json, chat_model, strict_object
from app.ai.grounding import ungrounded

SCHEMA = strict_object({
    "summary": {"type": "string", "description": "at most 3 sentences and 60 words, readable in five seconds"},
    "verdict_in_words": {"type": "string", "description": "one sentence, at most 25 words"},
})

SYSTEM = (
    "You explain one trading signal to an Indian retail trader in plain English. Use ONLY the facts in the JSON: "
    "the rule that fired, its conditions, the plan, the evidence after costs, similar trades and the risks. Do not "
    "add reasons, predictions or news that are not in the facts. Every number you write must appear in the facts. "
    "If the evidence is negative or missing, say so plainly. Never promise profit. "
    "Style: plain words, never JSON field names. Mention only what matters most: what fired, the entry, stop and "
    "target, and the one or two key evidence numbers (average R after costs and the number of trades). Round R to "
    "2 decimals and rates to whole percent. The card already lists every detail; do not repeat them all. "
    "Evidence rule: always state the FULL backtest's average R and trade count first; a held-back or regime number "
    "may follow, never replace it. When the full backtest's average R is zero or negative, or confidence is 'low' or "
    "'no track record', the verdict must say the evidence does not support taking the trade; never encourage it."
)


def facts(card: dict) -> dict:
    e = card.get("explanation") or {}
    return {
        "symbol": card.get("symbol"), "side": card.get("side"), "strategy": card.get("strategy_id"),
        "signal_time": card.get("created_at"), "rule": e.get("rule"), "conditions": e.get("conditions"),
        "plan": e.get("plan"), "evidence_after_costs": e.get("evidence"), "similar_setups": e.get("similar"),
        "risks": e.get("risks"), "invalidation": e.get("invalidation"), "confidence": e.get("confidence"),
    }


def summarize(card: dict, user_id: str | None = None) -> dict:
    f = facts(card)
    fallback = (card.get("explanation") or {}).get("summary")
    try:
        out = chat_json(SYSTEM, json.dumps(f, default=str, ensure_ascii=False), SCHEMA, name="signal_summary", purpose="summary",
                        user_id=user_id)
    except AIUnavailable as e:
        return {"summary": fallback, "verdict_in_words": None, "source": "template", "note": str(e)}
    except AIError as e:
        return {"summary": fallback, "verdict_in_words": None, "source": "template", "note": f"model failed: {e}"}
    bad = ungrounded(out["summary"] + " " + out["verdict_in_words"], f)
    if bad:
        return {"summary": fallback, "verdict_in_words": None, "source": "template",
                "note": f"the model quoted numbers that are not in the facts ({', '.join(str(x) for x in bad[:3])}); kept the template"}
    return {**out, "source": chat_model()}
