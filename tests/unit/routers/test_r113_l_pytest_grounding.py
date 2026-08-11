"""R113.L — pytest grounding-violation BLOCK uses dedicated reason kind.

Pre-R113.L: when R55.3 stamped `ARTA_GROUNDING_FAILED=true` on a pytest
spec, the dispatcher emitted a BLOCKED row with `blocked_reason=
"grounding_violation"`. The frontend BLOCKED_REASON_COPY maps that key
to "Newman gen-quality (grounding violations)" — wrong tool!

R113.L renames the pytest BLOCK reason to `pytest_grounding_violation`
(parallel to `playwright_grounding_violation` from R102.A/C) so the
operator dashboard surfaces a truthful per-tool BLOCK breakdown.
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"
_FRONTEND_TSX = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "app" / "run-history" / "RunDetailContent.tsx"
)


def test_r113_l_dispatcher_uses_pytest_grounding_violation_kind():
    """Source check: pytest dispatcher emits `pytest_grounding_violation`."""
    content = _EXECUTION_PY.read_text()
    # Find the ARTA_GROUNDING_FAILED branch
    branch_pattern = re.compile(
        r'if "ARTA_GROUNDING_FAILED=true" in head:.*?'
        r'"blocked_reason":\s*"pytest_grounding_violation"',
        re.DOTALL,
    )
    assert branch_pattern.search(content), (
        "R113.L: pytest dispatcher does NOT emit `pytest_grounding_violation` reason"
    )


def test_r113_l_no_regression_to_generic_grounding_violation():
    """REGRESSION GUARD: the pytest branch must NOT use the generic reason."""
    content = _EXECUTION_PY.read_text()
    # Locate ARTA_GROUNDING_FAILED block
    branch_match = re.search(
        r'if "ARTA_GROUNDING_FAILED=true" in head:.*?return',
        content, re.DOTALL,
    )
    assert branch_match, "ARTA_GROUNDING_FAILED block not found"
    branch_text = branch_match.group(0)
    # Within this block, no `"blocked_reason": "grounding_violation"` (generic)
    assert '"blocked_reason": "grounding_violation"' not in branch_text, (
        "R113.L REGRESSION: pytest dispatcher still uses generic `grounding_violation`"
    )


def test_r113_l_frontend_tile_for_pytest_grounding_violation_present():
    """Source check: frontend BLOCKED_REASON_COPY has pytest_grounding_violation tile."""
    content = _FRONTEND_TSX.read_text()
    pattern = re.compile(
        r"pytest_grounding_violation:\s*\{[^}]*?title:[^,]*Pytest gen-quality",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R113.L: frontend BLOCKED_REASON_COPY missing pytest_grounding_violation tile"
    )
