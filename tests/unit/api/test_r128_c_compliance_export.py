"""R128.C — Compliance audit export real-data integration unit tests.

Five cases lock down the real-data aggregation contract for
`/api/reports/compliance/export`:

1. Default call (no project_id, no real flag) → mock data preserved
   (backward compat for any legacy callers).
2. `?project_id=X` → real-data aggregator fires, returns the EU AI Act
   compliance_attestation header + per-decision rows.
3. R102.A grounding stamp on a Playwright spec is parsed into a
   `decision_type=auto_block_dispatch` entry with the right tool +
   violation_kinds metadata.
4. defect_intel triage rows are aggregated into `defect_classes` +
   `defect_subclasses` count maps.
5. xlsx format with real data → emits the new Decision Audit sheet
   shape (not the legacy TEA Layers / Non-Compliant Items mock sheets).
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from src.api.routers.reports import (
    _mock_compliance_data,
    _r128_c_real_compliance_data,
)


# ── Case 1: backward compat — mock path preserved ──────────────────────────


def test_r128c_mock_path_preserved_without_project_id():
    """Default mock_compliance_data still works (legacy callers safe)."""
    data = _mock_compliance_data()
    # Mock structure has overall_compliance + tea_layers; real does not.
    assert "overall_compliance" in data
    assert "tea_layers" in data
    # Real-only fields absent
    assert "compliance_attestation" not in data
    assert "human_in_the_loop_evidence" not in data


# ── Case 2: real-data aggregator returns the compliance attestation ───────


def test_r128c_real_data_includes_eu_ai_act_attestation():
    """`_r128_c_real_compliance_data` returns an EU AI Act Art. 26
    attestation referencing R102.A + R118.G + R127.D.6.F as the
    audit infrastructure."""
    data = _r128_c_real_compliance_data(project_id="test-proj")
    assert "compliance_attestation" in data
    attestation = data["compliance_attestation"]
    # The attestation MUST cite the specific ARTA mechanisms that satisfy
    # the regulatory contract (otherwise the export is meaningless).
    assert "EU AI Act" in attestation
    assert "R102.A" in attestation
    assert "R118.G" in attestation
    # Required structural fields
    assert "summary" in data
    assert "decisions" in data
    assert "human_in_the_loop_evidence" in data
    assert data["project_id"] == "test-proj"


# ── Case 3: R102.A grounding stamp surfaces as auto_block_dispatch entry ──


def test_r128c_r102a_stamp_parsed_into_decision_entry(tmp_path, monkeypatch):
    """A Playwright spec with an R102.A stamp produces a decision entry
    with type=auto_block_dispatch + tool=playwright + violation_kinds."""
    # Stage a fake PW spec dir with one stamped spec
    pw_dir = tmp_path / "src" / "automation" / "playwright"
    pw_dir.mkdir(parents=True)
    stamped = (
        "// ── ARTA _grounding_violations stamp (R102.A) ──\n"
        "// _dispatch_block_kind: playwright_grounding_violation\n"
        "// _grounding_violations:\n"
        '//   {"kind": "merged_paren_imbalance", "symbol": "<merged_spec>", "hint": "..."}\n'
        '//   {"kind": "subs_paren_imbalance", "symbol": "<merged_spec>", "hint": "..."}\n'
        "// ────────────────────────────────────────────────\n\n"
        "import { test, expect } from '@playwright/test';\n"
        "test('foo', async ({ page }) => { await page.goto('/'); });\n"
    )
    (pw_dir / "req_test_001.spec.ts").write_text(stamped)

    monkeypatch.chdir(tmp_path)
    data = _r128_c_real_compliance_data(project_id="p1")

    # Decision entry surfaced
    decisions = data["decisions"]
    pw_decision = next(
        (d for d in decisions if d["spec_file"] == "req_test_001.spec.ts"),
        None,
    )
    assert pw_decision is not None, f"Expected R102.A decision; got {decisions}"
    assert pw_decision["decision_type"] == "auto_block_dispatch"
    assert pw_decision["tool"] == "playwright"
    assert "merged_paren_imbalance" in pw_decision["violation_kinds"]
    assert "subs_paren_imbalance" in pw_decision["violation_kinds"]
    assert pw_decision["human_review_required"] is True
    # Summary aggregates the violation kinds across all stamped specs
    assert data["summary"]["playwright_grounding_stamps"] >= 1
    assert data["summary"]["violation_kinds"]["merged_paren_imbalance"] >= 1


# ── Case 4: defect_intel triage rows aggregate into class/subclass counts ─


def test_r128c_defect_classes_aggregated_from_triage():
    """defect_class + defect_subclass counts are populated from the
    defects module's in-memory store."""
    # Stage a stub defect list with diverse triage categories
    fake_defects = [
        {"project_id": "p1", "defect_class": "grounding_blocked",
         "defect_subclass": "per_sub_imbalance_escalated"},
        {"project_id": "p1", "defect_class": "grounding_blocked",
         "defect_subclass": "merge_paren_imbalance"},
        {"project_id": "p1", "defect_class": "operator_review",
         "defect_subclass": None},
        {"project_id": "p1", "defect_class": "test_gen_bug",
         "defect_subclass": "import_collision"},
        # Different project — must be filtered out when project_id=p1
        {"project_id": "other", "defect_class": "sut_regression"},
    ]
    with patch("src.api.routers.defects.MOCK_DEFECTS", fake_defects):
        data = _r128_c_real_compliance_data(project_id="p1")
    assert data["summary"]["defect_classes"]["grounding_blocked"] == 2
    assert data["summary"]["defect_classes"]["operator_review"] == 1
    assert data["summary"]["defect_classes"]["test_gen_bug"] == 1
    assert "sut_regression" not in data["summary"]["defect_classes"], (
        "Cross-project defects must be filtered out when project_id is set"
    )
    # Subclass aggregator surfaces the R127.D.6.F / R127.D.7 distinct kinds
    assert data["summary"]["defect_subclasses"]["per_sub_imbalance_escalated"] == 1
    assert data["summary"]["defect_subclasses"]["merge_paren_imbalance"] == 1
    # HITL evidence rolls up the same source data
    hitl = data["human_in_the_loop_evidence"]
    assert hitl["operator_review_routing"] == 1
    assert hitl["auto_heal_routing"] == 1
    assert hitl["grounding_blocked_routing"] == 2


