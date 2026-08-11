"""
ARTA Test Quality Validator — BMAD TEA Definition of Done

Validates generated test scripts against 8 mandatory quality criteria:
  TQ-001  No hard waits (sleep, waitForTimeout, time.sleep)
  TQ-002  No conditionals in test flow (if/else for flow control)
  TQ-003  Concise (<300 lines)
  TQ-004  Fast (<90 seconds / 1.5 minutes)
  TQ-005  Self-cleaning (afterEach/teardown/cleanup present)
  TQ-006  Visible assertions (expect in test body, not hidden in helpers)
  TQ-007  Unique data (faker/uuid usage, no hardcoded IDs)
  TQ-008  Parallel-safe (no shared mutable state)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Violation:
    code: str          # TQ-001 through TQ-008
    rule: str          # Short rule name
    severity: str      # error | warning
    message: str       # Human-readable description
    line: int | None = None
    snippet: str = ""


@dataclass
class TestQualityResult:
    violations: list[Violation] = field(default_factory=list)
    score: int = 100       # 0-100 quality score
    passed: bool = True    # True if no error-severity violations
    criteria_results: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "violation_count": len(self.violations),
            "violations": [
                {
                    "code": v.code,
                    "rule": v.rule,
                    "severity": v.severity,
                    "message": v.message,
                    "line": v.line,
                    "snippet": v.snippet,
                }
                for v in self.violations
            ],
            "criteria": self.criteria_results,
        }


# Regex patterns for each rule
_HARD_WAIT_PATTERNS = [
    r"\bsleep\s*\(",
    r"\bwaitForTimeout\s*\(",
    r"\btime\.sleep\s*\(",
    r"\bThread\.sleep\s*\(",
    r"\bsetTimeout\s*\(",
    r"\bawait\s+new\s+Promise.*setTimeout",
    r"\bpage\.waitForTimeout\s*\(",
]

_CONDITIONAL_FLOW_PATTERNS = [
    r"^\s*if\s*\(.+\)\s*\{",         # if (...) { in test body
    r"^\s*\}\s*else\s*\{",           # } else {
    r"^\s*if\s+.+:",                 # Python if in test body
    r"^\s*else\s*:",                 # Python else
]

_HARDCODED_ID_PATTERNS = [
    r'["\']user[-_]?[0-9]+["\']',
    r'["\']id[-_]?[0-9]+["\']',
    r'["\'][0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}["\']',  # hardcoded UUIDs
]

_FAKER_UNIQUE_PATTERNS = [
    r"\bfaker\b",
    r"\buuid\b",
    r"\brandomUUID\b",
    r"\bfake\.\w+",
    r"\bcrypto\.randomUUID",
    r"\buuidv4\b",
    r"\bgenerate_\w+",
]

_CLEANUP_PATTERNS = [
    r"\bafterEach\s*\(",
    r"\bafterAll\s*\(",
    r"\bteardown\b",
    r"\bcleanup\b",
    r"\bfinally\s*:",
    r"\baddCleanup\b",
    r"\byield\b.*\bfixture\b",  # pytest yield fixture
]

_ASSERTION_PATTERNS = [
    r"\bexpect\s*\(",
    r"\bassert\s+",
    r"\bassertEqual\s*\(",
    r"\bassertTrue\s*\(",
    r"\bshould\b",
    r"\bto_be\b",
    r"\bto_equal\b",
    r"\bto_have\b",
]

_SHARED_STATE_PATTERNS = [
    r"\bglobal\s+\w+",
    r"\blet\s+\w+\s*=",          # mutable let at module scope
    r"\bvar\s+\w+\s*=",          # var at module scope
    r"^\w+\s*=\s*\[\]",          # module-level list mutation
    r"^\w+\s*=\s*\{\}",          # module-level dict mutation
]

_HARDCODED_URL_PATTERNS = [
    r"""['"]https?://localhost[:\d]*['"]""",
    r"""['"]https?://127\.0\.0\.1[:\d]*['"]""",
    r"""['"]https?://[a-zA-Z0-9.-]+\.(com|io|net|org)[^'"]*['"]""",
]

_ACCEPTABLE_URL_PATTERNS = [
    r"process\.env\.\w+",
    r"TARGET_BASE_URL",
    r"\bAPI\b\s*[+/]",           # Using API constant
    r"`\$\{.*\}/",               # Template literal with variable
]


