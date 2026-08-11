"""
ARTA Test Review Agent — Suite-Level Quality & Coverage Analysis

Provides holistic review across a test suite:
  1. Runs TestQualityValidator on every test script
  2. Checks test pyramid balance (unit > integration > E2E)
  3. Detects duplicate coverage (multiple tests covering the same AC)
  4. Returns a structured TestReviewResult with score, violations,
     pyramid analysis, duplicates, and actionable recommendations
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .test_quality_validator import TestQualityValidator, Violation


# ── Ideal Pyramid Percentages ─────────────────────────────────────────────
# BMAD TEA recommended distribution
IDEAL_PYRAMID: dict[str, float] = {
    "unit": 60.0,
    "integration": 25.0,
    "e2e": 15.0,
}

# Map raw test_type values to pyramid tier
_TYPE_MAP: dict[str, str] = {
    "unit": "unit",
    "integration": "integration",
    "api": "integration",
    "contract": "integration",
    "e2e": "e2e",
    "ui": "e2e",
    "performance": "e2e",
    "security": "e2e",
}


@dataclass
class PyramidTier:
    actual_count: int = 0
    actual_pct: float = 0.0
    ideal_pct: float = 0.0
    diff: float = 0.0  # actual - ideal; negative means underweight


@dataclass
class DuplicateCoverage:
    ac_id: str
    test_ids: list[str] = field(default_factory=list)
    count: int = 0


@dataclass
class TestReviewResult:
    quality_score: int = 100          # 0-100 aggregate quality
    violations: list[dict] = field(default_factory=list)
    pyramid_balance: dict[str, dict] = field(default_factory=dict)
    duplicate_coverage: list[dict] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    test_count: int = 0
    per_test_scores: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "quality_score": self.quality_score,
            "test_count": self.test_count,
            "violations": self.violations,
            "pyramid_balance": self.pyramid_balance,
            "duplicate_coverage": self.duplicate_coverage,
            "recommendations": self.recommendations,
            "per_test_scores": self.per_test_scores,
        }


class TestReviewAgent:
    """
    Suite-level test review agent.

    Accepts a list of test dicts, each with:
        test_id, title, test_type, script_content, ac_id, avg_duration_ms

    Returns a TestReviewResult with aggregate quality score, violations,
    pyramid balance analysis, duplicate coverage detection, and recommendations.
    """

    def __init__(self) -> None:
        self._validator = TestQualityValidator()

    def review(self, tests: list[dict]) -> TestReviewResult:
        """Run full suite review and return structured result."""
        result = TestReviewResult(test_count=len(tests))

        if not tests:
            result.quality_score = 0
            result.recommendations.append("No tests provided for review")
            return result

        # ── Step 1: Run quality validation on each test ───────────────
        total_score = 0
        for t in tests:
            test_id = t.get("test_id", "unknown")
            script = t.get("script_content", "")
            duration = t.get("avg_duration_ms")

            if not script:
                result.violations.append({
                    "test_id": test_id,
                    "code": "TR-001",
                    "rule": "Missing script",
                    "severity": "error",
                    "message": f"Test {test_id} has no script_content to validate.",
                })
                result.per_test_scores[test_id] = 0
                continue

            qr = self._validator.validate(script, duration)
            result.per_test_scores[test_id] = qr.score
            total_score += qr.score

            for v in qr.violations:
                result.violations.append({
                    "test_id": test_id,
                    "code": v.code,
                    "rule": v.rule,
                    "severity": v.severity,
                    "message": v.message,
                    "line": v.line,
                    "snippet": v.snippet,
                })

        scored_count = sum(1 for s in result.per_test_scores.values() if s > 0) or 1
        avg_quality = total_score // max(scored_count, 1)

        # ── Step 2: Test pyramid balance ──────────────────────────────
        pyramid_counts: Counter[str] = Counter()
        for t in tests:
            raw_type = (t.get("test_type") or "e2e").lower()
            tier = _TYPE_MAP.get(raw_type, "e2e")
            pyramid_counts[tier] += 1

        total_tests = sum(pyramid_counts.values()) or 1
        pyramid_balance: dict[str, dict] = {}
        for tier_name in ("unit", "integration", "e2e"):
            count = pyramid_counts.get(tier_name, 0)
            actual_pct = round((count / total_tests) * 100, 1)
            ideal_pct = IDEAL_PYRAMID[tier_name]
            diff = round(actual_pct - ideal_pct, 1)
            pyramid_balance[tier_name] = {
                "actual_count": count,
                "actual_pct": actual_pct,
                "ideal_pct": ideal_pct,
                "diff": diff,
            }

        result.pyramid_balance = pyramid_balance

        # Penalise score if pyramid is inverted (more e2e than unit)
        pyramid_penalty = 0
        unit_info = pyramid_balance["unit"]
        e2e_info = pyramid_balance["e2e"]
        if unit_info["actual_count"] <= e2e_info["actual_count"] and total_tests > 2:
            pyramid_penalty = 15
            result.recommendations.append(
                "Add more unit tests — current suite is bottom-heavy "
                f"(unit={unit_info['actual_pct']}% vs e2e={e2e_info['actual_pct']}%)"
            )
        elif unit_info["diff"] < -20:
            pyramid_penalty = 10
            result.recommendations.append(
                f"Add more unit tests — currently {unit_info['actual_pct']}% vs ideal {unit_info['ideal_pct']}%"
            )

        integration_info = pyramid_balance["integration"]
        if integration_info["actual_count"] == 0 and total_tests > 2:
            pyramid_penalty += 5
            result.recommendations.append(
                "Add integration/API tests — none found in the current suite"
            )

        # ── Step 3: Detect duplicate coverage ─────────────────────────
        ac_to_tests: dict[str, list[str]] = {}
        for t in tests:
            ac_id = t.get("ac_id")
            test_id = t.get("test_id", "unknown")
            if ac_id:
                ac_to_tests.setdefault(ac_id, []).append(test_id)

        duplicate_penalty = 0
        for ac_id, test_ids in ac_to_tests.items():
            if len(test_ids) > 1:
                result.duplicate_coverage.append({
                    "ac_id": ac_id,
                    "test_ids": test_ids,
                    "count": len(test_ids),
                })
                result.recommendations.append(
                    f"Remove duplicate coverage for {ac_id} — "
                    f"covered by {len(test_ids)} tests: {', '.join(test_ids)}"
                )
                duplicate_penalty += 3  # 3 points per duplicate cluster

        # ── Final score ───────────────────────────────────────────────
        result.quality_score = max(
            0,
            min(100, avg_quality - pyramid_penalty - duplicate_penalty),
        )

        # Additional recommendations based on overall score
        if result.quality_score < 50:
            result.recommendations.append(
                "Suite quality is below 50 — address error-severity violations before merging"
            )
        elif result.quality_score < 75:
            result.recommendations.append(
                "Suite quality is moderate — review warning-level violations for improvement"
            )

        return result
