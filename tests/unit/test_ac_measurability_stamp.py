"""Per-test AC-measurability provenance stamp (_ac_measurability_of)."""
from src.api.routers.tests import _ac_measurability_of


def test_stamped_verdict_wins_over_enriched_text():
    # R205 appends measurable clauses before mint — the import-time ac_flags
    # verdict (pre-enrichment source truth) must take precedence.
    req = {"metadata": {"quality": {"ac_flags": ["AC-U"]}}}
    enriched = {"id": "AC-U", "statement": "friendly UI (Verifiable: returns HTTP 200)"}
    assert _ac_measurability_of(enriched, "AC-U", req) == "unmeasured"
    assert _ac_measurability_of(enriched, "AC-M", req) == "measurable"


def test_heuristic_fallback_without_stamp():
    assert _ac_measurability_of({"id": "A", "then": "returns HTTP 200"}, "A", {}) == "measurable"
    assert _ac_measurability_of({"id": "A", "statement": "feels friendly"}, "A", None) == "unmeasured"


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_AC_MEASURABILITY_WEIGHT_DISABLE", "1")
    assert _ac_measurability_of({"id": "A", "statement": "feels friendly"}, "A", {}) == "unknown"
