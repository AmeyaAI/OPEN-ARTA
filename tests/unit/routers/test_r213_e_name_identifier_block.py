"""R213.E — `*_name` resource selectors must NOT be fabricated with R43
synthetic values (which 500 on the SUT as false sut_regressions); they stay
unresolved so the referencing item gets a truthful per-item BLOCK.

Root cause (run-b31de9): req_am_008/015 storage GETs hit
`/api/storage/.../{container_name}` → R43 filled `arta-synthetic-container_name`
→ SUT 500. R170 already declined to fake `*_id`/`*_uuid`/`*_path`; R213.E
extends that to `*_name` resource selectors, while still allowing genuinely
free-form names (display/file/user names) where any value is a valid input.
"""
from __future__ import annotations

from src.api.routers.execution import _resolve_blocked_var_defaults

_PID = "a1b2c3d4-5678-4ef0-abcd-1234567890ab"


def test_resource_name_selectors_are_not_fabricated():
    out = _resolve_blocked_var_defaults(
        _PID, {"container_name", "schema_name", "dataset_name"},
    )
    # none of these resource selectors should get a synthetic value
    assert "container_name" not in out
    assert "schema_name" not in out
    assert "dataset_name" not in out


def test_freeform_names_still_synthesized():
    out = _resolve_blocked_var_defaults(
        _PID, {"display_name", "file_name", "username"},
    )
    # free-form names are valid inputs (not resource selectors) → still filled
    assert out.get("display_name", "").startswith("arta-synthetic-")
    assert out.get("file_name", "").startswith("arta-synthetic-")
    assert out.get("username", "").startswith("arta-synthetic-")


def test_ids_still_blocked_and_generic_still_synthesized():
    out = _resolve_blocked_var_defaults(
        _PID, {"collection_id", "page_size", "report_path"},
    )
    assert "collection_id" not in out      # R170 id block
    assert "report_path" not in out        # R170 path block
    # page_size has no R43 synthetic id-shape → may stay out; assert no fake id
    assert not str(out.get("page_size", "")).startswith("arta-synthetic-")


def test_killswitch_restores_name_synthesis(monkeypatch):
    monkeypatch.setenv("ARTA_R170_BLOCK_SYNTHETIC_IDS_DISABLE", "1")
    out = _resolve_blocked_var_defaults(_PID, {"container_name"})
    # with R170/R213.E off, the legacy R43 synthetic comes back
    assert out.get("container_name") == "arta-synthetic-container_name"