class TestQualityValidator:
    """
    BMAD TEA Test Quality Definition of Done validator.
    Runs regex-based static analysis against 8 mandatory rules.
    """

    MAX_LINES = 300
    MAX_DURATION_S = 90

    def validate(
        self,
        script_content: str,
        avg_duration_ms: int | None = None,
        acceptance_criteria: list[str] | None = None,
        api_endpoints: list[str] | None = None,
    ) -> TestQualityResult:
        """Validate a test script against all BMAD TEA quality criteria.

        Args:
            script_content: The test script source code.
            avg_duration_ms: Average execution duration (if known).
            acceptance_criteria: List of AC statements to check coverage for.
            api_endpoints: List of API endpoint paths to check coverage for.
        """
        lines = script_content.split("\n")
        result = TestQualityResult()
        criteria = {}

        # TQ-001: No hard waits
        v1 = self._check_hard_waits(lines)
        criteria["TQ-001"] = {"rule": "No hard waits", "passed": len(v1) == 0}
        result.violations.extend(v1)

        # TQ-002: No conditionals in test flow
        v2 = self._check_conditionals(lines)
        criteria["TQ-002"] = {"rule": "No conditionals in flow", "passed": len(v2) == 0}
        result.violations.extend(v2)

        # TQ-003: Concise (<300 lines)
        v3 = self._check_length(lines)
        criteria["TQ-003"] = {"rule": f"Concise (<{self.MAX_LINES} lines)", "passed": len(v3) == 0}
        result.violations.extend(v3)

        # TQ-004: Fast (<90s)
        v4 = self._check_duration(avg_duration_ms)
        criteria["TQ-004"] = {"rule": f"Fast (<{self.MAX_DURATION_S}s)", "passed": len(v4) == 0}
        result.violations.extend(v4)

        # TQ-005: Self-cleaning
        v5 = self._check_self_cleaning(lines)
        criteria["TQ-005"] = {"rule": "Self-cleaning", "passed": len(v5) == 0}
        result.violations.extend(v5)

        # TQ-006: Visible assertions (min 2 per test)
        v6 = self._check_visible_assertions(lines)
        criteria["TQ-006"] = {"rule": "Visible assertions", "passed": len(v6) == 0}
        result.violations.extend(v6)

        # TQ-007: Unique data
        v7 = self._check_unique_data(lines)
        criteria["TQ-007"] = {"rule": "Unique data", "passed": len(v7) == 0}
        result.violations.extend(v7)

        # TQ-008: Parallel-safe
        v8 = self._check_parallel_safe(lines)
        criteria["TQ-008"] = {"rule": "Parallel-safe", "passed": len(v8) == 0}
        result.violations.extend(v8)

        # TQ-009: No hardcoded URLs
        v9 = self._check_hardcoded_urls(lines)
        criteria["TQ-009"] = {"rule": "No hardcoded URLs", "passed": len(v9) == 0}
        result.violations.extend(v9)

        # TQ-010: Minimum assertions per test (at least 2)
        v10 = self._check_min_assertions_per_test(lines)
        criteria["TQ-010"] = {"rule": "Min 2 assertions per test", "passed": len(v10) == 0}
        result.violations.extend(v10)

        # TQ-011: Acceptance criteria coverage (if ACs provided)
        if acceptance_criteria:
            v11 = self._check_ac_coverage(lines, acceptance_criteria)
            criteria["TQ-011"] = {"rule": "AC coverage", "passed": len(v11) == 0}
            result.violations.extend(v11)

        # TQ-012: API endpoint coverage — positive + negative (if endpoints provided)
        if api_endpoints:
            v12 = self._check_api_endpoint_coverage(lines, api_endpoints)
            criteria["TQ-012"] = {"rule": "API endpoint coverage", "passed": len(v12) == 0}
            result.violations.extend(v12)

        # TQ-013: No shared state between test.describe blocks
        v13 = self._check_no_shared_describe_state(lines)
        criteria["TQ-013"] = {"rule": "No shared state between describes", "passed": len(v13) == 0}
        result.violations.extend(v13)

        # Score: 100 proportionally divided across active criteria
        total_criteria = len(criteria)
        points_per = 100.0 / total_criteria if total_criteria else 100
        failed_criteria = sum(1 for c in criteria.values() if not c["passed"])
        result.score = max(0, int(100 - failed_criteria * points_per))
        result.passed = all(
            v.severity != "error" for v in result.violations
        )
        result.criteria_results = criteria

        return result

    # ── Individual Checks ──────────────────────────────────────────────────

    def _check_hard_waits(self, lines: list[str]) -> list[Violation]:
        violations = []
        for i, line in enumerate(lines, 1):
            for pattern in _HARD_WAIT_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    violations.append(Violation(
                        code="TQ-001",
                        rule="No hard waits",
                        severity="error",
                        message=f"Hard wait detected: {line.strip()!r}. Use explicit waits (expect, waitForSelector) instead.",
                        line=i,
                        snippet=line.strip(),
                    ))
                    break
        return violations

    def _check_conditionals(self, lines: list[str]) -> list[Violation]:
        violations = []
        # Only flag conditionals inside test bodies, not in page objects/helpers
        in_test = False
        for i, line in enumerate(lines, 1):
            if re.search(r"\btest\s*\(|def\s+test_|it\s*\(|describe\s*\(", line):
                in_test = True
            if in_test:
                for pattern in _CONDITIONAL_FLOW_PATTERNS:
                    if re.search(pattern, line):
                        violations.append(Violation(
                            code="TQ-002",
                            rule="No conditionals in test flow",
                            severity="warning",
                            message="Conditional in test body — tests should follow a single deterministic path.",
                            line=i,
                            snippet=line.strip(),
                        ))
                        break
        return violations

    def _check_length(self, lines: list[str]) -> list[Violation]:
        if len(lines) > self.MAX_LINES:
            return [Violation(
                code="TQ-003",
                rule="Concise",
                severity="warning",
                message=f"Test file is {len(lines)} lines (max {self.MAX_LINES}). Split into smaller focused tests.",
            )]
        return []

    def _check_duration(self, avg_duration_ms: int | None) -> list[Violation]:
        if avg_duration_ms is not None and avg_duration_ms > self.MAX_DURATION_S * 1000:
            return [Violation(
                code="TQ-004",
                rule="Fast",
                severity="warning",
                message=f"Average duration {avg_duration_ms}ms exceeds {self.MAX_DURATION_S}s limit.",
            )]
        return []

    def _check_self_cleaning(self, lines: list[str]) -> list[Violation]:
        content = "\n".join(lines)
        for pattern in _CLEANUP_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                return []
        return [Violation(
            code="TQ-005",
            rule="Self-cleaning",
            severity="error",
            message="No cleanup/teardown found. Tests must clean up after themselves (afterEach, teardown, cleanup).",
        )]

    def _check_visible_assertions(self, lines: list[str]) -> list[Violation]:
        content = "\n".join(lines)
        for pattern in _ASSERTION_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                return []
        return [Violation(
            code="TQ-006",
            rule="Visible assertions",
            severity="error",
            message="No visible assertions found in test body. Tests must have explicit expect/assert statements.",
        )]

    def _check_unique_data(self, lines: list[str]) -> list[Violation]:
        content = "\n".join(lines)
        has_faker = any(
            re.search(p, content, re.IGNORECASE) for p in _FAKER_UNIQUE_PATTERNS
        )
        has_hardcoded = any(
            re.search(p, content) for p in _HARDCODED_ID_PATTERNS
        )
        violations = []
        if has_hardcoded and not has_faker:
            violations.append(Violation(
                code="TQ-007",
                rule="Unique data",
                severity="warning",
                message="Hardcoded IDs detected without faker/uuid usage. Use dynamic data generation.",
            ))
        if not has_faker and not has_hardcoded:
            # No data generation at all — might be fine for simple tests
            pass
        return violations

    def _check_parallel_safe(self, lines: list[str]) -> list[Violation]:
        violations = []
        # Check first 20 lines (module scope) for mutable shared state
        module_scope = lines[:20]
        for i, line in enumerate(module_scope, 1):
            for pattern in _SHARED_STATE_PATTERNS:
                if re.search(pattern, line):
                    violations.append(Violation(
                        code="TQ-008",
                        rule="Parallel-safe",
                        severity="warning",
                        message=f"Potential shared mutable state at module scope: {line.strip()!r}",
                        line=i,
                        snippet=line.strip(),
                    ))
                    break
        return violations

    # ── Wave 6 Extended Checks ─────────────────────────────────────────────

    def _check_hardcoded_urls(self, lines: list[str]) -> list[Violation]:
        """TQ-009: No hardcoded URLs — should use process.env.TARGET_BASE_URL."""
        content = "\n".join(lines)
        # If the script uses env vars / API constant, hardcoded URLs are acceptable as defaults
        uses_env = any(re.search(p, content) for p in _ACCEPTABLE_URL_PATTERNS)
        if uses_env:
            return []

        violations = []
        for i, line in enumerate(lines, 1):
            for pattern in _HARDCODED_URL_PATTERNS:
                if re.search(pattern, line):
                    violations.append(Violation(
                        code="TQ-009",
                        rule="No hardcoded URLs",
                        severity="warning",
                        message=f"Hardcoded URL detected. Use `process.env.TARGET_BASE_URL` or an API constant instead.",
                        line=i,
                        snippet=line.strip(),
                    ))
                    break
        return violations

    def _check_min_assertions_per_test(self, lines: list[str]) -> list[Violation]:
        """TQ-010: Every test must have at least 2 explicit expect()/assert calls."""
        violations = []
        current_test_name = None
        current_test_start = 0
        assertion_count = 0

        for i, line in enumerate(lines, 1):
            # Detect test block start
            test_match = re.search(r"""(?:test|it)\s*\(\s*['"](.+?)['"]""", line)
            if test_match:
                # Check previous test if any
                if current_test_name is not None and assertion_count < 2:
                    violations.append(Violation(
                        code="TQ-010",
                        rule="Min 2 assertions per test",
                        severity="warning",
                        message=f"Test '{current_test_name}' has only {assertion_count} assertion(s) — minimum 2 required.",
                        line=current_test_start,
                        snippet=current_test_name,
                    ))
                current_test_name = test_match.group(1)
                current_test_start = i
                assertion_count = 0
                continue

            # Count assertions inside tests
            if current_test_name is not None:
                for pattern in _ASSERTION_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        assertion_count += 1
                        break

        # Check last test
        if current_test_name is not None and assertion_count < 2:
            violations.append(Violation(
                code="TQ-010",
                rule="Min 2 assertions per test",
                severity="warning",
                message=f"Test '{current_test_name}' has only {assertion_count} assertion(s) — minimum 2 required.",
                line=current_test_start,
                snippet=current_test_name,
            ))

        return violations

    def _check_ac_coverage(
        self,
        lines: list[str],
        acceptance_criteria: list[str],
    ) -> list[Violation]:
        """TQ-011: Every acceptance criterion should have at least one corresponding test."""
        content = "\n".join(lines).lower()
        violations = []
        for ac in acceptance_criteria:
            # Extract key words from AC statement (skip very short words)
            keywords = [w.lower() for w in re.findall(r"\b\w{4,}\b", ac)]
            # Check if at least half the keywords appear in the test content
            if keywords:
                matched = sum(1 for kw in keywords if kw in content)
                coverage_ratio = matched / len(keywords)
                if coverage_ratio < 0.3:
                    violations.append(Violation(
                        code="TQ-011",
                        rule="AC coverage",
                        severity="warning",
                        message=f"Acceptance criterion may not be covered: '{ac[:80]}...' — low keyword match ({matched}/{len(keywords)}).",
                    ))
        return violations

    def _check_api_endpoint_coverage(
        self,
        lines: list[str],
        api_endpoints: list[str],
    ) -> list[Violation]:
        """TQ-012: Every API endpoint mentioned should have positive + negative tests."""
        content = "\n".join(lines)
        violations = []
        for endpoint in api_endpoints:
            # Normalize endpoint for search (strip leading slash)
            ep_pattern = re.escape(endpoint.strip("/"))
            occurrences = len(re.findall(ep_pattern, content, re.IGNORECASE))
            if occurrences == 0:
                violations.append(Violation(
                    code="TQ-012",
                    rule="API endpoint coverage",
                    severity="warning",
                    message=f"API endpoint '{endpoint}' has no tests. Add positive and negative test cases.",
                ))
            elif occurrences < 2:
                violations.append(Violation(
                    code="TQ-012",
                    rule="API endpoint coverage",
                    severity="warning",
                    message=f"API endpoint '{endpoint}' appears only once — add both positive and negative tests.",
                ))
        return violations

    def _check_no_shared_describe_state(self, lines: list[str]) -> list[Violation]:
        """TQ-013: No mutable variable declarations between test.describe blocks."""
        violations = []
        in_describe = False
        describe_depth = 0

        for i, line in enumerate(lines, 1):
            # Track describe block boundaries
            if re.search(r"\b(?:test\.)?describe\s*\(", line):
                in_describe = True
                describe_depth += 1
            if in_describe:
                describe_depth += line.count("{") - line.count("}")
                if describe_depth <= 0:
                    in_describe = False
                    describe_depth = 0

            # Check for mutable declarations outside describe blocks
            if not in_describe and describe_depth == 0:
                # Skip imports, const declarations, and type definitions
                stripped = line.strip()
                if not stripped or stripped.startswith("//") or stripped.startswith("import "):
                    continue
                if re.search(r"^\s*const\s+", line):
                    continue
                # Flag let/var at file scope between describes
                if re.search(r"^\s*(?:let|var)\s+\w+\s*=", line):
                    violations.append(Violation(
                        code="TQ-013",
                        rule="No shared state between describes",
                        severity="warning",
                        message=f"Mutable variable at file scope may cause shared state between test.describe blocks: {stripped!r}",
                        line=i,
                        snippet=stripped,
                    ))
        return violations
