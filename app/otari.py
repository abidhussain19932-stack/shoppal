"""Thin wrapper around the Otari LLM gateway (https://otari.ai).

Every model call in ShopPal goes through `OtariGateway.chat`, which:

* sends the request to Otari's OpenAI-compatible endpoint (base URL ``.../api/v1``),
* names a model *or an Otari routing policy* (e.g. ``shoppal-chat``) and, if a
  whole attempt fails, walks an app-side fallback chain of other models,
* tags the request with ``metadata`` (end user, role) so spend can be sliced in
  Otari's Usage view,
* reads Otari's serving headers (``Otari-Model``, ``Otari-Provider``,
  ``Otari-Fallback``, ``Otari-Attempt-Count``, ``Otari-Request-Id``) and the
  per-request ``usage.cost_usd`` and records them, so the app can show which
  model really answered and what it cost,
* turns Otari's ``budget_exceeded`` refusal into a typed exception so the bot can
  answer politely when a spend ceiling is hit.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import openai

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """Otari refused the request because a budget / spend ceiling is exhausted."""

    def __init__(self, scope: str = "", retry_after: str = ""):
        super().__init__(f"Otari budget exceeded (scope={scope or 'unknown'})")
        self.scope, self.retry_after = scope, retry_after


class GatewayError(Exception):
    """Every model in the chain failed."""


@dataclass
class CallInfo:
    role: str
    requested: str
    served_model: str = ""
    provider: str = ""
    otari_fallback: bool = False   # Otari's routing policy used a backup
    app_fallback: bool = False     # the app had to move to the next model in its chain
    attempts: int = 1
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    request_id: str = ""
    latency_ms: int = 0
    otari_tools: list = field(default_factory=list)

    def public(self) -> dict:
        return {"role": self.role, "requested": self.requested, "model": self.served_model,
                "provider": self.provider, "otari_fallback": self.otari_fallback,
                "app_fallback": self.app_fallback, "attempts": self.attempts,
                "cost_usd": round(self.cost_usd, 6), "tokens": self.prompt_tokens + self.completion_tokens,
                "latency_ms": self.latency_ms, "request_id": self.request_id,
                "otari_tools": self.otari_tools}


def _cost(usage: dict) -> float:
    for k in ("cost_usd", "estimated_cost"):
        v = usage.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _otari_tools(body: dict) -> list:
    out = []
    for c in body.get("otari_tool_calls") or []:
        if isinstance(c, dict):
            out.append(c.get("name") or c.get("type") or "tool")
    return out


class OtariGateway:
    def __init__(self, client: openai.AsyncOpenAI, store=None):
        self.client, self.store = client, store

    async def chat(self, role: str, models: list[str], *, end_user: str = "", extra_body: Optional[dict] = None,
                   **kwargs) -> tuple[Any, CallInfo]:
        """Call the first model in ``models`` that answers. Returns (completion, CallInfo)."""
        models = [m for m in dict.fromkeys(models) if m]  # dedupe, keep order
        body = dict(extra_body or {})
        meta = {"app": "shoppal", "role": role}
        if end_user:
            meta["end_user"] = str(end_user)[:200]
        body.setdefault("metadata", meta)
        errors = []
        for i, model in enumerate(models):
            t0 = time.monotonic()
            try:
                raw = await self.client.chat.completions.with_raw_response.create(
                    model=model, extra_body=body, **kwargs)
            except openai.APIStatusError as e:
                code = e.response.headers.get("otari-error-code", "")
                if code == "budget_exceeded" or "budget" in str(getattr(e, "message", "")).lower():
                    raise BudgetExceeded(e.response.headers.get("otari-budget-scope", ""),
                                         e.response.headers.get("retry-after", "")) from e
                if e.status_code in (401,):
                    raise
                log.warning("Otari %s call to %s failed (%s %s); trying next model",
                            role, model, e.status_code, code or e.message)
                errors.append(f"{model}: {e.status_code} {code or e.message}")
                continue
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                log.warning("Otari %s call to %s failed (%r); trying next model", role, model, e)
                errors.append(f"{model}: {e!r}")
                continue
            resp = raw.parse()
            h = raw.headers
            data = resp.model_dump() if hasattr(resp, "model_dump") else {}
            usage = data.get("usage") or {}
            info = CallInfo(
                role=role, requested=model,
                served_model=h.get("otari-model") or data.get("model") or model,
                provider=h.get("otari-provider", ""),
                otari_fallback=h.get("otari-fallback", "false").lower() == "true",
                app_fallback=i > 0,
                attempts=int(h.get("otari-attempt-count") or 1) + i,
                cost_usd=_cost(usage),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                request_id=h.get("otari-request-id", ""),
                latency_ms=int((time.monotonic() - t0) * 1000),
                otari_tools=_otari_tools(data),
            )
            if self.store is not None:
                try:
                    self.store.log_usage(info, end_user)
                except Exception:  # never let bookkeeping break a sale
                    log.exception("usage log failed")
            return resp, info
        raise GatewayError("; ".join(errors) or "no models configured")
