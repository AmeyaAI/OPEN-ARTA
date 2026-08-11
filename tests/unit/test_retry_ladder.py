"""S2 — RetryLadder rung progression + fail-with-RootCauseReport."""
from __future__ import annotations

import asyncio

from src.agents.retry_ladder import RetryLadder, LadderAttempt
from src.models.root_cause_report import build_report


def _rca(stage="dataset_recipe"):
    def _b(violations, trace):
        return build_report(
            failure_id="REQ-X", stage=stage,
            root_cause=f"exhausted after {len(violations)} violations",
            recommended_fix="refresh discovery", project_id="p1", requirement_id="REQ-X")
    return _b


def test_success_short_circuits_on_first_rung():
    seen = []

    async def gen(a: LadderAttempt):
        seen.append(a.rung)
        return "GOOD"

    def validate(out):
        return (out == "GOOD", [])

    ladder = RetryLadder(stage="risk_scoring", gen_fn=gen, validate_fn=validate,
                         build_rca=_rca("risk_scoring"))
    res = asyncio.run(ladder.execute())
    assert res["success"] is True
    assert seen == ["context"]          # stopped at the first rung
    assert res["ladder_trace"] == ["context"]


def test_progresses_through_rungs_then_succeeds_on_evidence():
    seen = []

    async def gen(a: LadderAttempt):
        seen.append(a.rung)
        # fail on context, succeed once evidence is injected
        return "GOOD" if a.rung == "evidence" else "BAD"

    def validate(out):
        return (out == "GOOD", [{"kind": "ungrounded", "hint": "needs evidence"}])

    ladder = RetryLadder(
        stage="dataset_recipe", gen_fn=gen, validate_fn=validate,
        build_rca=_rca(), evidence_fn=lambda: "ARCHITECTURE GRAPH EVIDENCE")
    res = asyncio.run(ladder.execute())
    assert res["success"] is True
    assert seen[:2] == ["context", "evidence"]
    assert res["ladder_trace"] == ["context", "evidence"]


def test_escalation_rung_uses_frontier_client():
    used_client = {}

    async def gen(a: LadderAttempt):
        if a.rung == "escalate":
            used_client["client"] = a.client
            return "GOOD"
        return "BAD"

    def validate(out):
        return (out == "GOOD", [{"kind": "x"}])

    ladder = RetryLadder(
        stage="atdd", gen_fn=gen, validate_fn=validate, build_rca=_rca("atdd"),
        evidence_fn=lambda: "ev", escalation_client="FRONTIER")
    res = asyncio.run(ladder.execute())
    assert res["success"] is True and res["escalated"] is True
    assert used_client["client"] == "FRONTIER"
    assert res["ladder_trace"] == ["context", "evidence", "strategy", "escalate"]


def test_strategy_rung_bumps_budget():
    budgets = []

    async def gen(a: LadderAttempt):
        budgets.append((a.rung, a.max_tokens))
        return "BAD"

    def validate(out):
        return (False, [{"kind": "x"}])

    ladder = RetryLadder(stage="risk_scoring", gen_fn=gen, validate_fn=validate,
                         build_rca=_rca("risk_scoring"), base_max_tokens=4000)
    asyncio.run(ladder.execute())
    strat = dict((r, t) for r, t in budgets)
    assert strat["strategy"] == 16000      # 4000 * 4
    assert strat["context"] is None


def test_exhaustion_returns_root_cause_report():
    async def gen(a: LadderAttempt):
        return "BAD"

    def validate(out):
        return (False, [{"kind": "ungrounded", "hint": "20 ungrounded columns"}])

    ladder = RetryLadder(
        stage="dataset_recipe", gen_fn=gen, validate_fn=validate, build_rca=_rca(),
        evidence_fn=lambda: "ev", escalation_client="FRONTIER", requirement_id="REQ-X")
    res = asyncio.run(ladder.execute())
    assert res["success"] is False
    rep = res["report"]
    assert rep.stage == "dataset_recipe"
    assert rep.ladder_trace == ["context", "evidence", "strategy", "escalate"]
    assert "exhausted" in rep.root_cause


def test_gen_exception_advances_not_crash():
    async def gen(a: LadderAttempt):
        if a.rung == "context":
            raise RuntimeError("LLM down")
        return "GOOD"

    def validate(out):
        return (out == "GOOD", [])

    ladder = RetryLadder(stage="risk_scoring", gen_fn=gen, validate_fn=validate,
                         build_rca=_rca("risk_scoring"), evidence_fn=lambda: "ev")
    res = asyncio.run(ladder.execute())
    assert res["success"] is True            # recovered on the evidence rung
    assert res["ladder_trace"] == ["context", "evidence"]
