"""R114.F — adversarial pytest tier-filter visibility.

Pre-R114.F.2: tier-filtered SKIPs had `skip_reason` as a TOP-LEVEL key
which the DB persistence layer dropped → operator dashboard showed
empty metadata → operator perceived adversarial tests as "not executing"
even though tier filtering is intentional ("tier3 stays nightly-only").

R114.F.2 surfaces:
  - metadata.skip_reason = "tier_filter_excluded"
  - metadata.skip_detail (human-readable)
  - metadata.test_kind ("adversarial" | "layer" | "extraction" | "other")
  - metadata.tier_filter + suite_type for context

R114.F.3 frontend tile renders `tier_filter_excluded` with operator CTA
"Run suite_type=regression or full to include".
"""
from __future__ import annotations

import re
from pathlib import Path


_EXECUTION_PY = Path(__file__).resolve().parents[3] / "src" / "api" / "routers" / "execution.py"
_FRONTEND_TSX = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "app" / "run-history" / "RunDetailContent.tsx"
)


def test_r114f_skip_reason_in_metadata_not_toplevel():
    """Source check: tier-mismatch SKIPs put skip_reason INSIDE metadata."""
    content = _EXECUTION_PY.read_text()
    # Anchored regex: find the exact `if not _spec_matches_tier(spec, tier_expr):`
    # branch and verify it puts skip_reason inside metadata (R114.F.2)
    branch_start = content.find("if not _spec_matches_tier(spec, tier_expr):")
    assert branch_start > 0, "tier-mismatch branch not found"
    # Window of ~2000 chars covers the SKIP append + return
    window = content[branch_start:branch_start + 2000]
    # skip_reason="tier_filter_excluded" must appear (inside metadata dict)
    assert '"skip_reason": "tier_filter_excluded"' in window, (
        f"R114.F.2: skip_reason='tier_filter_excluded' not found in branch"
    )
    assert '"test_kind":' in window, (
        f"R114.F.2: test_kind classification missing"
    )
    # The top-level `skip_reason` shape from pre-R114.F.2 should NOT exist
    # in this window (no `f"tier_mismatch (suite=` pattern at top level
    # outside the metadata dict).
    pre_r114_pattern = re.compile(r'^\s+"skip_reason":\s*f"tier_mismatch', re.MULTILINE)
    assert not pre_r114_pattern.search(window), (
        "R114.F.2 regression: top-level skip_reason still uses pre-R114 shape"
    )


def test_r114f_test_kind_classification():
    """Source check: test_kind logic classifies adversarial/layer/extraction."""
    content = _EXECUTION_PY.read_text()
    # _test_kind branch should reference all 4 categories
    assert "adversarial" in content
    assert "_nl_to_query" in content or "nl_to_query" in content
    assert "_query_to_result" in content or "query_to_result" in content
    assert "_result_to_insight" in content or "result_to_insight" in content


def test_r114f_frontend_tier_filter_excluded_tile_present():
    """Source check: frontend BLOCKED_REASON_COPY has tier_filter_excluded entry."""
    content = _FRONTEND_TSX.read_text()
    pattern = re.compile(
        r"tier_filter_excluded:\s*\{[^}]*?title:[^,]*deferred",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R114.F.3: frontend BLOCKED_REASON_COPY missing tier_filter_excluded tile"
    )


def test_r114f_frontend_cta_mentions_regression_or_full():
    """Source check: CTA for tier_filter_excluded mentions regression/full suite."""
    content = _FRONTEND_TSX.read_text()
    # Find the tier_filter_excluded block
    pattern = re.compile(
        r"tier_filter_excluded:\s*\{[^}]*?cta:[^,]*regression",
        re.DOTALL,
    )
    assert pattern.search(content), (
        "R114.F.3: CTA missing regression/full suite reference"
    )