# ── Case 5: xlsx format with real data emits Decision Audit sheet shape ───


def test_r128c_xlsx_real_data_emits_decision_audit_sheets(tmp_path, monkeypatch):
    """When real data is loaded, the xlsx response carries the new
    Attestation / Decision Audit / HITL Evidence sheet structure (not
    the legacy mock TEA Layers / Non-Compliant Items)."""
    import asyncio
    from fastapi.responses import JSONResponse
    import json
    from src.api.routers.reports import export_compliance_report, ExportFormat

    monkeypatch.chdir(tmp_path)
    # Run the endpoint with project_id set → real-data path
    resp = asyncio.run(export_compliance_report(
        format=ExportFormat.xlsx, project_id="test-proj", real=True,
    ))
    assert isinstance(resp, JSONResponse)
    payload = json.loads(resp.body.decode())
    assert payload["real_data"] is True
    sheets = payload["sheets"]
    # NEW real-data sheets are present
    assert "Attestation" in sheets
    assert "Decision Audit" in sheets
    assert "HITL Evidence" in sheets
    # LEGACY mock sheets are NOT used in real-data mode
    assert "TEA Layers" not in sheets
    assert "Non-Compliant Items" not in sheets
    # Attestation sheet carries the EU AI Act framing
    attestation_rows = sheets["Attestation"]["rows"]
    attestation_field = next(
        (row for row in attestation_rows if row[0] == "Attestation"), None,
    )
    assert attestation_field is not None
    assert "EU AI Act" in attestation_field[1]
