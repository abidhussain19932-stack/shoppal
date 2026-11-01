"""Safety guardrail: every customer message is screened by Llama Guard 4 (open weights),
called through the Otari gateway, before the sales agent sees it."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# MLCommons hazard taxonomy used by Llama Guard 4
CATEGORIES = {
    "S1": "violent crimes", "S2": "non-violent crimes", "S3": "sex-related crimes",
    "S4": "child sexual exploitation", "S5": "defamation", "S6": "specialized advice",
    "S7": "privacy", "S8": "intellectual property", "S9": "indiscriminate weapons",
    "S10": "hate", "S11": "suicide & self-harm", "S12": "sexual content", "S13": "elections",
    "S14": "code interpreter abuse",
}


@dataclass
class Verdict:
    safe: bool
    categories: list
    model: str = ""
    cost_usd: float = 0.0
    checked: bool = True  # False when the guard could not run (fail-open)
    call: Optional[dict] = None  # the Otari call record

    def label(self) -> str:
        if not self.checked:
            return "unchecked"
        if self.safe:
            return "safe"
        return "unsafe: " + ", ".join(CATEGORIES.get(c, c) for c in self.categories)


def parse(text: str) -> tuple[bool, list]:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    if not lines:
        return True, []
    if lines[0].lower().startswith("unsafe"):
        cats = []
        for ln in lines[1:]:
            cats += [c.strip() for c in ln.split(",") if c.strip()]
        return False, cats
    return True, []


class Guard:
    def __init__(self, gateway, model: str, enabled: bool = True):
        self.gw, self.model, self.enabled = gateway, model, enabled

    async def check(self, text: str, end_user: str = "", history: Optional[list] = None) -> Verdict:
        if not self.enabled or not self.model:
            return Verdict(True, [], checked=False)
        msgs = [m for m in (history or [])[-2:] if m.get("role") in ("user", "assistant")]
        if msgs and msgs[0]["role"] == "assistant":
            msgs = msgs[1:]
        msgs.append({"role": "user", "content": text[:4000]})
        try:
            resp, info = await self.gw.chat("guardrail", [self.model], end_user=end_user,
                                            messages=msgs, max_tokens=20, temperature=0)
        except Exception as e:  # fail open: a guard outage must not take the shop down
            if e.__class__.__name__ == "BudgetExceeded":
                raise
            log.warning("guard unavailable, serving unchecked: %r", e)
            return Verdict(True, [], checked=False)
        safe, cats = parse(resp.choices[0].message.content or "")
        return Verdict(safe, cats, info.served_model, info.cost_usd, call=info.public())
