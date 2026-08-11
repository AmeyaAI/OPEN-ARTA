"""R113.H — paste-trust meta sidecar write failures propagate as HTTP 500.

Pre-R113.H: when `meta_path.write_text(...)` failed (read-only FS, perm
denied, etc.), the exception was swallowed with `log.warning` and the
R45.2 endpoint returned 200 OK. The operator believed paste succeeded
but auth-setup.ts then ran without the paste-trust marker → next R45.3
probe wiped the storage state → smoke later failed with auth_failure.
The truth was hidden from the operator at a 1-line `log.warning` deep
in the logs.

R113.H raises HTTPException(500) with a diagnostic message describing
WHAT failed + actionable filesystem permission CTA, so the operator
sees the failure immediately + can fix it.
"""
from __future__ import annotations

import re
from pathlib import Path


_PROJECTS_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "projects.py"


def test_r113_h_paste_trust_error_raises_httpexception():
    """Source check: R112.A.5 try/except raises HTTPException on write failure."""
    content = _PROJECTS_PY.read_text()

    # Locate the R112.A.5 except clause
    except_pattern = re.compile(
        r"except Exception as _r112_a5_exc:\s*\n"
        r".*R113\.H.*\n"
        r".*log\.(error|exception)\(.*R113\.H.*meta sidecar write FAILED",
        re.MULTILINE | re.DOTALL,
    )
    assert except_pattern.search(content), (
        "R113.H: R112.A.5 except clause does NOT contain R113.H log.error + HTTPException path"
    )


def test_r113_h_diagnostic_message_includes_operator_cta():
    """The 500 response detail includes filesystem-permissions CTA."""
    content = _PROJECTS_PY.read_text()

    # Verify the HTTPException detail string contains the operator CTA keywords
    cta_pattern = re.compile(
        r"HTTPException\(\s*\n?"
        r"\s*status_code=500.*?"
        r"detail=.*?"
        r"(filesystem permissions|\.arta/environments)",
        re.DOTALL,
    )
    assert cta_pattern.search(content), (
        "R113.H: HTTPException(500) detail missing filesystem-perms CTA"
    )
