"""RetryLadder — the Fail-Fast/Explain-Clearly orchestrator.

The directive's failure ladder: when a generation step can't produce a correct
artifact, ARTA must escalate effort along explicit rungs rather than silently
switch to an alternate path:

  1. context   — retry with an improved-context hint built from the prior
                 attempt's validation failures (R57.1 idiom).
  2. evidence  — retry with ADDITIONAL evidence injected (e.g. the Phase-1
                 architecture graphs / Phase-4 OpenAPI shapes).
  3. strategy  — retry with a different strategy (e.g. a larger token budget —
                 the _llm_with_growing_budget idiom).
  4. escalate  — escalate to a frontier model (R127.C `*_escalation` client).
  5. fail      — assemble a structured RootCauseReport and FAIL LOUDLY.

Every rung transition is logged (never silent). The ladder is generic over the
caller's `gen_fn` / `validate_fn` so the recipe / ATDD / risk / upstream stages
reuse one implementation. It does NOT itself call any LLM — the caller's
`gen_fn(LadderAttempt)` decides how to use the per-rung hint/evidence/budget/
client. Returns a result dict; on exhaustion it returns (does not raise) a
RootCauseReport so the caller controls how to surface the failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..models.root_cause_report import RootCauseReport

log = logging.getLogger("arta.retry_ladder")

_DEFAULT_RUNGS = ("context", "evidence", "strategy", "escalate")


@dataclass
class LadderAttempt:
    """Per-rung context handed to the caller's gen_fn."""
    rung: str
    attempt: int
    hint: str = ""                 # accumulated improved-context hint
    evidence: str = ""             # extra grounding evidence (rung >= evidence)
    max_tokens: int | None = None  # bumped on the 'strategy' rung
    client: Any = None             # frontier client on the 'escalate' rung
    last_violations: list = field(default_factory=list)


class RetryLadder:
    def __init__(
        self,
        *,
        stage: str,
        gen_fn: Callable[[LadderAttempt], Awaitable[Any]],
        validate_fn: Callable[[Any], tuple],     # output -> (ok: bool, violations: list)
        build_rca: Callable[[list, list], RootCauseReport],  # (violations, trace) -> RCA
        evidence_fn: Callable[[], str] | None = None,
        escalation_client: Any = None,
        hint_fn: Callable[[list], str] | None = None,
        base_max_tokens: int = 4000,
        requirement_id: str | None = None,
    ) -> None:
        self.stage = stage
        self._gen = gen_fn
        self._validate = validate_fn
        self._build_rca = build_rca
        self._evidence_fn = evidence_fn
        self._esc_client = escalation_client
        self._hint_fn = hint_fn or _default_hint
        self._base_max_tokens = base_max_tokens
        self._req = requirement_id

    def _rungs(self) -> list[str]:
        rungs = ["context"]
        if self._evidence_fn is not None:
            rungs.append("evidence")
        rungs.append("strategy")
        if self._esc_client is not None:
            rungs.append("escalate")
        return rungs

    async def execute(self) -> dict:
        trace: list[str] = []
        last_violations: list = []
        hint = ""
        evidence = ""
        for i, rung in enumerate(self._rungs(), start=1):
            if rung == "evidence" and self._evidence_fn is not None:
                try:
                    evidence = self._evidence_fn() or ""
                except Exception as exc:
                    log.info("retry_ladder[%s]: evidence_fn failed: %s", self.stage, exc)
                    evidence = ""
            attempt = LadderAttempt(
                rung=rung, attempt=i, hint=hint, evidence=evidence,
                max_tokens=(self._base_max_tokens * 4 if rung == "strategy" else None),
                client=(self._esc_client if rung == "escalate" else None),
                last_violations=last_violations,
            )
            log.info("retry_ladder[%s]: rung=%s attempt=%d (req=%s)",
                     self.stage, rung, i, self._req)
            try:
                output = await self._gen(attempt)
            except Exception as exc:
                last_violations = [{"kind": "gen_exception", "symbol": type(exc).__name__,
                                    "hint": str(exc)[:240]}]
                hint = self._hint_fn(last_violations)
                trace.append(rung)
                # TERMINAL exception — the failure is NOT fixable by advancing rungs
                # (e.g. a recipe whose columns can't ground because the SUT has NO
                # response shape at all; the evidence/strategy rungs would only
                # augment with a shape that doesn't exist). A gen_fn marks such an
                # exception via `exc._ladder_terminal = True`. Stop immediately
                # instead of burning the remaining rungs on an un-fixable failure.
                if getattr(exc, "_ladder_terminal", False):
                    log.warning(
                        "retry_ladder[%s]: rung=%s raised TERMINAL %s — stopping "
                        "(no rung can fix this)", self.stage, rung, type(exc).__name__)
                    break
                log.warning("retry_ladder[%s]: rung=%s raised %s — advancing",
                            self.stage, rung, type(exc).__name__)
                continue
            try:
                ok, violations = self._validate(output)
            except Exception as exc:
                ok, violations = False, [{"kind": "validate_exception",
                                          "symbol": type(exc).__name__, "hint": str(exc)[:240]}]
            trace.append(rung)
            if ok:
                log.info("retry_ladder[%s]: SUCCESS on rung=%s (trace=%s)",
                         self.stage, rung, "+".join(trace))
                return {"success": True, "output": output, "ladder_trace": trace,
                        "escalated": rung == "escalate"}
            last_violations = violations or []
            hint = self._hint_fn(last_violations)
            log.warning("retry_ladder[%s]: rung=%s produced %d violation(s) — advancing",
                        self.stage, rung, len(last_violations))

        # All rungs exhausted → fail loudly with a structured explanation.
        report = self._build_rca(last_violations, trace)
        report.ladder_trace = trace
        log.error("retry_ladder[%s]: EXHAUSTED (trace=%s) — %s",
                  self.stage, "+".join(trace), report.one_line())
        return {"success": False, "report": report, "ladder_trace": trace}


def _default_hint(violations: list) -> str:
    if not violations:
        return ""
    lines = []
    for v in violations[:5]:
        if isinstance(v, dict):
            lines.append(f"- {v.get('kind', '?')}: {v.get('hint') or v.get('symbol') or ''}")
        else:
            lines.append(f"- {getattr(v, 'kind', '?')}: {getattr(v, 'hint', '')}")
    return "Previous attempt failed validation; fix these before retrying:\n" + "\n".join(lines)
