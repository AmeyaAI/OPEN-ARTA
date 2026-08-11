"""R211 B2 — gen-time test-CASE quality validator (fail-first + endpoint-grounded)."""
from __future__ import annotations

from src.agents.grounding_validator import (
    validate_test_case_quality,
    scenario_budget_for_risk,
)

_MAPPED = [{"method": "GET", "path": "/api/v1/datasets/{id}"}]


def _kinds(viols):
    return {v.kind for v in viols}


def test_vague_then_is_rejected():
    sc = ("Scenario: user logs in\n  Given a user\n  When they submit\n"
          "  Then it works\n  And the user is logged in")
    viols = validate_test_case_quality(sc)
    assert "vague_assertion" in _kinds(viols)


def test_measurable_status_accepted():
    sc = ("Scenario: list datasets\n  When I GET datasets\n"
          "  Then the response status is 200\n  And the body contains a non-empty id field")
    assert validate_test_case_quality(sc) == []


def test_measurable_number_unit_accepted():
    sc = ("Scenario: latency\n  When I query\n  Then the response time is under 2000ms")
    assert validate_test_case_quality(sc) == []


def test_endpoint_ungrounded_for_api_typed():
    sc = ("Scenario: wrong api\n  When I GET /api/v1/billing/invoices/9\n"
          "  Then the response status is 200")
    viols = validate_test_case_quality(
        sc, mapped_endpoints=_MAPPED, is_api_typed=True)
    assert "endpoint_ungrounded" in _kinds(viols)


def test_endpoint_grounded_when_in_mapped_surface():
    sc = ("Scenario: right api\n  When I GET /api/v1/datasets/123\n"
          "  Then the response status is 200")
    viols = validate_test_case_quality(
        sc, mapped_endpoints=_MAPPED, is_api_typed=True)
    assert "endpoint_ungrounded" not in _kinds(viols)


def test_scenario_budget_scales_with_risk():
    p0 = scenario_budget_for_risk("P0")
    p3 = scenario_budget_for_risk("P3")
    assert {"happy_path", "negative"} <= p3
    assert "security" not in p3 and "concurrency" not in p3
    assert {"security", "concurrency", "accessibility", "performance"} <= p0
    # score-derived when priority absent
    assert "security" in scenario_budget_for_risk("", risk_score=8)
