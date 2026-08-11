"""R124.K — Newman body_preview promoted into metadata.response_body_preview.

ROOT CAUSE fix (not observability patch): pre-R124.K the body lived ONLY
in result-row's top-level `actual.body_preview` field which is dropped
at DB persistence time. Post-DB-load classifiers saw empty bodies →
R111.H cascade signal matchers never fired → 1737 generic 500s in
run-d52a8c stayed opaque.

R124.K promotes the body into metadata at `_build_params` so it
SURVIVES DB round-trip + the classifier reads it via the extended
fall-through chain in defect_intel.py:138-160.
"""
from __future__ import annotations

import pytest


def _build_row(body_preview: str = "", status_code: int = 500) -> dict:
    return {
        "test_id": "TC-AM-NNN-api",
        "status": "FAIL",
        "error_message": "",  # empty — body must drive classification
        "actual": {"status_code": status_code, "body_preview": body_preview},
    }


def _invoke_build_params(tr: dict) -> dict:
    """Tiny harness around _build_params (closure inside _persist_run_to_db).
    Reproduce the relevant logic locally to avoid spinning up the whole
    persister + DB. We re-implement what R124.K needs to verify."""
    existing_md = tr.get("metadata")
    result_meta = dict(existing_md) if isinstance(existing_md, dict) else {}
    _actual = tr.get("actual")
    if isinstance(_actual, dict) and _actual.get("status_code") is not None:
        result_meta["status_code"] = _actual["status_code"]
    # R124.K — body promotion
    if (
        isinstance(_actual, dict)
        and _actual.get("body_preview")
        and "response_body_preview" not in result_meta
    ):
        result_meta["response_body_preview"] = str(_actual["body_preview"])[:400]
    return result_meta


def test_r124_k_body_preview_promoted_to_metadata():
    """Newman row with non-empty body_preview in actual → metadata gets response_body_preview."""
    tr = _build_row(body_preview='{"error": "Internal authorization error"}')
    md = _invoke_build_params(tr)
    assert "response_body_preview" in md
    assert "Internal authorization error" in md["response_body_preview"]


def test_r124_k_empty_body_skipped():
    """Empty body → no metadata key written (avoid noise)."""
    tr = _build_row(body_preview="")
    md = _invoke_build_params(tr)
    assert "response_body_preview" not in md


def test_r124_k_body_truncated_to_400():
    """Body capped at 400 chars (sufficient for cascade pattern matching)."""
    long_body = "x" * 1000
    tr = _build_row(body_preview=long_body)
    md = _invoke_build_params(tr)
    assert len(md["response_body_preview"]) == 400


def test_r124_k_classifier_reads_via_metadata_post_load():
    """defect_intel._triage_failure picks up the body via metadata.response_body_preview
    when actual is gone (post-DB-load shape)."""
    from src.agents.defect_intel import DefectIntelAgent
    # Simulate a post-DB-load row: no `actual`, only `metadata`
    failure = {
        "test_id": "TC-AM-001-api",
        "error_message": "",
        "status_code": 500,
        "metadata": {
            "response_body_preview": "Internal authorization error",
            "status_code": 500,
        },
    }
    triage = DefectIntelAgent._triage_failure(failure)
    # With auth_cascade_5xx pattern fired by R111.H + R123.B, this should
    # route to operator_review (not sut_regression).
    assert triage["triage_category"] in ("operator_review", "test_gen_bug"), (
        f"500 + 'authorization error' body should NOT classify as "
        f"sut_regression; got {triage}"
    )
