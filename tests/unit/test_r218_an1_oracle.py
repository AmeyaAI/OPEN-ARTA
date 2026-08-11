"""AN1 (R218) — the independent ground-truth oracle (`compute_aggregate`) computes
the answer over the GENERATED rows, pure-Python + deterministic. This is the
"computed answer" a manual tester would derive; AN4 verifies the SUT's answer
against it on the same controlled data.
"""
from __future__ import annotations

from src.agents.recipe_verifier import compute_aggregate

_ROWS = [
    {"revenue": 10, "region": "north"},
    {"revenue": 20, "region": "south"},
    {"revenue": 30, "region": "north"},
    {"revenue": 40, "region": "north"},
]


def test_an1_count():
    assert compute_aggregate(_ROWS, "count") == 4


def test_an1_sum_avg_min_max():
    assert compute_aggregate(_ROWS, "sum", "revenue") == 100.0
    assert compute_aggregate(_ROWS, "avg", "revenue") == 25.0
    assert compute_aggregate(_ROWS, "min", "revenue") == 10.0
    assert compute_aggregate(_ROWS, "max", "revenue") == 40.0


def test_an1_top_category():
    assert compute_aggregate(_ROWS, "top_category", "region") == "north"  # 3 of 4


def test_an1_percentile_median():
    # [10,20,30,40] median (p50) interpolated = 25.0
    assert compute_aggregate(_ROWS, "percentile", "revenue", percentile=50) == 25.0
    assert compute_aggregate(_ROWS, "max", "revenue") == compute_aggregate(
        _ROWS, "percentile", "revenue", percentile=100)


def test_an1_filtered():
    assert compute_aggregate(_ROWS, "count", filter_col="region", filter_val="north") == 3
    assert compute_aggregate(_ROWS, "sum", "revenue", filter_col="region", filter_val="north") == 80.0


def test_an1_empty_or_unknown_safe():
    assert compute_aggregate([], "sum", "revenue") is None
    assert compute_aggregate(_ROWS, "stddev", "revenue") is None  # unsupported → None, no crash
