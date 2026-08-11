"""R213 (WS2a) — grade Gherkin PER SCENARIO, not per FILE.

atdd_designer appends one whole .feature FILE per requirement
(`all_gherkin.append(result["feature_file"])`, returned as `gherkin_scenarios`
— its docstring even says `["feature file content", ...]`), but tests.py
iterated that list as if each element were a scenario. For a single-requirement
generate that means n=1:
  • measurable_pct = 100% if ANY Then anywhere in the file is measurable
  • _acs[_i] paired AC[0] with "the entire file"
Every R213 number measured the wrong unit — which is why the gate could never
be trusted enough to turn on.
"""
from src.agents.grounding_validator import (
    ac_for_scenario,
    split_feature_scenarios,
)

_FEATURE = """Feature: Account management

  Background:
    Given the system is available

  @ac:AC-1 @smoke
  Scenario: View an account
    Given an account exists
    When the user opens it
    Then the balance shows 42

  @ac:AC-2
  Scenario: Reject an unknown account
    Given no such account
    When the user opens it
    Then a 404 is returned

  Scenario Outline: Filter accounts
    Given accounts exist
    When filtering by <status>
    Then only <status> accounts show
"""


def test_r213_splits_all_scenario_kinds():
    s = split_feature_scenarios(_FEATURE)
    assert len(s) == 3
    assert [x["name"] for x in s] == [
        "View an account", "Reject an unknown account", "Filter accounts"]


def test_r213_the_bug_one_file_is_not_one_scenario():
    """The exact defect: the whole file used to count as n=1."""
    assert len(split_feature_scenarios(_FEATURE)) > 1


def test_r213_background_is_not_graded_as_a_scenario():
    """Background is setup, not a test case — grading it would skew every pct."""
    s = split_feature_scenarios(_FEATURE)
    assert not any("Background" in x["name"] for x in s)
    assert "the system is available" not in s[0]["text"]


def test_r213_tags_attach_to_the_scenario_below_them():
    s = split_feature_scenarios(_FEATURE)
    assert "@ac:AC-1" in s[0]["tags"]
    assert "@smoke" in s[0]["tags"]
    assert "@ac:AC-2" in s[1]["tags"]
    # a scenario's trailing tag lines belong to the NEXT scenario
    assert "@ac:AC-2" not in s[0]["tags"]
    assert "@ac:AC-2" not in s[0]["text"]


def test_r213_scenario_body_is_self_contained():
    s = split_feature_scenarios(_FEATURE)
    assert "the balance shows 42" in s[0]["text"]
    assert "a 404 is returned" not in s[0]["text"]


def test_r213_line_numbers_reported():
    s = split_feature_scenarios(_FEATURE)
    assert s[0]["line"] < s[1]["line"] < s[2]["line"]


def test_r213_empty_and_headerless_inputs():
    assert split_feature_scenarios("") == []
    assert split_feature_scenarios("Feature: nothing here") == []


# ── AC pairing by identity, never position ──────────────────────────────────

_ACS = [
    {"id": "AC-1", "title": "view account"},
    {"id": "AC-2", "title": "reject unknown"},
]


def test_r213_ac_paired_by_tag():
    s = split_feature_scenarios(_FEATURE)
    assert ac_for_scenario(s[0], _ACS)["id"] == "AC-1"
    assert ac_for_scenario(s[1], _ACS)["id"] == "AC-2"


def test_r213_ac_pairing_survives_scenario_reordering():
    """Scenario order is the LLM's choice and has no relationship to AC order.
    Positional pairing reported violations against unrelated ACs."""
    s = split_feature_scenarios(_FEATURE)
    reordered = [s[1], s[0]]
    assert ac_for_scenario(reordered[0], _ACS)["id"] == "AC-2"
    assert ac_for_scenario(reordered[1], _ACS)["id"] == "AC-1"


def test_r213_ac_paired_by_name_when_untagged():
    scn = {"name": "AC-2 rejects it", "text": "", "line": 1, "tags": []}
    assert ac_for_scenario(scn, _ACS)["id"] == "AC-2"


def test_r213_no_ac_match_returns_none_not_a_wrong_one():
    scn = {"name": "something unrelated", "text": "", "line": 1, "tags": []}
    assert ac_for_scenario(scn, _ACS) is None


def test_r213_no_acs_returns_none():
    scn = {"name": "x", "text": "", "line": 1, "tags": ["@ac:AC-1"]}
    assert ac_for_scenario(scn, []) is None
