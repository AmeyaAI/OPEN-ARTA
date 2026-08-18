"""Unit tests for the upstream artifact-quality validators (BMAD TEA L1-3).

Covers the warn-by-default / block-on-catastrophic severity policy:
requirement testability, Gherkin structural+intent quality, AC coverage,
and the combined post-ATDD seam helper.
"""
from __future__ import annotations

from src.agents.upstream_quality import (
    validate_requirement_quality,
    validate_gherkin_quality,
    validate_ac_coverage,
    validate_gherkin_stage,
    violations_to_hint,
)


def _codes(res):
    return {v.code for v in res.violations}


# ── B1: requirement quality ──────────────────────────────────────────────────

def test_requirement_no_ac_is_error_and_blocks():
    res = validate_requirement_quality({"req_id": "REQ-X", "acceptance_criteria": []})
    assert "RQ-001" in _codes(res)
    assert res.passed is False  # error severity → block
    assert any(v.severity == "error" for v in res.violations)


def test_non_measurable_ac_warns_but_does_not_block():
    req = {"req_id": "REQ-XY-013", "acceptance_criteria": [
        {"id": "AC1", "statement": "Database connector validates connection"},
        {"id": "AC2", "statement": "Cross-source query works"},
    ]}
    res = validate_requirement_quality(req)
    assert "RQ-002" in _codes(res)         # non-measurable flagged
    assert res.passed is True              # but does NOT block
    m = res.criteria_results["_metrics"]
    assert m["ac_count"] == 2
    assert m["measurable_ac_count"] == 0


def test_measurable_ac_is_clean():
    req = {"req_id": "REQ-OK", "acceptance_criteria": [
        {"statement": "Indexing completes within 5 seconds"},
        {"statement": "Unauthenticated requests return HTTP 401"},
    ]}
    res = validate_requirement_quality(req)
    assert "RQ-002" not in _codes(res)
    assert res.criteria_results["_metrics"]["measurable_pct"] == 100.0


def test_vague_term_flagged():
    req = {"req_id": "REQ-V", "acceptance_criteria": [
        {"statement": "The system works correctly and is user-friendly"}]}
    res = validate_requirement_quality(req)
    assert "RQ-003" in _codes(res)


def test_duplicate_acs_flagged():
    req = {"req_id": "REQ-D", "acceptance_criteria": [
        {"statement": "Returns 200 on success"},
        {"statement": "returns 200 on success"},  # case-insensitive dup
    ]}
    res = validate_requirement_quality(req)
    assert "RQ-004" in _codes(res)


# ── B2: Gherkin quality ──────────────────────────────────────────────────────

_GOOD_GHERKIN = """Feature: Email Monitoring Workers
  Scenario: Gmail filters by subject and target emails
    Given a Gmail account is connected with subject filters
    When the worker polls the Gmail API for new messages
    Then only messages matching the subject and target email are downloaded
"""


def test_valid_gherkin_passes():
    req = {"req_id": "REQ-XY-009", "title": "Email Monitoring Workers Gmail Outlook",
           "description": "Gmail polls via API, subject filters, downloads attachments",
           "acceptance_criteria": [{"statement": "Gmail filters by subject and target emails"}]}
    res = validate_gherkin_quality([_GOOD_GHERKIN], req, req["acceptance_criteria"])
    assert res.passed is True
    assert not any(v.severity == "error" for v in res.violations)


def test_missing_when_then_blocks():
    gk = "Feature: Y\n  Scenario: incomplete\n    Given something exists\n"
    res = validate_gherkin_quality([gk], {"req_id": "R"})
    assert "GQ-002" in _codes(res)
    assert res.passed is False


def test_no_feature_blocks():
    res = validate_gherkin_quality(["just some prose, not gherkin"], {"req_id": "R"})
    assert "GQ-001" in _codes(res)
    assert res.passed is False


def test_fallback_gherkin_blocks_via_markers():
    fb = ("Feature: X\n  Scenario: do thing\n"
          "    Given the system is configured for the feature\n"
          "    When the user performs: stuff\n"
          "    Then the expected outcome is achieved\n")
    res = validate_gherkin_quality([fb], {"req_id": "R"})
    assert "GQ-003" in _codes(res)
    assert res.passed is False


def test_fallback_gherkin_blocks_via_gen_source():
    res = validate_gherkin_quality([_GOOD_GHERKIN], {"req_id": "R"}, gen_source="fallback")
    assert "GQ-003" in _codes(res)
    assert res.passed is False


def test_intent_drift_warns_not_blocks():
    req = {"req_id": "R", "title": "Document schema auto-generation OCR extraction",
           "description": "Auto-generate extraction schema from sample document via OCR and LLM"}
    # Gherkin about something totally unrelated.
    gk = ("Feature: Z\n  Scenario: navigate menu\n    Given a sidebar exists\n"
          "    When the visitor opens the catalog tab\n    Then breadcrumbs appear\n")
    res = validate_gherkin_quality([gk], req)
    assert "GQ-004" in _codes(res)
    assert res.passed is True  # warning only


