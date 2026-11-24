"""The language-model client (GPT-5 on Azure OpenAI, or OpenAI).

Rules the rest of the app relies on:
  - every call returns JSON that matches a strict schema, or raises;
  - the model never sees credentials and never calls the order API;
  - every call is logged in `ai_usage`, and a daily token cap stops runaway costs.
GPT-5 is a reasoning model: it takes no `temperature`, and its reasoning uses the
completion budget, so budgets here are generous and reasoning effort is low.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from typing import Any

from app.config import get_settings

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")   # the daily token cap resets at midnight IST; kept here so
                                                              # the global setup can use the client without NSE's calendar

log = logging.getLogger("ai")
_lock = threading.Lock()
_client = None


class AIError(Exception):
    pass


class AIUnavailable(AIError):
    pass


def _get_client():
    global _client
    s = get_settings()
    if not s.llm_configured:
        raise AIUnavailable("no language model configured: set the Azure OpenAI (or OpenAI) keys in backend/.env")
    with _lock:
        if _client is None:
            if s.llm_provider == "azure":
                from openai import AzureOpenAI
                _client = AzureOpenAI(azure_endpoint=s.azure_openai_endpoint, api_key=s.azure_openai_api_key,
                                      api_version=s.azure_openai_api_version, timeout=120, max_retries=2)
            else:
                from openai import OpenAI
                _client = OpenAI(api_key=s.openai_api_key, timeout=120, max_retries=2)
    return _client


def chat_model() -> str:
    s = get_settings()
    return s.azure_openai_deployment if s.llm_provider == "azure" else s.openai_model


def embedding_model() -> str:
    s = get_settings()
    return s.azure_openai_embedding_deployment if s.llm_provider == "azure" else s.openai_embedding_model


# ---------------------------------------------------------------- usage and the daily cap

def _db():
    try:
        from app.db import db
        return db()
    except Exception:  # noqa: BLE001 - usage logging is best effort when the database is down
        return None


def tokens_used_today() -> int:
    d = _db()
    if d is None:
        return 0
    start = dt.datetime.combine(dt.datetime.now(IST).date(), dt.time(0, 0), IST).astimezone(dt.timezone.utc).isoformat()
    try:
        rows = d.select("ai_usage", {"ts": f"gte.{start}"}, columns="total_tokens")
    except Exception:  # noqa: BLE001 - table missing until migration 0002 runs
        return 0
    return sum(int(r["total_tokens"] or 0) for r in rows)


def _log(user_id: str | None, purpose: str, model: str, usage: Any, ok: bool, detail: dict | None = None) -> None:
    d = _db()
    if d is None:
        return
    try:
        d.insert("ai_usage", {"user_id": user_id, "purpose": purpose, "model": model, "ok": ok,
                              "prompt_tokens": getattr(usage, "prompt_tokens", None),
                              "completion_tokens": getattr(usage, "completion_tokens", None),
                              "total_tokens": getattr(usage, "total_tokens", None), "detail": detail or {}})
    except Exception as e:  # noqa: BLE001
        log.warning("ai_usage log failed: %s", e)


def _check_cap() -> None:
    cap = get_settings().llm_daily_token_cap
    used = tokens_used_today()
    if used >= cap:
        raise AIUnavailable(f"today's AI budget is used up ({used:,} of {cap:,} tokens); it resets at midnight IST")


# ---------------------------------------------------------------- calls

def chat_json(system: str, user: str, schema: dict, *, name: str, purpose: str, user_id: str | None = None,
              effort: str = "low", max_tokens: int = 6000) -> dict:
    """One structured call. `schema` is a strict JSON schema; the reply is parsed and returned."""
    client = _get_client()
    _check_cap()
    model = chat_model()
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            max_completion_tokens=max_tokens,
            reasoning_effort=effort,
        )
    except Exception as e:  # noqa: BLE001 - surface provider errors as AIError
        _log(user_id, purpose, model, None, False, {"error": str(e)[:300]})
        raise AIError(f"the model call failed: {str(e)[:200]}")
    _log(user_id, purpose, model, r.usage, True)
    content = r.choices[0].message.content
    if not content:
        raise AIError("the model returned nothing (it may have run out of its token budget)")
    try:
        return json.loads(content)
    except ValueError:
        raise AIError("the model's reply was not valid JSON")


def embed(texts: list[str], *, user_id: str | None = None) -> list[list[float]]:
    client = _get_client()
    model = embedding_model()
    r = client.embeddings.create(model=model, input=texts)
    _log(user_id, "embedding", model, r.usage, True, {"count": len(texts)})
    return [d.embedding for d in r.data]


def strict_object(properties: dict, required: list[str] | None = None) -> dict:
    """A strict JSON-schema object (every property required, nothing extra), as structured outputs need."""
    return {"type": "object", "properties": properties, "required": required or list(properties),
            "additionalProperties": False}
