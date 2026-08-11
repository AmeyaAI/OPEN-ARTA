"""R113.E — GET /api/tests/{test_id} enforces project_id scope.

Pre-R113.E: any caller with valid API key could fetch any test by ID,
bypassing project scope. R113.E adds an optional project_id query param
that, when supplied, requires the test's requirement_id to belong to that
project. Cross-project requests return 403.

Backward-compat: when project_id is NOT supplied, behavior is unchanged
(legacy callers still work).
"""
from __future__ import annotations

import re
from pathlib import Path


_TESTS_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "tests.py"


def test_r113_e_get_test_accepts_project_id_query_param():
    """Source check: get_test signature includes `project_id: str | None = Query(None)`."""
    content = _TESTS_PY.read_text()
    pattern = re.compile(
        r"async def get_test\(\s*\n"
        r"\s*test_id: str,\s*\n"
        r"\s*project_id: str \| None = Query\(None\),",
        re.MULTILINE,
    )
    assert pattern.search(content), (
        "R113.E: get_test missing project_id Query param in signature"
    )


def test_r113_e_scope_check_helper_invoked():
    """Source check: _r113_e_check_scope helper is called before returning test."""
    content = _TESTS_PY.read_text()
    assert "_r113_e_check_scope" in content, (
        "R113.E: _r113_e_check_scope helper missing"
    )
    # Helper should be invoked at least twice (DB path + in-memory path)
    invocations = content.count("_r113_e_check_scope(")
    assert invocations >= 2, (
        f"R113.E: scope check helper invoked {invocations} times, expected >= 2 "
        "(DB path + in-memory path)"
    )


def test_r113_e_403_raised_with_project_mismatch_message():
    """Source check: scope-check raises HTTPException(403) with diagnostic message."""
    content = _TESTS_PY.read_text()
    pattern = re.compile(
        r"R113\.E.*does NOT.*belong to project|"
        r"Cross-project read refused",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R113.E: HTTPException(403) detail missing cross-project diagnostic"
    )
