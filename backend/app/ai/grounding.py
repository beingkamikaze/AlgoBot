"""Grounding check: every number the model writes must come from the facts it was given.

The model is told to use only the facts; this is the code that checks it did. A
text that quotes a number not in the facts (a made-up win rate, a wrong price) is
rejected and the caller falls back to the deterministic text.
"""

from __future__ import annotations

import json
import re

_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        raw = m.group(0).replace(",", "")
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def _variants(x: float) -> set[float]:
    """A fact can be quoted rounded, as a percentage of a fraction, or without its sign."""
    vs = {x, abs(x), -abs(x)}           # "went 1.07R against you" may be written as -1.07R
    for v in list(vs):
        vs |= {round(v, 0), round(v, 1), round(v, 2)}
        if abs(v) <= 1:
            vs |= {round(v * 100, 0), round(v * 100, 1), round(v * 100, 2)}
    return vs


def allowed_numbers(facts) -> set[float]:
    text = facts if isinstance(facts, str) else json.dumps(facts, default=str, ensure_ascii=False)
    allowed: set[float] = set()
    for x in numbers(text):
        allowed |= _variants(x)
    for t in re.findall(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text):   # 10:15, and 16:08:26 inside ISO timestamps
        allowed |= {float(x) for x in t if x}
    return allowed


def ungrounded(text: str, facts, small_ok: int = 3) -> list[float]:
    """Numbers in `text` that are not in `facts`. Tiny counting numbers (1..small_ok) are allowed."""
    allowed = allowed_numbers(facts)
    bad = []
    for x in numbers(text):
        if abs(x) <= small_ok and float(x).is_integer():
            continue
        if not any(abs(x - a) < 0.006 for a in allowed):
            bad.append(x)
    return bad