def test_impl_leakage_warns():
    gk = ("Feature: Z\n  Scenario: s\n    Given a page\n"
          "    When the user clicks await page.click('#x')\n    Then it works\n")
    res = validate_gherkin_quality([gk], {"req_id": "R"})
    assert "GQ-005" in _codes(res)


# ── B3: AC coverage ──────────────────────────────────────────────────────────

def test_ac_coverage_computes_and_flags_uncovered():
    acs = [{"statement": "Gmail filters by subject and target emails"},
           {"statement": "Quarterly revenue forecasting dashboard export"}]
    res = validate_ac_coverage(acs, [_GOOD_GHERKIN])
    m = res.criteria_results["_metrics"]
    assert m["ac_total"] == 2
    assert m["ac_covered"] == 1            # only the Gmail one is represented
    assert "AC-001" in _codes(res)


# ── seam helper ──────────────────────────────────────────────────────────────

def test_stage_helper_blocks_on_fallback_with_hint():
    fb = ("Feature: X\n  Scenario: s\n    Given the system is in the correct state\n"
          "    When the user performs the required action\n"
          "    Then the expected outcome is observed\n")
    out = validate_gherkin_stage([fb], {"req_id": "R"}, gen_source="fallback")
    assert out["should_block"] is True
    assert out["error_count"] >= 1
    assert "MUST FIX" in out["hint"]


def test_stage_helper_warnings_do_not_block():
    req = {"req_id": "R", "title": "Document schema OCR extraction",
           "description": "schema auto-generation"}
    gk = ("Feature: Z\n  Scenario: unrelated nav\n    Given a sidebar\n"
          "    When the visitor opens the catalog\n    Then breadcrumbs appear\n")
    out = validate_gherkin_stage([gk], req)
    assert out["should_block"] is False     # only warnings (drift / coverage)
    assert out["warning_count"] >= 1


def test_hint_renders_severity_tags():
    req = {"req_id": "R", "acceptance_criteria": [{"statement": "it works nicely"}]}
    res = validate_requirement_quality(req)
    hint = violations_to_hint(res.violations)
    assert "improve" in hint  # warnings rendered as "improve"


# ── B5: persistence + project aggregation ────────────────────────────────────

def test_persist_and_aggregate_roundtrip(tmp_path, monkeypatch):
    import src.agents.upstream_quality as uq

    monkeypatch.setattr(uq, "_UPSTREAM_DIR", tmp_path)

    # Requirement with 1 of 2 measurable ACs, aligned good Gherkin.
    req_good = {"req_id": "REQ-G", "title": "Email Monitoring Gmail",
                "description": "Gmail polls API subject filters downloads attachments",
                "acceptance_criteria": [
                    {"statement": "Indexing completes within 5 seconds"},
                    {"statement": "Gmail filters by subject works"}]}
    rq = uq.validate_requirement_quality(req_good)
    gs = uq.validate_gherkin_stage([_GOOD_GHERKIN], req_good, req_good["acceptance_criteria"])
    uq.persist_upstream_quality("REQ-G", "proj-1", requirement_result=rq, gherkin_stage=gs)

    # A fallback (blocked) requirement.
    req_bad = {"req_id": "REQ-B", "title": "X", "description": "y",
               "acceptance_criteria": [{"statement": "it works"}]}
    rqb = uq.validate_requirement_quality(req_bad)
    gsb = uq.validate_gherkin_stage([_GOOD_GHERKIN], req_bad, gen_source="fallback")
    uq.persist_upstream_quality("REQ-B", "proj-1", requirement_result=rqb, gherkin_stage=gsb)

    agg = uq.read_upstream_quality(project_id="proj-1")
    assert agg["requirement_count"] == 2
    assert agg["fallback_rate"] == 50.0          # 1 of 2 fallback
    assert agg["gherkin_block_rate"] == 50.0     # the fallback one blocks
    assert agg["measurable_ac_pct"] is not None
    assert agg["gherkin_alignment_pct"] is not None
    assert len(agg["rows"]) == 2


def test_aggregate_filters_by_project(tmp_path, monkeypatch):
    import src.agents.upstream_quality as uq
    monkeypatch.setattr(uq, "_UPSTREAM_DIR", tmp_path)
    rq = uq.validate_requirement_quality(
        {"req_id": "A", "acceptance_criteria": [{"statement": "returns 200"}]})
    uq.persist_upstream_quality("A", "proj-1", requirement_result=rq)
    uq.persist_upstream_quality("B", "proj-2", requirement_result=rq)
    assert uq.read_upstream_quality(project_id="proj-1")["requirement_count"] == 1
    assert uq.read_upstream_quality()["requirement_count"] == 2


# ── Phase 2: clarity score + source augmentation ─────────────────────────────

