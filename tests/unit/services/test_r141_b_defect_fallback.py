"""R141.B — deterministic fallback truthfulness + blanket-defect safety net.

Pre-R141.B: when LLM cascade returned 0 defects AND deterministic
fallback also returned 0, the dashboard silently read "0 defects" while
hundreds of FAILs sat in execution_results (R135 evidence: 522 + 224
failures across 2 iters → 0 defects → operator-invisible classifier
outage).

Post-R141.B:
  1. WARN log emitted on EVERY fallback fire (not only when defects > 0)
  2. Blanket-defect helper emits ≥1 defect per failures batch so the
     dashboard NEVER reads 0 defects with hundreds of FAILs. R141.C
     dashboard tile reads this signal as alert.
"""
from __future__ import annotations

from src.api.services.post_run_chain_pipeline import _r141_b_build_blanket_defect


def test_r141_b_blanket_defect_has_required_shape():
    failures = [
        {"test_id": "TC-001", "automation_tool": "newman", "status": "FAIL"},
        {"test_id": "TC-002", "automation_tool": "newman", "status": "FAIL"},
    ]
    defect = _r141_b_build_blanket_defect(failures, "run-test-123")
    # Mission-truthful shape: severity=medium (not critical — classifier
    # outage doesn't necessarily mean a real SUT bug); triage_signals
    # carries the r141_b_blanket marker so R141.C tile can detect it.
    assert defect["severity"] == "medium"
    assert defect["triage_category"] == "unclassified"
    assert "r141_b_blanket" in defect["triage_signals"]
    assert defect["triage_confidence"] == 0.0
    assert defect["cluster_size"] == 2
    assert "run-test-123" in defect["defect_id"]


def test_r141_b_blanket_defect_aggregates_tools():
    """Multiple distinct tools must surface in the defect's tools list."""
    failures = [
        {"test_id": "TC-1", "automation_tool": "newman"},
        {"test_id": "TC-2", "automation_tool": "playwright"},
        {"test_id": "TC-3", "automation_tool": "newman"},
        {"test_id": "TC-4", "automation_tool": "k6"},
    ]
    defect = _r141_b_build_blanket_defect(failures, "run-Y")
    assert set(defect["tools"]) == {"newman", "playwright", "k6"}


def test_r141_b_blanket_defect_caps_sample_test_ids_at_10():
    """Sample test_ids slot capped at 10 to keep defect rows compact."""
    failures = [{"test_id": f"TC-{i:03d}", "automation_tool": "newman"} for i in range(50)]
    defect = _r141_b_build_blanket_defect(failures, "run-Z")
    assert len(defect["affected_tests"]) == 10
    assert defect["cluster_size"] == 50  # full count preserved in cluster_size


def test_r141_b_blanket_defect_handles_empty_failures():
    """Edge case: empty failures list still produces a valid defect dict
    (caller is responsible for skipping the call when failures is empty,
    but the helper itself must be robust)."""
    defect = _r141_b_build_blanket_defect([], "run-empty")
    assert defect["cluster_size"] == 0
    assert defect["affected_tests"] == []
    assert defect["tools"] == []
    assert defect["severity"] == "medium"


def test_r141_b_blanket_defect_filters_non_dict_failures():
    """Defensive: malformed failure entries (None, strings) must not
    crash the helper — they get filtered."""
    failures = [
        {"test_id": "TC-1", "automation_tool": "newman"},
        None,
        "not-a-dict",
        {"test_id": "TC-2", "automation_tool": "newman"},
    ]
    defect = _r141_b_build_blanket_defect(failures, "run-mix")  # type: ignore
    assert defect["cluster_size"] == 4  # raw count includes malformed
    assert defect["affected_tests"] == ["TC-1", "TC-2"]  # filtered cleanly
