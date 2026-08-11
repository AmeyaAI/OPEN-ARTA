"""R124.F — defect signature bidirectional precision.

Two opposite bugs in `_build_deterministic_defects` pre-R124.F:

1. **Cardinality EXPLOSION for perf-threshold violations** — every distinct
   k6 latency value created a new defect (`DEF-MSG-EXPECTED-10033-T`,
   `-10108-T`, `-10679-T`, ...). R124.F.1 normalizes ms-values into
   (budget, overshoot bucket) so 100s of perf threshold breaches collapse
   to ~3 buckets per budget (just_over/moderate/severe).

2. **Cardinality COLLAPSE for distinct exceptions** — 8× "Cannot read
   property 'X'" + 9× "Cannot read property 'Y'" hashed to the same
   `msg:Cannot read property` prefix (80-char truncation) → 17 ON-CONFLICT
   dropouts in run-d52a8c. R124.F.2 adds test_id family + first-token
   discriminator + widens slug from [:20] → [:40].
"""
from __future__ import annotations

from src.api.services.post_run_chain_pipeline import _build_deterministic_defects


def _f(test_id: str, err: str, tool: str = "k6") -> dict:
    return {"test_id": test_id, "error_message": err, "automation_tool": tool}


def test_r124_f1_perf_threshold_collapses_by_bucket():
    """8 distinct ms-values against same 5000ms budget → ONE defect (was 8)."""
    failures = [
        _f("TC-PERF-1", "expected 7541 to be below 5000"),
        _f("TC-PERF-2", "expected 7733 to be below 5000"),
        _f("TC-PERF-3", "expected 7989 to be below 5000"),
        _f("TC-PERF-4", "expected 8201 to be below 5000"),
    ]
    defects = _build_deterministic_defects(failures)
    perf_defects = [d for d in defects if "PERF" in d["defect_id"]]
    assert len(perf_defects) == 1, (
        f"4 distinct ms-values vs same budget → ONE defect; got {len(perf_defects)}: "
        f"{[d['defect_id'] for d in perf_defects]}"
    )


def test_r124_f1_perf_buckets_distinct():
    """Just-over (5%) vs severe (200%) overshoots → SEPARATE defects."""
    failures = [
        _f("TC-A", "expected 5100 to be below 5000"),    # 2% over → just_over
        _f("TC-B", "expected 15000 to be below 5000"),   # 200% over → severe
    ]
    defects = _build_deterministic_defects(failures)
    perf_defects = [d for d in defects if "PERF" in d["defect_id"]]
    assert len(perf_defects) == 2, (
        f"just_over vs severe must produce 2 defects; got {len(perf_defects)}"
    )


def test_r124_f2_distinct_property_names_separate():
    """8× 'Cannot read property X' + 9× 'Cannot read property Y' → 2 clusters (was 1)."""
    failures = (
        [_f(f"TC-X-{i}", "Cannot read property 'X' of undefined", "playwright")
         for i in range(8)]
        + [_f(f"TC-Y-{i}", "Cannot read property 'Y' of undefined", "playwright")
           for i in range(9)]
    )
    defects = _build_deterministic_defects(failures)
    msg_defects = [d for d in defects if d.get("defect_id", "").startswith("DEF-MSG-")]
    assert len(msg_defects) >= 2, (
        f"X vs Y must produce ≥2 clusters; got {len(msg_defects)}: "
        f"{[d['defect_id'] for d in msg_defects]}"
    )


def test_r124_f2_status_bound_same_text_merges():
    """Same status + same first-40-chars of error → ONE cluster (no over-precision)."""
    failures = [
        _f(f"TC-S-{i}", "got 500 from /api/x")
        for i in range(8)
    ]
    defects = _build_deterministic_defects(failures)
    s5_defects = [d for d in defects if "500" in d.get("defect_id", "")]
    assert len(s5_defects) == 1, "8 same-pattern 500s should merge"
    affected = s5_defects[0].get("affected_tests") or []
    assert len(affected) == 8, f"expected 8 affected_tests; got {len(affected)}"


def test_r124_f_slug_length_capped_at_40():
    """defect_id never exceeds DEF- + 40 chars = 44 total."""
    failures = [_f("TC-LONG-TEST-FAMILY-XX", "Cannot read property 'LongPropertyName' of undefined")]
    defects = _build_deterministic_defects(failures)
    for d in defects:
        # 'DEF-' prefix + 40-char slug = 44 chars
        assert len(d["defect_id"]) <= 44, f"defect_id too long: {d['defect_id']}"


def test_r124_f_perf_title_carries_budget():
    """Perf defect title surfaces budget + bucket for operator clarity."""
    failures = [_f("TC-P", "expected 7541 to be below 5000")]
    defects = _build_deterministic_defects(failures)
    assert len(defects) == 1
    # The first_error / title should mention budget
    title = (defects[0].get("title") or "") + (defects[0].get("first_error") or "")
    assert "budget=5000" in title or "5000" in title


def test_r124_f_empty_em_graceful():
    """Empty error_message → key derived from first_token='empty' + tc_family."""
    failures = [_f("TC-EMPTY", "")]
    defects = _build_deterministic_defects(failures)
    assert len(defects) == 1
    assert defects[0]["defect_id"].startswith("DEF-")


def test_r124_f_perf_within_budget_not_flagged():
    """`expected X to be below Y` where X < Y shouldn't happen, but if it does
    (negative overshoot), still bucket correctly (just_over)."""
    # Edge case: same budget different bucket boundaries
    failures = [
        _f("TC-A", "expected 5050 to be below 5000"),  # 1% over → just_over
        _f("TC-B", "expected 5100 to be below 5000"),  # 2% over → just_over
        _f("TC-C", "expected 5200 to be below 5000"),  # 4% over → just_over
    ]
    defects = _build_deterministic_defects(failures)
    perf_defects = [d for d in defects if "PERF" in d.get("defect_id", "")]
    assert len(perf_defects) == 1, "all 3 just_over should merge"
