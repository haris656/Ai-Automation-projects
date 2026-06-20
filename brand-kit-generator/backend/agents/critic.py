"""
Critic agent.

Reviews the assembled brand kit against the strategy and returns a structured
verdict: which pieces pass and which need work, with specific feedback. This is
what turns the project from "four parallel generations" into a real agentic loop
where one agent judges another's work.
"""
from __future__ import annotations

import json
from typing import Any

from ..llm import LLMClient

# Pieces the critic is allowed to flag for regeneration.
REVIEWABLE = ("tagline", "copy", "visual")

SYSTEM = """You are a critical brand director reviewing a draft brand kit. You receive the strategy and the draft kit as JSON. \
Judge how well the kit delivers on the strategy. Be honest and specific. Do not rubber-stamp.

For each reviewable piece, decide pass or fail:
- "tagline": is it specific, memorable, and on-strategy (not generic filler)?
- "copy": is the one-liner and about paragraph concrete and in the right voice (no cliche AI words)?
- "visual": do the palette and font choices fit the visual direction in the strategy?

Respond with ONLY a raw JSON object in exactly this shape:
{
  "verdicts": {
    "tagline": {"pass": true or false, "feedback": "one specific sentence"},
    "copy": {"pass": true or false, "feedback": "one specific sentence"},
    "visual": {"pass": true or false, "feedback": "one specific sentence"}
  },
  "overall": "one sentence summary of the kit's strength"
}"""


def run(llm: LLMClient, strategy: dict[str, Any], kit: dict[str, Any]) -> dict[str, Any]:
    review_input = {
        "strategy": strategy,
        "draft": {
            "tagline": kit.get("tagline"),
            "alt_taglines": kit.get("alt_taglines"),
            "one_liner": kit.get("one_liner"),
            "about": kit.get("about"),
            "palette": kit.get("palette"),
            "fonts": kit.get("fonts"),
        },
    }
    user = json.dumps(review_input, ensure_ascii=False)

    try:
        data = llm.complete_json(SYSTEM, user, max_tokens=700, temperature=0.3)
    except Exception:
        # If the critic itself fails, pass everything through rather than block the kit.
        return {"verdicts": {k: {"pass": True, "feedback": ""} for k in REVIEWABLE}, "overall": ""}

    verdicts_in = data.get("verdicts", {}) if isinstance(data, dict) else {}
    verdicts: dict[str, dict[str, Any]] = {}
    for key in REVIEWABLE:
        v = verdicts_in.get(key, {}) if isinstance(verdicts_in, dict) else {}
        verdicts[key] = {
            "pass": bool(v.get("pass", True)) if isinstance(v, dict) else True,
            "feedback": (str(v.get("feedback", "")).strip()[:300] if isinstance(v, dict) else ""),
        }
    overall = str(data.get("overall", "")).strip()[:300] if isinstance(data, dict) else ""
    return {"verdicts": verdicts, "overall": overall}
