"""R211 Phase A — unified Test-Plan on RiskProfile + architecture-grounded
test-type refinement.

The keyword/priority floor (Phase L1 K9/K10/M4) stays the safety net; A2 only
REFINES it with real architecture signals (mapped endpoints, protocol, mutation,
DOM catalog). RiskProfile carries the new Test-Plan fields with back-compat
defaults.
"""
from __future__ import annotations

from dataclasses import fields

from src.agents.strategy_architect import (
    RiskProfile,
    architecture_ground_test_types,
    derive_protocols,
    detect_mutation_intent,
)


def test_detect_mutation_intent():
    # action verbs → destructive
    m = detect_mutation_intent("the user creates a dataset and uploads a file")
    assert m["destructive"] is True
    assert "create" in m["verbs"] and "upload" in m["verbs"]
    # read-only → not destructive
    assert detect_mutation_intent("the user lists and views collections")["destructive"] is False
    # empty
    assert detect_mutation_intent("")["destructive"] is False


def test_riskprofile_has_test_plan_fields_with_defaults():
    fnames = {f.name for f in fields(RiskProfile)}
    for new in ("endpoints", "workflow_chain", "data_needs", "mutation",
                "protocols", "ungroundable"):
        assert new in fnames, f"RiskProfile missing R211 field {new}"
    # constructing WITHOUT the new fields still works (back-compat)
    rp = RiskProfile(
        requirement_id="REQ-1", priority="P1", risk_score=6, impact=3,
        probability=2, risk_action="MITIGATE", rationale="x",
        test_types=["API"], coverage_target_pct=90, recommended_tools=["newman"],
    )
    assert rp.endpoints == [] and rp.ungroundable is False
    assert rp.mutation == {} and rp.protocols == []


def test_architecture_ground_is_additive_only():
    # floor already has API; DOM catalog adds UI + Accessibility, keeps API
    out = architecture_ground_test_types(
        ["API"], has_api_endpoints=True, has_dom_catalog=True)
    assert "API" in out and "UI" in out and "Accessibility" in out
    # mutation adds Performance
    out2 = architecture_ground_test_types(["API"], is_mutation=True)
    assert "Performance" in out2
    # no signals → unchanged (never drops the floor)
    assert architecture_ground_test_types(["Security", "API"]) == ["Security", "API"]


def test_derive_protocols():
    assert derive_protocols([]) == []
    assert derive_protocols([{"path": "/api/v1/datasets"}]) == ["rest"]
    protos = derive_protocols([{"path": "/api/v1/insights/stream"},
                               {"path": "/graphql"}])
    assert "sse" in protos and "graphql" in protos