def test_clarity_score_bands():
    import src.agents.upstream_quality as uq
    clear = uq.requirement_clarity_score({"req_id": "C", "acceptance_criteria": [
        {"statement": "Indexing completes within 5 seconds"},
        {"statement": "Unauthenticated requests return HTTP 401"}]})
    assert clear["band"] == "clear" and clear["score"] >= 80
    unclear = uq.requirement_clarity_score({"req_id": "U", "acceptance_criteria": []})
    assert unclear["band"] == "unclear"  # no ACs → RQ-001 error tanks the score
    assert unclear["highlights"]


def test_source_augmentation_uses_architecture(monkeypatch):
    import src.agents.upstream_quality as uq
    monkeypatch.setattr("src.agents.architecture_discovery.summarize_for_prompt",
                        lambda pid, max_chars=2200: "[ARCHITECTURE DISCOVERY] GET /api/v1/datasets")
    plain = uq.build_source_augmentation({"req_id": "R"}, "pid-1")
    assert "ARCHITECTURE DISCOVERY" in plain and "NOTE" not in plain
    emph = uq.build_source_augmentation({"req_id": "R"}, "pid-1", clarity_band="unclear")
    assert "underspecified" in emph and "ARCHITECTURE DISCOVERY" in emph


def test_source_augmentation_empty_when_no_discovery(monkeypatch):
    import src.agents.upstream_quality as uq
    monkeypatch.setattr("src.agents.architecture_discovery.summarize_for_prompt",
                        lambda pid, max_chars=2200: "")
    assert uq.build_source_augmentation({"req_id": "R"}, "pid-1") == ""


def test_clarity_aggregated_in_read(tmp_path, monkeypatch):
    import src.agents.upstream_quality as uq
    monkeypatch.setattr(uq, "_UPSTREAM_DIR", tmp_path)
    rq = uq.validate_requirement_quality({"req_id": "A", "acceptance_criteria": [{"statement": "returns 200"}]})
    uq.persist_upstream_quality("A", "p1", requirement_result=rq,
                                clarity={"score": 92, "band": "clear"})
    uq.persist_upstream_quality("B", "p1", requirement_result=rq,
                                clarity={"score": 30, "band": "unclear"})
    agg = uq.read_upstream_quality(project_id="p1")
    assert agg["clarity_bands"].get("clear") == 1
    assert agg["clarity_bands"].get("unclear") == 1
    assert agg["clarity_score_mean"] == 61.0


# ── Measurability reads the WHOLE AC (statement + given/when/then) ──────────

def test_measurability_reads_then_clause():
    # `then` was false-flagged when only `statement` was scored.
    req = {"req_id": "ABC-394", "acceptance_criteria": [
        {"id": "AC1", "statement": "Token endpoint issues access token",
         "given": "a configured OIDC client", "when": "the client requests a token",
         "then": "the endpoint returns HTTP 200 with a non-empty access_token"},
    ]}
    res = validate_requirement_quality(req)
    assert "RQ-002" not in _codes(res)
    assert res.criteria_results["_metrics"]["measurable_pct"] == 100.0


def test_ac_measurability_flags_per_ac():
    from src.agents.upstream_quality import ac_measurability_flags
    acs = [
        {"id": "AC-M", "statement": "t", "then": "returns HTTP 200"},
        {"id": "AC-U", "statement": "The UI feels responsive and friendly"},
    ]
    assert ac_measurability_flags(acs) == ["AC-U"]
    assert ac_measurability_flags([]) == []


def test_extraction_prompts_carry_unmeasured_fallback():
    # C0: when the source states no observable outcome, the synthesis contract
    # demands an explicit [UNMEASURED] marker instead of an invented threshold.
    from src.prompts.tea_prompts import (
        REQUIREMENT_EXTRACTION, SINGLE_REQUIREMENT_EXTRACTION)
    for p in (REQUIREMENT_EXTRACTION, SINGLE_REQUIREMENT_EXTRACTION):
        assert "[UNMEASURED — needs refinement]" in p
        assert "do NOT invent" in p


def test_r205_enrich_skips_ac_with_measurable_then(monkeypatch):
    # R205's inline check scored only the statement — an AC whose `then` is
    # measurable must NOT be enriched (needless mutation + prompt bloat).
    from src.agents.upstream_quality import enrich_requirement_acs_with_source
    req = {"req_id": "R", "acceptance_criteria": [
        {"id": "A1", "statement": "Token endpoint issues token",
         "then": "the endpoint returns HTTP 200"},
        {"id": "A2", "statement": "The UI feels friendly"},
    ]}
    out = enrich_requirement_acs_with_source(req, "no-such-project")
    assert "Verifiable" not in out["acceptance_criteria"][0]["statement"]
    assert "Verifiable" in out["acceptance_criteria"][1]["statement"]
    assert out.get("_r205_acs_enriched") == 1
