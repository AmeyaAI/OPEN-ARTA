"""F9-7: Cover the F8-5 _dedup_scenarios method on ATDDDesignerAgent."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agents.atdd_designer import ATDDDesignerAgent


@pytest.fixture
def agent():
    # AsyncAnthropic isn't called by _dedup_scenarios, so any mock works
    return ATDDDesignerAgent(client=AsyncMock())


def _make_feature(*scenarios: str) -> str:
    body = "\n\n".join(scenarios)
    return f"Feature: Test\n  As a tester\n\n{body}\n"


_SCENARIO_A = (
    "  Scenario: Login succeeds\n"
    "    Given valid credentials\n"
    "    When the user logs in\n"
    "    Then access is granted"
)
_SCENARIO_B = (
    "  Scenario: Login fails\n"
    "    Given invalid credentials\n"
    "    When the user logs in\n"
    "    Then access is denied"
)


class TestDedupScenarios:

    def test_drops_byte_identical_duplicates(self, agent):
        feature = _make_feature(_SCENARIO_A, _SCENARIO_A, _SCENARIO_B)
        out = agent._dedup_scenarios(feature, "REQ-100")
        # Only 2 unique scenario blocks should remain (A + B)
        assert out.count("Scenario:") == 2
        assert "Login succeeds" in out and "Login fails" in out

    def test_unique_scenarios_unchanged(self, agent):
        feature = _make_feature(_SCENARIO_A, _SCENARIO_B)
        out = agent._dedup_scenarios(feature, "REQ-101")
        assert out.count("Scenario:") == 2

    def test_single_scenario_unchanged(self, agent):
        feature = _make_feature(_SCENARIO_A)
        out = agent._dedup_scenarios(feature, "REQ-102")
        assert out == feature  # short-circuit returns input unchanged

    def test_whitespace_variance_treated_as_duplicate(self, agent):
        # Same scenario with extra leading whitespace on each line
        scen_a_padded = "\n".join("    " + ln.strip() for ln in _SCENARIO_A.splitlines() if ln.strip())
        feature = _make_feature(_SCENARIO_A, "  " + scen_a_padded)
        out = agent._dedup_scenarios(feature, "REQ-103")
        # Both blocks normalise to the same hash → only one survives
        assert out.count("Scenario:") == 1
