"""J4: LLM-as-Judge Runtime.

Evaluates analytics narrative outputs (and any test result with an `eval_rubric`)
against the criteria defined in the rubric. Used by the execution router to:
  - Compute a 0.0-1.0 `judge_score` per analytics test result
  - Emit `judge_issues` listing specific problems
  - Gate the test PASS/FAIL on `score >= rubric.passing_threshold`

Cheap by design: defaults to Haiku/qwen3:8b (the FAST tier) so judging at scale
doesn't blow the cost budget.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("arta.judge")


_DEFAULT_RUBRIC_PROMPT = """\
Given this underlying data: {actual_data}
And this generated insight: {generated_insight}

Evaluate the insight on these criteria:
1. Numerically accurate: does the text match the data?
2. Directionally correct: does the trend direction match?
3. Scope adherent: does it respect the requested filters and time window?
4. Hallucination-free: are all claims supported by the data?

[CRITICAL] Output ONLY the JSON object. No prose, no markdown fences. Start with `{{` and end with `}}`.
{{
  "accurate": true|false,
  "directional": true|false,
  "scope_adherent": true|false,
  "hallucination_free": true|false,
  "score": 0.0-1.0,
  "issues": ["list of specific problems if any"]
}}
"""


class LLMJudge:
    """Lightweight evaluator that scores generated insights via LLM-as-judge."""

    def __init__(self, client: Any, model: str = "claude-haiku-4-5-20251001") -> None:
        # client must implement .messages.create(model=..., max_tokens=..., messages=[...])
        # OllamaDirectClient routes "haiku" → ARTA_FAST_MODEL automatically.
        self._client = client
        self._model = model

    async def evaluate(
        self,
        generated_insight: str,
        actual_data: Any,
        rubric: dict | None = None,
    ) -> dict:
        """Return {accurate, directional, scope_adherent, hallucination_free, score, issues}.

        Falls back to a "judge unavailable" record (score=None) if the LLM call errors;
        callers can decide whether to mark the test PASS or FAIL on judge unavailability.
        """
        prompt_template = (rubric or {}).get("judge_prompt") or _DEFAULT_RUBRIC_PROMPT
        prompt = prompt_template.format(
            actual_data=_truncate(_to_str(actual_data), 2000),
            generated_insight=_truncate(_to_str(generated_insight), 2000),
        )

        # F9-9: Route through circuit breaker like every other agent — without
        # this, a degraded provider would slow every judged analytics test
        # down by N×60s instead of failing fast after the breaker opens.
        from .circuit_breaker import get_breaker
        provider_name = getattr(self._client, "provider", None) or type(self._client).__name__
        breaker = get_breaker(str(provider_name))
        try:
            resp = await breaker.call(
                self._client.messages.create,
                model=self._model,
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
                timeout=60.0,
            )
            text = resp.content[0].text if resp.content else ""
        except Exception as exc:
            log.warning("Judge LLM call failed (%s: %s) — recording judge_unavailable",
                        type(exc).__name__, exc)
            return {
                "accurate": None, "directional": None,
                "scope_adherent": None, "hallucination_free": None,
                "score": None, "issues": [f"judge_unavailable: {exc}"],
                "judge_status": "unavailable",
            }

        try:
            parsed = _extract_json(text)
        except ValueError:
            log.warning("Judge returned unparseable response: %s", text[:200])
            return {
                "accurate": None, "directional": None,
                "scope_adherent": None, "hallucination_free": None,
                "score": None, "issues": [f"unparseable_response: {text[:120]}"],
                "judge_status": "unparseable",
            }

        # Normalise + clamp score
        score = parsed.get("score", 0.0)
        try:
            score_f = float(score)
            score_f = max(0.0, min(1.0, score_f))
        except (TypeError, ValueError):
            score_f = 0.0
        parsed["score"] = score_f
        parsed.setdefault("issues", [])
        parsed["judge_status"] = "ok"
        return parsed


def _extract_json(text: str) -> dict:
    """Robustly pull a JSON object out of LLM text."""
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"No JSON found in: {text[:200]}")


def _to_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len] + "...[truncated]"
