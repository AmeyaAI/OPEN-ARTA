"""
ARTA Quality Gate Agent — TEA Layer 9: Evidence-Based Release Decisioning

BMAD TEA 4-outcome gate decisions:
  PASS     — all checks clear
  CONCERNS — risk 6-8 with assigned mitigation owners (not blocking)
  FAIL     — score=9 uncovered, or critical check failed
  WAIVED   — authorized exception with rationale + expiry date
"""
from __future__ import annotations

import logging   # R280 — 11 `log.*` call sites existed with NO import (F821)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

# R280 — module-level logger. ELEVEN `log.debug(...)` call sites had no `log`
# defined, and EVERY ONE sits inside an `except Exception:` handler:
#
#     try:    checks += self._check_gen_quality(coverage_report)
#     except Exception as exc:
#             log.debug("R72.3: gen-quality check skipped: %s", exc)   # NameError!
#
# So a soft SKIP became a hard NameError raised FROM the handler, which
# propagates and takes the whole quality gate down — the opposite of the
# best-effort behaviour the code plainly intends. Matches the "arta.gate" name
# the module's own local `_logging.getLogger` call already uses.
log = logging.getLogger("arta.gate")


@dataclass
class GateThresholds:
    """Configurable quality gate thresholds per deployment environment."""

    # Coverage requirements by risk priority
    p0_coverage_pct: float = 100.0      # P0 must be 100% covered
    # Verified by Coder agent on v2026.3.14
    p1_coverage_pct: float = 90.0
    p2_coverage_pct: float = 75.0
    p3_coverage_pct: float = 50.0
    overall_coverage_pct: float = 80.0

    # Pass rate requirements
    p0_pass_rate_pct: float = 100.0     # Zero P0 failures allowed
    p1_pass_rate_pct: float = 95.0
    overall_pass_rate_pct: float = 90.0
    # R33.7 — per-tool effective pass rate target. The user's bar is
    # ≥95% for every test type. Effective rate excludes BLOCKED +
    # sut_regression so the gate measures ARTA's tool quality, not
    # operator-config gaps or detected SUT bugs.
    per_tool_pass_rate_pct: float = 95.0

    # Non-functional requirements — Performance
    performance_p95_ms: int = 3000
    performance_p99_ms: int = 5000
    performance_error_rate_pct: float = 1.0

    # Non-functional requirements — Security
    max_critical_security_findings: int = 0
    max_high_security_findings: int = 2
    require_auth_bypass_test: bool = True

    # Non-functional requirements — Reliability
    require_health_checks: bool = True

    # Non-functional requirements — Maintainability
    min_code_coverage_pct: float = 80.0
    max_duplication_pct: float = 5.0

    # F3-3: Accessibility (WCAG 2.1 AA) — counts of axe-core violations
    # P0 requirements must be 0 violations; warn/fail thresholds for the rest.
    max_a11y_violations_critical: int = 0   # serious / critical impact
    max_a11y_violations_moderate: int = 5   # moderate impact

    # Open defect policy
    max_open_p0_defects: int = 0        # No P0 defects allowed
    max_open_p1_defects: int = 3

    # R37.7 — SUT quality gate thresholds. These complement the test-
    # quality checks (per_tool_pass_rate) by measuring the SUT's own
    # health: did this build introduce new backend regressions, and is
    # the SUT team responding to critical bugs in time?
    #   - new_sut_regressions: count of triage_category=sut_regression
    #     defects opened in the latest run vs the prior run. > N means
    #     the SUT got worse this build.
    #   - critical_sut_age_hours: max age (hours) for an open P0
    #     sut_regression. Beyond this means the SUT team isn't
    #     responding fast enough to a critical detected bug.
    max_new_sut_regressions: int = 5
    max_critical_sut_age_hours: float = 24.0

    # F3-7: Flakiness gating — flakiness_score = 100 * (1 - flaky_count / total_distinct_tests)
    # WARN below the warn threshold; BLOCK below the fail threshold. Only evaluated when
    # execution_history is provided to decide().
    flakiness_warn_score: float = 70.0
    flakiness_fail_score: float = 50.0
    flakiness_min_runs_per_test: int = 3   # need ≥ N runs to call a test flaky

    # F7-1: Compliance attestation. The required_attestations list is the set of
    # framework-specific marker names the strategy artifact must claim coverage for
    # (e.g. ["GDPR-art32", "PCI-DSS-6.5"]). When non-empty, the gate checks the
    # latest strategy's `compliance_markers` field — any required marker missing
    # from the strategy blocks the release.
    required_compliance_attestations: list[str] = field(default_factory=list)

    # F7-1: Reproducibility gate. A test is reproducible when the SAME test_id
    # produced the SAME outcome across the last `repro_window` runs. Emits a
    # `reproducibility_score` (0-100); BLOCK below fail, WARN below warn.
    repro_warn_score: float = 80.0
    repro_fail_score: float = 60.0
    repro_window: int = 3   # consider the last N runs per test


@dataclass
class GateCheck:
    name: str
    passed: bool
    actual: str
    expected: str
    severity: str   # BLOCK | WARN | INFO
    detail: str = ""
    category: str = ""  # coverage | pass_rate | defect | nfr_security | nfr_performance | nfr_reliability | nfr_maintainability | risk


@dataclass
class GateWaiver:
    """Active waiver for a specific gate check."""
    waived_check: str
    rationale: str
    approved_by: str
    expires_at: datetime


@dataclass
class GateDecision:
    decision: Literal["PASS", "CONCERNS", "FAIL", "WAIVED"]
    checks: list[GateCheck] = field(default_factory=list)
    blocking_checks: list[GateCheck] = field(default_factory=list)
    warnings: list[GateCheck] = field(default_factory=list)
    summary: str = ""
    evidence_package_id: str = ""
    waiver: GateWaiver | None = None
    # F7-1: Audit trail — every check's rationale (the `detail` field plus the
    # actual vs expected) goes into a structured list so an auditor can
    # reconstruct WHY the gate decided what it did. Mandatory for
    # regulated-industry deploys; harmless for everyone else.
    audit_trail: list[dict] = field(default_factory=list)
    # Gap 8b: HMAC-SHA256 over a canonical serialization of audit_trail.
    # Verification endpoint recomputes from the stored entries and compares.
    audit_trail_hash: str = ""
    audit_trail_signed_at: str = ""
    # Structured signals for downstream ATDD regen — keyed by violation type.
    violation_signals: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "decision": self.decision,
            "summary": self.summary,
            "blocking": [
                {"name": c.name, "actual": c.actual, "expected": c.expected, "category": c.category}
                for c in self.blocking_checks
            ],
            "warnings": [
                {"name": c.name, "actual": c.actual, "expected": c.expected, "category": c.category}
                for c in self.warnings
            ],
            "passed": [
                {"name": c.name, "actual": c.actual, "expected": c.expected, "category": c.category}
                for c in self.checks if c.passed
            ],
            "evidence_package_id": self.evidence_package_id,
            "audit_trail": self.audit_trail,  # F7-1
            "audit_trail_hash": self.audit_trail_hash,            # Gap 8b
            "audit_trail_signed_at": self.audit_trail_signed_at,  # Gap 8b
            "violation_signals": self.violation_signals,
        }
        if self.waiver:
            result["waiver"] = {
                "waived_check": self.waiver.waived_check,
                "rationale": self.waiver.rationale,
                "approved_by": self.waiver.approved_by,
                "expires_at": self.waiver.expires_at.isoformat(),
            }
        return result


class QualityGateAgent:
    """
    TEA Quality Gate Authority.

    Evidence-based PASS/CONCERNS/FAIL/WAIVED decision for release.
    Never blocks on opinion — always on measurable evidence.

    BMAD TEA 4-outcome model:
      PASS     — all checks clear
      CONCERNS — risk 6-8 warnings with assigned mitigation owners
      FAIL     — score=9 requirement uncovered, or any critical check failed
      WAIVED   — authorized exception (requires rationale + expiry)
    """

    def __init__(self, thresholds: GateThresholds | None = None):
        self._thresholds = thresholds or GateThresholds()

    async def decide(
        self,
        coverage_report: dict,
        risk_profiles: list[dict],
        defects: list[dict],
        waivers: list[dict] | None = None,
        execution_history: list[dict] | None = None,
        strategy: dict | None = None,  # F7-1: pass strategy artifact for compliance check
    ) -> dict:
        checks: list[GateCheck] = []

        # ── Coverage checks ───────────────────────────────────────────────
        checks += self._check_coverage(coverage_report, risk_profiles)

        # ── Pass rate checks ──────────────────────────────────────────────
        checks += self._check_pass_rates(coverage_report, risk_profiles)
        # R33.7 — per-tool ≥95% gate. One row per tool present in the
        # run; lets operators see which tool is the bottleneck. Excludes
        # BLOCKED + sut_regression from each tool's denominator.
        # R42.5 demoted this row to INFO-only (the operator gets the
        # context but it's no longer the gate decision).
        checks += self._check_per_tool_pass_rates(coverage_report)
        # R42.5 KEYSTONE — RAW per-tool pass rate (no exclusions). This
        # is the row the gate now decides on. The user's bar: ≥95% raw,
        # zero skips, every test type.
        checks += self._check_per_tool_raw_pass_rates(coverage_report)
        # R42.5 — zero-skips gate. BLOCKs the build when any test
        # ended in BLOCKED/SKIP rather than running to PASS/FAIL.
        checks += self._check_zero_skips(coverage_report)
        # R29.3d — separate Configuration-completeness gate row when
        # any BLOCKED row exists. Distinct from pass-rate so operators
        # see WHICH env vars need filling, with WARN/FAIL based on
        # blocked count (≥50 = run is meaningless without fixing).
        checks += self._check_blocked_count(coverage_report)
        # R29.5 — spec-drift WARN row when generated specs reference
        # API endpoints discovery hasn't observed. Operator-actionable
        # but never blocks the run (LLM may have valid guesses).
        checks += self._check_spec_drift(coverage_report)
        # R30.6 — traceability chain-health WARN when Result edges are
        # missing for requirements that had tests dispatched.
        checks += self._check_traceability_health(coverage_report)
        # R55.13 — endpoint coverage gate row. INFO when ≥70%, WARN below.
        # Reads `traceability_chain_health.endpoint_coverage` stamped by
        # `_compute_endpoint_coverage` in post_run_chain_pipeline. Reuses
        # R55.7's Newman Result→Endpoint edges as the coverage signal.
        checks += self._check_endpoint_coverage(coverage_report)
        # R72.3 — Pillar 1 gen-quality metric. Operators see upstream
        # quality regressions (prompt/model drift) within minutes of a
        # single gen run, not 30 min after runtime failures pile up.
        try:
            checks += self._check_gen_quality(coverage_report)
        except Exception as _r72_3_exc:
            log.debug("R72.3: gen-quality check skipped: %s", _r72_3_exc)
        # R118.H — operator-visible "grounding-blocked rate" metric.
        # Scans on-disk PW specs for R102.A stamps; severity tiers
        # INFO ≤5% / WARN ≤15% / BLOCK >15%. Closes the visibility gap
        # where dispatch-excluded specs were invisible to R72.3.
        try:
            checks += self._check_r102_a_stamp_rate(coverage_report)
        except Exception as _r118_h_exc:
            log.debug("R118.H: stamp-rate check skipped: %s", _r118_h_exc)
        # R93.5 — Pillar 4 visibility for Bearer-injection propagation.
        # Tells operators whether R91.A/R93.1/R93.A's gen-time fixes
        # have landed on disk. Pre-R93.5 the predicate-gate regression
        # was invisible for 24h; R93.5 surfaces it in 1 run.
        try:
            checks += self._check_bearer_auth_coverage(coverage_report)
        except Exception as _r93_5_exc:
            log.debug("R93.5: bearer-coverage check skipped: %s", _r93_5_exc)
        # R85.M+R86.M — autofix-invocation rate. Counts specs healed by
        # deterministic validators (R85.1 bare-object, R86.2/R86.2a
        # Content-Type, R57.1 grounding) vs clean-from-gen specs. A
        # downward trend over time = LLM gen-quality improving.
        try:
            checks += self._check_autofix_rate(coverage_report)
        except Exception as _r85_m_exc:
            log.debug("R85.M+R86.M: autofix-rate check skipped: %s", _r85_m_exc)
        # R57.8 — failure-trend gate row. Async because it queries
        # test_runs DB for the most-recent prior completed run. Cold-
        # start (no prior run) returns [] → no-op.
        try:
            trend_checks = await self._check_failure_trend(coverage_report)
            checks += trend_checks
        except Exception as _r57_8_exc:
            log.debug("R57.8: failure-trend check skipped: %s", _r57_8_exc)

        # ── Defect policy checks ──────────────────────────────────────────
        checks += self._check_defect_policy(defects)
        # R37.7 — SUT-quality checks complement defect policy. Measures
        # whether the SUT is getting better or worse, not whether the
        # tests pass. Operators see "SUT regressed" alongside the
        # existing pass-rate signal.
        checks += self._check_sut_quality(defects, coverage_report)

        # ── NFR checks (4 categories) ────────────────────────────────────
        nfr = coverage_report.get("nfr", {})
        checks += self._check_nfr_performance(nfr)
        checks += self._check_nfr_security(nfr)
        checks += self._check_nfr_reliability(nfr)
        checks += self._check_nfr_maintainability(nfr)

        # ── BMAD TEA: Score=9 auto-FAIL ──────────────────────────────────
        checks += self._check_risk_auto_fail(risk_profiles, coverage_report)

        # ── BMAD canonical: per-requirement coverage targets ─────────────
        checks += self._check_priority_coverage_targets(coverage_report, risk_profiles)

        # ── Fix UUU (Phase G): UNCOVERED AC detection ────────────────────
        # The reference traceability graphic shows AC-003 → UNCOVERED.
        # Surface as a distinct gate check rather than only via the
        # priority-target math (which averages partial-coverage with
        # full-uncoverage). Any P0 AC fully uncovered = FAIL gate;
        # any other priority = CONCERNS.
        checks += self._check_uncovered_acs(coverage_report, risk_profiles)

        # ── BMAD canonical: Score 6-8 requires mitigation plan ───────────
        checks += self._check_risk_concerns(risk_profiles, strategy)

        # ── Layer 7 quality smells: orphans + redundant tests ────────────
        checks += self._check_traceability_smells(coverage_report)

        # ── Phase 4.5: DatasetRecipe closed-loop verified ────────────────
        checks += self._check_recipe_verified(risk_profiles)

        # ── Phase 5.6: Self-healing requires_rerun ───────────────────────
        # Inline-heal applies patches mid-run but the runner can't reload
        # the test file — the run's results reflect the OLD broken state.
        # Surface as CONCERNS so the gate doesn't approve on stale data.
        checks += self._check_pending_heal_rerun()

        # ── Phase F1: call-sequence integrity (cascade vs root cause) ────
        # Reads from coverage_report["sequence_integrity"]; the traceability
        # agent populates that with a single Neo4j MATCH (failed)-[:DEPENDS_ON*1..5]->(prov)
        # query post-execution. Surfaces cascade failures (INFO),
        # provider contract violations (BLOCK), and unresolved chain starts
        # (WARN). Per DR-3, BLOCK→WARN downgrade when Neo4j is degraded.
        checks += self._check_call_sequence_integrity(coverage_report)

        # ── run-dea20e follow-up: unresolved path-params ────────────────
        # When Newman skipped tests because env vars weren't populated, we
        # want a visible gate signal so the operator knows "X% of tests
        # were skipped because Y env vars need to be set" rather than
        # silently passing on the runs that did execute.
        # Phase F2 upgrade: pulls provider linkage from sequence_integrity.
        checks += self._check_unresolved_path_params(coverage_report)

        # ── BMAD RV: test-quality score (waitForTimeout, hard sleeps, …) ─
        checks += self._check_test_quality(coverage_report)

        # ── F3-3: Accessibility (WCAG 2.1 AA) ────────────────────────────
        checks += self._check_a11y(nfr)

        # ── F3-7: Flakiness gate (only when history provided) ────────────
        if execution_history:
            checks += self._check_flakiness(execution_history)

        # ── F7-1: Compliance / audit / reproducibility dimensions ────────
        if self._thresholds.required_compliance_attestations:
            checks += self._check_compliance(strategy)
        if execution_history:
            checks += self._check_reproducibility(execution_history)

        blocking = [c for c in checks if not c.passed and c.severity == "BLOCK"]
        warnings = [c for c in checks if not c.passed and c.severity == "WARN"]

        # ── R134.B.1 KEYSTONE — per-blocker waiver matching ──────────────
        # Pre-R134.B.1: a single waiver matching ANY blocker silently waived
        # ALL blockers → operator saw green release with 2-3 active blockers
        # hidden. Pillar 4 mission violated.
        # Post-R134.B.1: only checks WITH an explicit matching waiver are
        # waived; unwaived blockers keep blocking. Decision = WAIVED only
        # when EVERY blocker is individually waived.
        waived_checks, still_blocking = self._r134_b_1_match_waivers_per_blocker(
            waivers or [], blocking,
        )
        # Legacy field — still exposed on the decision for backward compat,
        # but the truthful blocker-list is `still_blocking`.
        active_waiver = self._find_active_waiver(waivers or [], blocking)

        if still_blocking:
            # Any unwaived blocker → FAIL (mission-truthful per R134.B.1)
            decision_value: Literal["PASS", "CONCERNS", "FAIL", "WAIVED"] = "FAIL"
        elif waived_checks:
            # All blockers have explicit per-blocker waivers
            decision_value = "WAIVED"
        elif warnings:
            decision_value = "CONCERNS"
        else:
            decision_value = "PASS"

        # F7-1: Build the audit trail — one entry per check with the decision rationale.
        # Auditors get a complete record of "what was measured, what threshold was applied,
        # what the decision was" for compliance reviews.
        audit_trail = [
            {
                "name": c.name,
                "category": c.category,
                "outcome": "PASS" if c.passed else c.severity,
                "actual": c.actual,
                "expected": c.expected,
                "rationale": c.detail,
            }
            for c in checks
        ]

        signals: dict = {}
        for check in checks:
            if not check.passed:
                if check.category == "accessibility":
                    signals["a11y_critical"] = True
                elif check.category == "performance":
                    signals["perf_p95_exceeded"] = True
                elif check.category == "security":
                    signals["security_critical"] = True

        # Gap 8b: HMAC-sign the audit_trail. Tamper-evident — auditors verify
        # via /api/gates/{gate_id}/verify which recomputes and compares.
        audit_hash, signed_at = self._sign_audit_trail(audit_trail)

        decision = GateDecision(
            decision=decision_value,
            checks=checks,
            blocking_checks=blocking,
            warnings=warnings,
            summary=self._build_summary(decision_value, blocking, warnings, checks),
            waiver=active_waiver,
            audit_trail=audit_trail,
            audit_trail_hash=audit_hash,
            audit_trail_signed_at=signed_at,
            violation_signals=signals,
        )
        result = decision.to_dict()

        # Fix RRR (Phase G) + Phase 5.4: numeric quality_score 0-100 derived
        # from the same checks. The formula is now ALSO published in the
        # response payload (`quality_score_formula` + `quality_score_inputs`)
        # so auditors can re-compute the score by hand from the exposed
        # inputs and confirm no fudge factor was applied.
        try:
            pass_rate = float(coverage_report.get("pass_rate", 0)) / 100.0
            coverage_pct = float(coverage_report.get("coverage_pct", 0)) / 100.0
            total = max(1, int(coverage_report.get("total", coverage_report.get("total_tests", 1))))
            defects_open = int(coverage_report.get("open_defects", 0))
            defect_density = min(1.0, defects_open / total)
            quality_score = round(
                (max(0.0, min(1.0, pass_rate)) * 50)
                + (max(0.0, min(1.0, coverage_pct)) * 30)
                + ((1 - defect_density) * 20)
            )
            result["quality_score_inputs"] = {
                "pass_rate": round(pass_rate, 4),
                "coverage_pct": round(coverage_pct, 4),
                "defect_density": round(defect_density, 4),
                "total_tests": total,
                "open_defects": defects_open,
            }
        except Exception:
            quality_score = 0
        result["quality_score"] = max(0, min(100, quality_score))
        result["quality_score_formula"] = (
            "pass_rate * 50 + coverage_pct * 30 + (1 - defect_density) * 20  "
            "(each input clamped to [0, 1]; defect_density = open_defects / max(1, total))"
        )
        # Letter grade for UI consistency
        qs = result["quality_score"]
        if qs >= 90:
            grade = "A"
        elif qs >= 80:
            grade = "B"
        elif qs >= 70:
            grade = "C"
        elif qs >= 60:
            grade = "D"
        else:
            grade = "F"
        result["quality_grade"] = grade
        return result

    @staticmethod
    def _sign_audit_trail(audit_trail: list[dict]) -> tuple[str, str]:
        """Compute HMAC-SHA256 over a canonical JSON serialization of the
        audit trail. Returns (hex_digest, signed_at_isoformat).

        Reads secret from ARTA_AUDIT_HMAC_SECRET env var. If unset, falls back
        to a process-local random key and logs a loud warning — this breaks
        cross-restart verification but keeps the API contract stable in dev.
        """
        import hashlib
        import hmac
        import json as _json
        import os
        import secrets
        secret = os.environ.get("ARTA_AUDIT_HMAC_SECRET")
        if not secret:
            import logging as _logging
            _key = getattr(QualityGateAgent._sign_audit_trail, "_ephemeral_key", None)
            if _key is None:
                _key = secrets.token_hex(32)
                QualityGateAgent._sign_audit_trail._ephemeral_key = _key  # type: ignore[attr-defined]
                _logging.getLogger("arta.gate").warning(
                    "ARTA_AUDIT_HMAC_SECRET unset — using ephemeral signing key. "
                    "Audit-trail verification will FAIL after process restart. "
                    "Set this env var in production for tamper-evident audit logs."
                )
            secret = _key
        payload = _json.dumps(audit_trail, sort_keys=True, separators=(",", ":")).encode()
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        return digest, datetime.now(timezone.utc).isoformat()

    # ── Check Implementations ──────────────────────────────────────────────

    def _check_coverage(
        self, report: dict, risk_profiles: list[dict]
    ) -> list[GateCheck]:
        checks = []
        by_priority: dict[str, dict] = report.get("coverage_by_priority", {})

        priority_thresholds = {
            "P0": self._thresholds.p0_coverage_pct,
            "P1": self._thresholds.p1_coverage_pct,
            "P2": self._thresholds.p2_coverage_pct,
            "P3": self._thresholds.p3_coverage_pct,
        }

        for priority, threshold in priority_thresholds.items():
            actual_pct = by_priority.get(priority, {}).get("coverage_pct", 100.0)
            passed = actual_pct >= threshold
            checks.append(GateCheck(
                name=f"coverage_{priority}",
                passed=passed,
                actual=f"{actual_pct:.1f}%",
                expected=f"\u2265{threshold:.0f}%",
                severity="BLOCK" if not passed and priority in ("P0", "P1") else "WARN",
                detail=f"{priority} requirements must reach {threshold}% test coverage",
                category="coverage",
            ))

        overall = report.get("coverage_pct", 0.0)
        checks.append(GateCheck(
            name="overall_coverage",
            passed=overall >= self._thresholds.overall_coverage_pct,
            actual=f"{overall:.1f}%",
            expected=f"\u2265{self._thresholds.overall_coverage_pct:.0f}%",
            severity="BLOCK",
            category="coverage",
        ))
        return checks

    def _check_pass_rates(
        self, report: dict, risk_profiles: list[dict]
    ) -> list[GateCheck]:
        checks = []
        pass_rate = report.get("pass_rate", 0.0)
        p0_pass_rate = report.get("p0_pass_rate", 0.0)

        # Phase K7 \u2014 effective_pass_rate excludes cascade-skipped tests
        # from BOTH numerator and denominator. Cascade-skips reflect
        # missing env vars (config gap) NOT real test failures; counting
        # them against the gate punishes operators for what is actually
        # a Discovery-not-yet-run state, not test quality.
        #
        # IMPORTANT: cascade_failures from sequence_integrity is STEP-level
        # (one entry per failing API call), but total_test_count is
        # TEST-level. Deduplicate by `test_id` before counting so the
        # arithmetic stays balanced; AND fall back to counting tests with
        # status=SKIP+cascade_reason when sequence_integrity is empty.
        cascade_test_ids: set[str] = set()
        blocked_test_ids: set[str] = set()  # R29.3d — config-gap rows
        sut_regression_test_ids: set[str] = set()  # R33.6 — real SUT bugs
        test_gen_bug_test_ids: set[str] = set()  # R34.3 — auto-heal queue
        total_test_count = 0
        passed_count = 0
        skipped_test_count = 0
        # R33.6 helper — extract triage_category from any of the three
        # possible result-row paths (see R30.7-D for the contract).
        def _triage_cat_of(row: dict) -> str | None:
            md = row.get("metadata") or row.get("metadata_") or {}
            if isinstance(md, str):
                try:
                    import json as _json
                    md = _json.loads(md)
                except Exception:
                    md = {}
            if not isinstance(md, dict):
                md = {}
            op_triage = md.get("operator_triage") or {}
            if not isinstance(op_triage, dict):
                op_triage = {}
            return (
                op_triage.get("category")
                or row.get("triage_category")
                or md.get("triage_category")
                or (row.get("triage") or {}).get("triage_category")
            )
        try:
            seq = report.get("sequence_integrity") or {}
            for c in (seq.get("cascade_failures") or []):
                tid = c.get("test_id") if isinstance(c, dict) else None
                if tid:
                    cascade_test_ids.add(tid)
            # Phase M5 — scope to the run under audit, not all runs ever.
            # Pre-M5 this iterated ALL of `_REAL_RUNS.values()` and SUMMED
            # results across the entire process lifetime. Stale runs from
            # earlier in the day distorted the current gate decision —
            # build N could pass/fail because of build N-3's data.
            from ..api.routers.execution import _REAL_RUNS
            target_run_id = (
                report.get("run_id")
                or report.get("build_id")
                or report.get("target_run_id")
            )
            target_run = None
            if target_run_id and isinstance(_REAL_RUNS, dict):
                candidate = _REAL_RUNS.get(target_run_id)
                if isinstance(candidate, dict):
                    target_run = candidate
            if target_run is None and isinstance(_REAL_RUNS, dict):
                # Fallback: pick the most recent run by started_at.
                runs = [r for r in _REAL_RUNS.values() if isinstance(r, dict)]
                if runs:
                    target_run = max(
                        runs,
                        key=lambda r: r.get("started_at") or "",
                    )
            if target_run:
                for r in (target_run.get("results") or []):
                    if isinstance(r, dict):
                        total_test_count += 1
                        status = r.get("status")
                        if status == "PASS":
                            passed_count += 1
                        elif status == "SKIP":
                            skipped_test_count += 1
                            # Also count test-level SKIPs from cascade
                            err = str(r.get("error_message") or "")
                            if "unresolved" in err.lower() or "cascade" in err.lower():
                                tid = r.get("test_id")
                                if tid:
                                    cascade_test_ids.add(tid)
                        elif status == "BLOCKED":
                            # R29.3d — BLOCKED is config-gap, not a real
                            # test failure. Exclude from pass-rate
                            # denominator (same treatment as cascade).
                            tid = r.get("test_id")
                            if tid:
                                blocked_test_ids.add(tid)
                        # R33.6 / R34.3 — when the failure was correctly
                        # classified, route it OUT of the test-quality
                        # denominator into a separate bucket:
                        # - sut_regression: real SUT bug (operator
                        #   investigates SUT, not ARTA tool quality)
                        # - test_gen_bug: hallucinated selector / missing
                        #   import / wrong assertion (ARTA's self-heal
                        #   pipeline auto-regenerates these via R30.3 +
                        #   Tier-1 autofix; counting them as failures
                        #   double-attributes the same signal)
                        # Both surface as separate counters; both reduce
                        # the effective denominator.
                        if status in ("FAIL", "ERROR"):
                            cat = _triage_cat_of(r)
                            if cat == "sut_regression":
                                tid = r.get("test_id")
                                if tid:
                                    sut_regression_test_ids.add(tid)
                            elif cat == "test_gen_bug":
                                tid = r.get("test_id")
                                if tid:
                                    test_gen_bug_test_ids.add(tid)
        except Exception:
            pass

        cascade_skip_count = len(cascade_test_ids)
        blocked_count = len(blocked_test_ids)
        sut_bug_count = len(sut_regression_test_ids)
        test_gen_bug_count = len(test_gen_bug_test_ids)  # R34.3
        effective_total = max(
            0,
            total_test_count
            - cascade_skip_count
            - blocked_count
            - sut_bug_count
            - test_gen_bug_count,
        )
        effective_pass_rate = (
            100.0 * passed_count / effective_total
            if effective_total > 0 else pass_rate
        )

        checks.append(GateCheck(
            name="p0_pass_rate",
            passed=p0_pass_rate >= self._thresholds.p0_pass_rate_pct,
            actual=f"{p0_pass_rate:.1f}%",
            expected=f"={self._thresholds.p0_pass_rate_pct:.0f}%",
            severity="BLOCK",
            detail="P0 critical tests must have 100% pass rate",
            category="pass_rate",
        ))

        # Phase K7 / R29.3d / R33.6 \u2014 when cascade OR BLOCKED OR sut_regression
        # dominates, surface BOTH numbers; gate decides on
        # `effective_pass_rate` (the operator-actionable one). All three
        # categories are excluded from the denominator because:
        #   - cascade: upstream provider failed, this test couldn't run
        #   - BLOCKED: config gap, not test failure
        #   - sut_regression: real SUT bug ARTA correctly detected; ARTA
        #     gets credit for the detection, not blamed for the failure
        if (
            (cascade_skip_count > 0 or blocked_count > 0
             or sut_bug_count > 0 or test_gen_bug_count > 0)
            and effective_total > 0
        ):
            _exclusions: list[str] = []
            if cascade_skip_count:
                _exclusions.append(f"{cascade_skip_count} cascade-skips")
            if blocked_count:
                _exclusions.append(f"{blocked_count} BLOCKED")
            if sut_bug_count:
                _exclusions.append(f"{sut_bug_count} SUT bugs detected")
            if test_gen_bug_count:
                _exclusions.append(f"{test_gen_bug_count} test-gen bugs (auto-heal queued)")
            _exclusion_str = " + ".join(_exclusions)
            checks.append(GateCheck(
                name="overall_pass_rate",
                passed=effective_pass_rate >= self._thresholds.overall_pass_rate_pct,
                actual=f"{pass_rate:.1f}% raw / {effective_pass_rate:.1f}% excluding {_exclusion_str}",
                expected=f"\u2265{self._thresholds.overall_pass_rate_pct:.0f}%",
                severity="BLOCK",
                detail=(
                    f"Effective pass rate (excluding cascade-skips, BLOCKED "
                    f"config-gap rows, and SUT-regression detections) = "
                    f"{effective_pass_rate:.1f}% on {effective_total} ARTA-quality "
                    f"executable tests. "
                    + (f"{cascade_skip_count} cascade-skipped (missing path-params); " if cascade_skip_count else "")
                    + (f"{blocked_count} BLOCKED (unresolved env vars \u2014 operator must fill via Settings \u2192 Environments \u2192 Variables); " if blocked_count else "")
                    + (f"{sut_bug_count} real SUT bugs detected (open as defects; ARTA's tool quality is independent of these); " if sut_bug_count else "")
                    + "click 'Re-run discovery' in the Discovery panel to harvest values from a Playwright run."
                ),
                category="pass_rate",
            ))
        else:
            checks.append(GateCheck(
                name="overall_pass_rate",
                passed=pass_rate >= self._thresholds.overall_pass_rate_pct,
                actual=f"{pass_rate:.1f}%",
                expected=f"\u2265{self._thresholds.overall_pass_rate_pct:.0f}%",
                severity="BLOCK",
                category="pass_rate",
            ))
        # R33.6 \u2014 surface SUT-bug count as a separate informational
        # check so the gate panel can render "ARTA detected 8 backend
        # regressions" alongside pass rate. Unlike pass_rate this is a
        # detection metric, not a quality regression: more is "more
        # signal," not "worse."
        if sut_bug_count > 0:
            checks.append(GateCheck(
                name="sut_regressions_detected",
                passed=True,   # detection is success, not failure
                actual=f"{sut_bug_count} detected",
                expected="(detection metric)",
                severity="INFO",
                detail=(
                    f"ARTA classified {sut_bug_count} test failure(s) as "
                    f"sut_regression (R30.1 Layer 1B: 5xx / network errors / "
                    f"auth regressions). These reflect real SUT-side issues "
                    f"and are surfaced as defects via /api/defects?run_id=..."
                ),
                category="sut_quality",
            ))
        return checks

    def _check_per_tool_pass_rates(self, coverage_report: dict) -> list[GateCheck]:
        """R33.7 — emit one GateCheck per tool with effective pass rate.

        The user's bar: every test type must execute with ≥95% pass rate.
        Per-tool granularity lets operators see WHICH tool is the
        bottleneck (Playwright at 0% drags overall down even when Axe
        is at 100%). The overall row stays as a holistic summary.

        Effective pass rate per tool = pass / (pass + fail), excluding:
        - BLOCKED (config gap)
        - sut_regression (real SUT bug, not test bug)
        - cascade-skipped (upstream provider failed)

        Severity:
        - >= 95% effective → PASS (✓)
        - 80–94% effective → WARN (⚠) — investigate but don't block
        - <80% effective → BLOCK (✗) — tool is broken, run is unreliable
        - 0 dispatched → INFO ('not exercised') — no rows to judge

        When ALL exec rows for a tool are BLOCKED/sut_regression
        (effective_total=0), emit INFO with the operator-action context.
        """
        try:
            from ..api.routers.execution import _REAL_RUNS
        except Exception:
            return []

        target_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        target_run = None
        if target_run_id and isinstance(_REAL_RUNS, dict):
            cand = _REAL_RUNS.get(target_run_id)
            if isinstance(cand, dict):
                target_run = cand
        if target_run is None and isinstance(_REAL_RUNS, dict):
            runs = [r for r in _REAL_RUNS.values() if isinstance(r, dict)]
            if runs:
                target_run = max(
                    runs, key=lambda r: r.get("started_at") or "",
                )
        if target_run is None:
            return []

        # Same triage-category extractor as _check_pass_rates uses.
        def _triage_cat_of(row: dict) -> str | None:
            md = row.get("metadata") or row.get("metadata_") or {}
            if isinstance(md, str):
                try:
                    import json as _json
                    md = _json.loads(md)
                except Exception:
                    md = {}
            if not isinstance(md, dict):
                md = {}
            op_triage = md.get("operator_triage") or {}
            if not isinstance(op_triage, dict):
                op_triage = {}
            return (
                op_triage.get("category")
                or row.get("triage_category")
                or md.get("triage_category")
                or (row.get("triage") or {}).get("triage_category")
            )

        # Aggregate per tool. We use the tool TAG (`automation_tool` or
        # `tool` field) since `_REAL_RESULTS` rows carry both. Tools we
        # know about — extra tools are accepted automatically.
        from collections import defaultdict
        per_tool: dict[str, dict[str, int]] = defaultdict(lambda: {
            "pass": 0, "fail": 0, "skip": 0, "blocked": 0,
            "sut_regression": 0, "test_gen_bug": 0, "total": 0,
        })

        for r in (target_run.get("results") or []):
            if not isinstance(r, dict):
                continue
            tool = (r.get("automation_tool") or r.get("tool") or "unknown").lower()
            status = r.get("status") or ""
            buckets = per_tool[tool]
            buckets["total"] += 1
            if status == "PASS":
                buckets["pass"] += 1
            elif status == "FAIL":
                # R33.6 + R34.3 — route classified failures into
                # buckets that DON'T count against test quality.
                cat = _triage_cat_of(r)
                if cat == "sut_regression":
                    buckets["sut_regression"] += 1
                elif cat == "test_gen_bug":
                    buckets["test_gen_bug"] += 1
                else:
                    buckets["fail"] += 1
            elif status == "SKIP":
                buckets["skip"] += 1
            elif status == "BLOCKED":
                buckets["blocked"] += 1

        # Display labels match the dashboard's gate panel naming.
        TOOL_LABELS = {
            "playwright": "Playwright (UI)",
            "newman": "Newman (API)",
            "k6": "k6 (Performance)",
            "zap": "ZAP (Security)",
            "axe": "Axe (Accessibility)",
            "pytest": "Pytest (Analytics)",
            "appium": "Appium (Mobile)",
            "selenium": "Selenium (Legacy UI)",
            "cypress": "Cypress (UI)",
        }

        out: list[GateCheck] = []
        # Threshold from config; default to 95 if unspecified.
        per_tool_threshold = float(
            getattr(self._thresholds, "per_tool_pass_rate_pct", 95.0)
        )
        warn_threshold = max(0.0, per_tool_threshold - 15.0)  # 80% by default

        for tool, b in sorted(per_tool.items()):
            if tool == "unknown":
                continue
            label = TOOL_LABELS.get(tool, tool.title())
            effective_total = b["pass"] + b["fail"]
            raw_total = b["total"]
            if effective_total == 0:
                # All rows for this tool were BLOCKED / sut_regression /
                # SKIP. Emit INFO so operators see the tool ran but
                # produced no judge-able outcomes; gate doesn't block.
                if raw_total == 0:
                    continue   # tool wasn't part of the suite
                excluded = []
                if b["blocked"]:
                    excluded.append(f"{b['blocked']} BLOCKED")
                if b["sut_regression"]:
                    excluded.append(f"{b['sut_regression']} SUT bugs")
                if b["test_gen_bug"]:
                    excluded.append(f"{b['test_gen_bug']} test-gen bugs")
                if b["skip"]:
                    excluded.append(f"{b['skip']} SKIP")
                out.append(GateCheck(
                    name=f"{tool}_effective_pass_rate",
                    passed=True,
                    actual="no judge-able rows",
                    expected=f"≥{per_tool_threshold:.0f}%",
                    severity="INFO",
                    detail=(
                        f"{label}: {raw_total} dispatched, all rows in "
                        f"non-judging states ({', '.join(excluded) or 'mixed'}). "
                        f"Operator action: fill BLOCKED env vars or open "
                        f"defects for SUT-regression rows."
                    ),
                    category="per_tool_pass_rate",
                ))
                continue
            effective_pct = 100.0 * b["pass"] / effective_total
            passed = effective_pct >= per_tool_threshold
            # R77.7.B — re-promote effective rate as a co-gate row.
            # Pre-R77.7.B R42.5 demoted this to WARN-only after deciding
            # RAW is the user-facing bar. But operators ALSO need to know
            # when ARTA's tool quality slips below 95% on the rows it
            # judges (effective denominator excludes BLOCKED / SUT bugs /
            # test_gen_bugs). The full per-tool bar is now: BOTH raw AND
            # effective must be ≥95%. RAW reflects SUT health; EFFECTIVE
            # reflects ARTA's tool quality. Severity tiers:
            #   ≥ threshold (95%)    → PASS (✓)
            #   warn_threshold..<    → WARN (⚠) — investigate
            #   < warn_threshold     → BLOCK (✗) — gate fails
            if passed:
                severity = "INFO"
            elif effective_pct < warn_threshold:
                severity = "BLOCK"
            else:
                severity = "WARN"
            actual_str = (
                f"{b['pass']}/{effective_total} = {effective_pct:.1f}% "
                f"effective (raw {b['pass']}/{raw_total})"
            )
            detail_extras = []
            if b["blocked"]:
                detail_extras.append(f"{b['blocked']} BLOCKED")
            if b["sut_regression"]:
                detail_extras.append(f"{b['sut_regression']} SUT bugs detected")
            if b["test_gen_bug"]:
                detail_extras.append(f"{b['test_gen_bug']} test-gen bugs (auto-heal queued)")
            if b["skip"]:
                detail_extras.append(f"{b['skip']} skipped")
            extras_str = (
                f" — exclusions: {', '.join(detail_extras)}" if detail_extras else ""
            )
            # R77.7.B — rename emitted name to `{tool}_effective_pass_rate`
            # so the gate panel + dashboard render side-by-side with the
            # RAW row `{tool}_raw_pass_rate` (R42.5). The old name
            # `{tool}_pass_rate` was ambiguous (raw or effective?).
            out.append(GateCheck(
                name=f"{tool}_effective_pass_rate",
                passed=passed,
                actual=actual_str,
                expected=f"≥{per_tool_threshold:.0f}%",
                severity=severity,
                detail=f"{label} effective pass rate{extras_str}.",
                category="per_tool_pass_rate",
            ))
        return out

    def _check_per_tool_raw_pass_rates(self, coverage_report: dict) -> list[GateCheck]:
        """R42.5 KEYSTONE — RAW per-tool pass rate gate. NO exclusions.

        The user's bar is unambiguous: ≥95% pass rate for every type
        of test script, with zero skips. RAW = passed / total, where
        total is every dispatched row regardless of triage_category,
        BLOCKED status, or cascade origin. No metric-gaming.

        This complements (not replaces) `_check_per_tool_pass_rates`
        which surfaces the effective rate as INFO context. The RAW
        row is what the gate actually decides on:
          - ≥95% raw → PASS (✓)
          - <80% raw → BLOCK (✗)
          - 80–94% raw → WARN (⚠)

        R42.5 supersedes R33.7's BLOCK behaviour — the effective row
        stays as INFO so operators can see "tool quality is fine,
        config gap is the issue" without that being the gate signal.
        """
        try:
            from ..api.routers.execution import _REAL_RUNS
        except Exception:
            return []

        target_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        target_run = None
        if target_run_id and isinstance(_REAL_RUNS, dict):
            cand = _REAL_RUNS.get(target_run_id)
            if isinstance(cand, dict):
                target_run = cand
        if target_run is None and isinstance(_REAL_RUNS, dict):
            runs = [r for r in _REAL_RUNS.values() if isinstance(r, dict)]
            if runs:
                target_run = max(runs, key=lambda r: r.get("started_at") or "")
        if target_run is None:
            return []

        results = target_run.get("results") or []
        if not isinstance(results, list):
            return []

        per_tool: dict[str, dict[str, int]] = {}
        for row in results:
            if not isinstance(row, dict):
                continue
            tool = (row.get("automation_tool") or row.get("tool") or "unknown").lower()
            slot = per_tool.setdefault(tool, {"passed": 0, "failed": 0, "blocked": 0, "skipped": 0, "total": 0})
            slot["total"] += 1
            status = (row.get("status") or "").upper()
            if status == "PASS":
                slot["passed"] += 1
            elif status in ("BLOCKED",):
                slot["blocked"] += 1
            elif status in ("SKIP", "SKIPPED"):
                slot["skipped"] += 1
            else:
                slot["failed"] += 1

        out: list[GateCheck] = []
        for tool, c in sorted(per_tool.items()):
            total = c["total"]
            if total == 0:
                continue
            passed = c["passed"]
            raw_pct = round(100.0 * passed / total, 1)
            target = self._thresholds.per_tool_pass_rate_pct
            if raw_pct >= target:
                severity = "WARN"   # info-only when target is met
                passed_flag = True
            elif raw_pct >= 80.0:
                severity = "WARN"
                passed_flag = False
            else:
                severity = "BLOCK"
                passed_flag = False
            detail = (
                f"RAW pass rate (no exclusions). "
                f"passed={c['passed']} failed={c['failed']} "
                f"blocked={c['blocked']} skipped={c['skipped']}"
            )
            out.append(GateCheck(
                name=f"{tool}_raw_pass_rate",
                passed=passed_flag,
                actual=f"{passed}/{total} ({raw_pct}%)",
                expected=f"≥{target}%",
                severity=severity,
                detail=detail,
                category="raw_pass_rate",
            ))
        return out

    def _check_gen_quality(self, coverage_report: dict) -> list[GateCheck]:
        """R72.3 — Pillar 1 gen-time quality metric.

        Measures the percentage of generated specs that completed
        successfully (no `generation_failure` stamped). Surfaces
        upstream quality regressions BEFORE they manifest as runtime
        failures 30 minutes later.

        Reads `GENERATED_TESTS` directly so no per-tool gen-path
        changes are needed. Each spec already records its generation
        outcome via the existing `generation_failure` field; this
        gate aggregates them into a single signal.

        Severity tiers:
          - BLOCK at <60% first-try gen success — prompt/model
            regression; pipeline is producing mostly broken specs
          - WARN at <80% — gen quality slipping; investigate
          - INFO at ≥80% — healthy

        Detail breaks down by tool so operators see WHICH tool's
        gen is the bottleneck.
        """
        try:
            from ..api.routers.tests import GENERATED_TESTS  # type: ignore
        except Exception:
            return []
        if not GENERATED_TESTS:
            return []

        # R75.5 — scope strictly to the project carried in the coverage
        # report. Pre-R75.5 the gate read GENERATED_TESTS globally when
        # no project_id was present, which contaminated unit tests
        # (test_pass_all_checks_clear) with live state from disk and
        # made the gate return CONCERNS instead of PASS. The check is
        # project-scoped semantically; without project context, skip.
        project_id = (
            coverage_report.get("project_id")
            or coverage_report.get("target_project_id")
        )
        if not project_id:
            return []
        scoped = [
            t for t in GENERATED_TESTS
            if isinstance(t, dict)
            and t.get("project_id") == project_id
        ]
        if not scoped:
            return []

        total = len(scoped)
        clean = sum(
            1 for t in scoped
            if not t.get("generation_failure")
        )
        pct = round(100.0 * clean / total, 1) if total else 0.0

        # Per-tool breakdown for the detail field
        by_tool: dict[str, tuple[int, int]] = {}
        # R74.3 — also track which requirements failed per tool so
        # operators see a hit list, not just an aggregate %. Investigation
        # while 21 other requirements failed — a per-req pattern that
        # would be invisible without surfacing the offending req_ids.
        failed_reqs_by_tool: dict[str, set[str]] = {}
        for t in scoped:
            tool = (t.get("automation_tool") or t.get("tool") or "unknown").lower()
            p, f = by_tool.get(tool, (0, 0))
            if t.get("generation_failure"):
                f += 1
                req_id = t.get("requirement_id") or t.get("req_id")
                if isinstance(req_id, str):
                    failed_reqs_by_tool.setdefault(tool, set()).add(req_id)
            else:
                p += 1
            by_tool[tool] = (p, f)
        breakdown = ", ".join(
            f"{tool}={p}/{p+f} ({round(100.0*p/(p+f), 1) if (p+f) else 0}%)"
            for tool, (p, f) in sorted(by_tool.items())
        )

        # R74.3 hit-list — show up to 5 failing requirements per tool
        # below the WARN threshold (80%). Helps operators jump directly
        # 89 entries to find them.
        hit_list_lines: list[str] = []
        for tool, (p, f) in sorted(by_tool.items()):
            tool_pct = (100.0 * p / (p + f)) if (p + f) else 0.0
            if tool_pct < 80.0 and tool in failed_reqs_by_tool:
                sample = sorted(failed_reqs_by_tool[tool])[:5]
                more = len(failed_reqs_by_tool[tool]) - len(sample)
                hit_list_lines.append(
                    f"{tool} failing reqs: {', '.join(sample)}"
                    + (f" (+{more} more)" if more > 0 else "")
                )
        hit_list = " | ".join(hit_list_lines) if hit_list_lines else ""

        if pct < 60.0:
            severity = "BLOCK"
            passed = False
        elif pct < 80.0:
            severity = "WARN"
            passed = False
        else:
            severity = "INFO"
            passed = True
        return [GateCheck(
            name="gen_quality_first_try_pass_rate",
            passed=passed,
            actual=f"{clean}/{total} ({pct:.1f}%)",
            expected="≥80% (WARN below 80%, BLOCK below 60%)",
            severity=severity,
            detail=(
                f"R72.3 — Pillar 1 gen-time quality. Specs where the "
                f"generation pipeline completed without errors (no "
                f"`generation_failure` stamped). Per-tool: {breakdown}. "
                + (f"R74.3 hit list: {hit_list}. " if hit_list else "")
                + f"A regression here surfaces prompt/model issues BEFORE "
                f"30-min runtime cycles reveal them as failed tests."
            ),
            category="gen_quality",
        )]

    def _check_r102_a_stamp_rate(self, coverage_report: dict) -> list[GateCheck]:
        """R118.H — Pillar 4 visibility: percentage of Playwright specs
        carrying an R102.A `_dispatch_block_kind: playwright_grounding_violation`
        stamp on disk.

        Pre-R118.H: the gate's `gen_quality_first_try_pass_rate` checked
        whether the GEN PIPELINE completed (no `generation_failure`
        stamp), but did NOT count specs whose generation completed AND
        passed validators BUT were R102.A-stamped at retry exhaustion.
        Those specs are excluded from dispatch by R102.C — invisible to
        the gate yet operator-actionable (each is a "regen-me" CTA on
        the dashboard).

        R118.H closes the visibility gap by scanning the on-disk PW
        spec inventory + reporting the stamped ratio with severity:
          - INFO at ≤5% (acceptable noise — happens occasionally)
          - WARN at ≤15% (gen quality slipping; investigate violation_kinds)
          - BLOCK at >15% (gen-quality crisis; trigger bulk regen)

        Mission: operator sees gen-quality degradation BEFORE
        runtime-failure metrics surface it 30 min later. Pairs with
        R118.G's distinct `defect_class="grounding_blocked"` on the
        dashboard tile.
        """
        # R75.5 — scope strictly to the project carried in the coverage
        # report. Pre-R75.5/H this would read the global PW spec dir
        # whenever no project_id was present, contaminating unit tests
        # with live disk state (test_pass_all_checks_clear → FAIL).
        # The metric is project-scoped semantically (each project owns
        # its automation_dir per R113.M.1); without project context, skip.
        _project_id = (
            coverage_report.get("project_id")
            or coverage_report.get("target_project_id")
        )
        if not _project_id:
            return []
        try:
            from pathlib import Path as _Path_r118_h
            pw_dir = _Path_r118_h("src/automation/playwright")
            if not pw_dir.is_dir():
                return []
            total = stamped = 0
            for spec in pw_dir.glob("req_*.spec.ts"):
                # Skip a11y variants — those have their own pipeline +
                # don't carry PW grounding stamps in the same way.
                if spec.name.endswith("_a11y.spec.ts"):
                    continue
                total += 1
                try:
                    head = spec.read_text(errors="replace")[:2000]
                except Exception:
                    continue
                if "_dispatch_block_kind: playwright_grounding_violation" in head:
                    stamped += 1
            if total == 0:
                return []
            ratio = stamped / total
            pct = round(100.0 * ratio, 1)
            if ratio > 0.15:
                severity = "BLOCK"
                passed = False
            elif ratio > 0.05:
                severity = "WARN"
                passed = False
            else:
                severity = "INFO"
                passed = True
            return [GateCheck(
                name="r118_h_grounding_blocked_rate",
                passed=passed,
                actual=f"{stamped}/{total} ({pct}%)",
                expected="≤5% (INFO threshold; WARN ≤15%; BLOCK >15%)",
                severity=severity,
                detail=(
                    f"R118.H — {stamped} of {total} PW specs R102.A-stamped "
                    f"(dispatch-excluded by R102.C). Above 5% indicates "
                    f"gen-quality regression; investigate violation_kinds "
                    f"via the run-detail page or trigger bulk regen via "
                    f"/api/admin/bulk-regen-playwright-grounding. Pairs with "
                    f"R118.G dashboard tile for `grounding_blocked` defects."
                ),
                category="gen_quality",
            )]
        except Exception as _r118_h_exc:
            log.debug("R118.H: stamp-rate check skipped: %s", _r118_h_exc)
            return []

    def _check_bearer_auth_coverage(self, coverage_report: dict) -> list[GateCheck]:
        """R93.5 — Pillar 4 visibility for R91.A/R93.1/R93.A Bearer-injection
        propagation.

        Pre-R93.5, R91.A regressed silently for ~24h because no metric
        surfaced gen-quality propagation: the code was in container,
        deployed, working, but the gating predicate was wrong → specs
        on disk silently stayed pre-R91.A. Operators couldn't see the
        gap. R93.5 makes the gap MEASURABLE per run.

        Reads `GENERATED_TESTS` (scoped to coverage_report.project_id);
        for each Newman + k6 spec, checks `script_content` for
        Authorization-header presence. The R93.1 predicate determines
        WHETHER coverage is expected: pure-cookie legacy projects skip
        this check (no Bearer required); Bearer/mixed-auth projects
        require ≥95%.

        Severity tiers:
          - INFO at ≥95% — healthy
          - WARN at <95% — propagation gap; run bulk-regen-by-tool
          - never BLOCK — the gate's job is to surface, not gate, the
            gen-quality drift
        """
        try:
            from ..api.routers.tests import GENERATED_TESTS  # type: ignore
        except Exception:
            return []
        if not GENERATED_TESTS:
            return []
        project_id = (
            coverage_report.get("project_id")
            or coverage_report.get("target_project_id")
        )
        if not project_id:
            return []
        scoped = [
            t for t in GENERATED_TESTS
            if isinstance(t, dict) and t.get("project_id") == project_id
        ]
        if not scoped:
            return []

        # Group specs by tool; compute Bearer-header presence.
        # Newman: look for "Authorization" substring in script_content
        # (the JSON-encoded collection serialises headers as JSON, so
        # the literal text appears). k6: look for either "Authorization"
        # OR "_auth_r93a" (R93.A backstop's const).
        TOOL_PROBES = {
            "newman": ("Authorization", None),
            "k6": ("Authorization", "_auth_r93a"),
        }

        out: list[GateCheck] = []
        for tool, (primary, secondary) in TOOL_PROBES.items():
            tool_specs = [
                t for t in scoped
                if (t.get("automation_tool") or t.get("tool") or "").lower() == tool
            ]
            if not tool_specs:
                continue
            total = len(tool_specs)
            with_bearer = 0
            for t in tool_specs:
                content = t.get("script_content") or ""
                if not isinstance(content, str):
                    continue
                if primary in content or (secondary and secondary in content):
                    with_bearer += 1
            pct = round(100.0 * with_bearer / total, 1) if total else 0.0
            if pct >= 95.0:
                severity = "INFO"
                passed = True
            else:
                severity = "WARN"
                passed = False
            out.append(GateCheck(
                name=f"{tool}_bearer_auth_coverage_pct",
                passed=passed,
                actual=f"{with_bearer}/{total} ({pct:.1f}%)",
                expected="≥95% (WARN below)",
                severity=severity,
                detail=(
                    f"R93.5 — Pillar 4 visibility for R91.A/R93.1/R93.A "
                    f"Bearer-injection. {tool} specs carrying Authorization "
                    f"header out of total: {with_bearer}/{total} ({pct:.1f}%). "
                    f"{'Healthy — gen-time fix has propagated.' if pct >= 95.0 else f'Below 95% — run POST /api/admin/bulk-regen-{tool}-for-bearer to close the gap.'}"
                ),
                category="gen_quality",
            ))
        return out

    def _check_autofix_rate(self, coverage_report: dict) -> list[GateCheck]:
        """R85.M+R86.M — gen-quality autofix-invocation rate.

        Counts specs that needed an autofix invocation (R51, R57.1, R85.1,
        R86.2, R86.2a) vs specs that came CLEAN from gen. Each autofix
        is a signal that the LLM produced a non-canonical pattern that
        a deterministic validator caught + healed. A HIGH autofix rate
        means LLM quality is degrading; LOW means the prompts are
        landing right.

        The mission says "generate high quality test scripts" — autofix
        rate is the direct measure of how much of "generate high
        quality" is achieved at gen-time vs healed downstream.

        Severity tiers:
          - INFO at ≤10% autofix — healthy; LLM mostly produces clean specs
          - WARN at 10-30% — moderate drift; prompt tuning warranted
          - BLOCK above 30% — gen pipeline degraded; investigate prompts
            / model / OpenAPI cache freshness

        Detail surfaces a per-tool breakdown so operators see WHICH tool's
        gen needs prompt work. Markers detected:
          - R85.1 stamps `// _arta_autofix=R85.1` comment on k6 scripts
          - R86.2a stamps `_arta_meta.injected_by=R86.2a` on Newman header objects
          - R86.2 stamps `_arta_meta.injected_by=R86.2` on Newman item bodies
          - R57.1 + others stamp `info._grounding_violations` on Newman collections
        """
        try:
            from ..api.routers.tests import GENERATED_TESTS  # type: ignore
        except Exception:
            return []
        if not GENERATED_TESTS:
            return []
        project_id = (
            coverage_report.get("project_id")
            or coverage_report.get("target_project_id")
        )
        if not project_id:
            return []
        scoped = [
            t for t in GENERATED_TESTS
            if isinstance(t, dict)
            and t.get("project_id") == project_id
        ]
        if not scoped:
            return []

        # Count autofix per spec, broken down by tool
        by_tool_total: dict[str, int] = {}
        by_tool_autofixed: dict[str, int] = {}
        total = 0
        autofixed = 0
        for t in scoped:
            tool = (t.get("automation_tool") or t.get("tool") or "unknown").lower()
            by_tool_total[tool] = by_tool_total.get(tool, 0) + 1
            total += 1
            content = t.get("script_content") or ""
            if not isinstance(content, str):
                content = ""
            # k6 — explicit R85.1 marker
            if tool == "k6" and "_arta_autofix=R85.1" in content:
                by_tool_autofixed[tool] = by_tool_autofixed.get(tool, 0) + 1
                autofixed += 1
                continue
            # newman — parse JSON + scan for `injected_by` markers on
            # headers and items
            if tool == "newman" and content.strip().startswith("{"):
                try:
                    import json as _json_r85m
                    parsed = _json_r85m.loads(content)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    if "_grounding_violations" in (parsed.get("info") or {}):
                        by_tool_autofixed[tool] = by_tool_autofixed.get(tool, 0) + 1
                        autofixed += 1
                        continue
                    # Walk items recursively for `injected_by` markers
                    def _walk_item(it: object) -> bool:
                        if not isinstance(it, dict):
                            return False
                        meta = (it.get("_arta_meta") or {}) if isinstance(it.get("_arta_meta"), dict) else {}
                        if meta.get("autofix_invocations") or meta.get("injected_by"):
                            return True
                        req = it.get("request") or {}
                        if isinstance(req, dict):
                            for hdr in (req.get("header") or []):
                                if isinstance(hdr, dict) and (hdr.get("_arta_meta") or {}).get("injected_by"):
                                    return True
                            body = req.get("body")
                            if isinstance(body, dict) and (body.get("_arta_meta") or {}).get("injected_by"):
                                return True
                        for sub in (it.get("item") or []):
                            if _walk_item(sub):
                                return True
                        return False
                    for item in (parsed.get("item") or []):
                        if _walk_item(item):
                            by_tool_autofixed[tool] = by_tool_autofixed.get(tool, 0) + 1
                            autofixed += 1
                            break

        if total == 0:
            return []
        pct = round(100.0 * autofixed / total, 1)

        if pct > 30.0:
            severity = "BLOCK"
            passed = False
        elif pct > 10.0:
            severity = "WARN"
            passed = False
        else:
            severity = "INFO"
            passed = True

        breakdown = ", ".join(
            f"{tool}: {by_tool_autofixed.get(tool, 0)}/{by_tool_total[tool]} "
            f"({round(100.0 * by_tool_autofixed.get(tool, 0) / by_tool_total[tool], 1)}%)"
            for tool in sorted(by_tool_total.keys())
        )

        return [GateCheck(
            name="gen_autofix_rate",
            passed=passed,
            actual=f"{autofixed}/{total} ({pct}%) needed autofix",
            expected="≤10% (healthy LLM gen)",
            severity=severity,
            category="gen_quality",
            detail=(
                f"R85.M+R86.M — per-tool autofix invocation rate. Per-tool: "
                f"{breakdown}. A LOWER rate over time = LLM prompts + OpenAPI "
                f"cache freshness produce clean-from-gen specs; HIGH rate = "
                f"the deterministic safety nets (R85.1 bare-object, R86.2 "
                f"runtime CT, R86.2a OpenAPI CT) are catching LLM drift. "
                f"Trend matters more than absolute %."
            ),
        )]

    def _check_zero_skips(self, coverage_report: dict) -> list[GateCheck]:
        """R42.5 — zero-skips gate. The user's bar requires no skips.

        Skip-count includes both BLOCKED (pre-dispatch config gaps)
        and SKIP/SKIPPED (cascade-skipped, fixture-missing, etc.).
        Each is a sign the test didn't run to a real outcome.

        BLOCK severity when count > 0 — drives the team to eliminate
        skips at source rather than tolerate them as "operator config".
        """
        try:
            from ..api.routers.execution import _REAL_RUNS
        except Exception:
            return []

        target_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        target_run = None
        if target_run_id and isinstance(_REAL_RUNS, dict):
            cand = _REAL_RUNS.get(target_run_id)
            if isinstance(cand, dict):
                target_run = cand
        if target_run is None and isinstance(_REAL_RUNS, dict):
            runs = [r for r in _REAL_RUNS.values() if isinstance(r, dict)]
            if runs:
                target_run = max(runs, key=lambda r: r.get("started_at") or "")
        if target_run is None:
            return []

        results = target_run.get("results") or []
        if not isinstance(results, list):
            return []
        skip_count = sum(
            1 for r in results
            if isinstance(r, dict)
            and (r.get("status") or "").upper() in ("BLOCKED", "SKIP", "SKIPPED")
        )
        return [GateCheck(
            name="zero_skips",
            passed=skip_count == 0,
            actual=str(skip_count),
            expected="0",
            severity="BLOCK" if skip_count > 0 else "INFO",
            detail=(
                "No skips — every dispatched test must run to a real "
                "outcome (PASS or FAIL). BLOCKED = pre-dispatch config "
                "gap (fix env vars / paste auth); SKIP = cascade or "
                "fixture-missing (R42.3 chain-replay should eliminate)."
            ),
            category="raw_pass_rate",
        )]

    async def _check_failure_trend(self, coverage_report: dict) -> list[GateCheck]:
        """R57.8 — fail count growth signal.

        Compares current run's `failed` count to the most-recent prior
        completed run within a 14-day window. Severity tiers:
          - INFO when flat or decreasing
          - WARN when growth > 10%
          - BLOCK when growth > 30%

        The user explicitly identified "failures grow every run" (R56) as
        the symptom that motivated this work. Making the trend a gate
        signal closes the visibility loop: operators see the spiral
        directly in the gate decision, not by mentally diffing 7 runs of
        dashboard history.

        Cold-start (no prior run within window) returns []. Async because
        prior-run lookup hits the `test_runs` DB table. Called from
        async post-pipeline (not from `check_quality()` which is sync).
        """
        try:
            from ..api.db_adapter import try_db
            from sqlalchemy import text
        except ImportError:
            return []

        current_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        current_failed = int(coverage_report.get("failed", 0) or 0)
        if not current_run_id:
            return []

        prior_failed: int | None = None
        prior_run_id: str | None = None
        try:
            async with try_db() as db:
                if db:
                    result = await db.execute(
                        text(
                            """
                            SELECT run_id, failed
                              FROM test_runs
                             WHERE status = 'completed'
                               AND run_id <> :current
                               AND started_at > NOW() - INTERVAL '14 days'
                             ORDER BY started_at DESC
                             LIMIT 1
                            """
                        ),
                        {"current": current_run_id},
                    )
                    row = result.first()
                    if row:
                        prior_run_id = row[0]
                        prior_failed = int(row[1] or 0)
        except Exception as exc:
            log.debug("R57.8: prior-run query failed: %s", exc)
            return []

        if prior_failed is None:
            # Cold-start project — trend gate cannot fire meaningfully.
            return []

        delta_abs = current_failed - prior_failed
        delta_pct = (delta_abs / max(prior_failed, 1)) * 100.0
        if delta_pct > 30.0:
            severity = "BLOCK"
            passed = False
        elif delta_pct > 10.0:
            severity = "WARN"
            passed = False
        else:
            severity = "INFO"
            passed = True

        detail = (
            f"Current run failed={current_failed}; prior run "
            f"({prior_run_id}) failed={prior_failed}; "
            f"delta={delta_abs:+d} ({delta_pct:+.1f}%). The fail count "
            f"must trend DOWN or stay flat over consecutive runs — growth "
            f"indicates the gen-time or heal-loop isn't catching up to the "
            f"failure surface."
        )
        return [GateCheck(
            name="failure_trend",
            passed=passed,
            actual=f"{current_failed} fails ({delta_pct:+.1f}% vs prior)",
            expected="≤ +10% (WARN at +10%, BLOCK at +30%)",
            severity=severity,
            detail=detail,
            category="trend",
        )]

    def _check_traceability_health(self, coverage_report: dict) -> list[GateCheck]:
        """R30.6 — emit WARN row when any requirement has tests dispatched
        but ZERO ExecutionResult edges in Neo4j (broken trace chain).

        Reads `traceability_chain_health` stamped by R30.6's post-run
        helper. WARN-only — never blocks because the chain may rebuild
        on the next run after R29.1 catches up.
        """
        try:
            from ..api.routers.execution import _REAL_RUNS
        except Exception:
            return []
        target_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        target_run = None
        if target_run_id and isinstance(_REAL_RUNS, dict):
            cand = _REAL_RUNS.get(target_run_id)
            if isinstance(cand, dict):
                target_run = cand
        if target_run is None:
            return []
        health = target_run.get("traceability_chain_health") or {}
        broken = health.get("requirements_without_results") or []
        if not broken:
            return []
        sample = broken[:5]
        more = max(0, len(broken) - len(sample))
        return [GateCheck(
            name="traceability_chain_health",
            passed=False,
            actual=f"{len(broken)} requirement(s) with no Result edges",
            expected="0",
            severity="WARN",
            detail=(
                f"{len(broken)} requirement(s) had test cases dispatched "
                f"but zero ExecutionResult nodes in Neo4j for this run. "
                f"Likely cause: R29.1 didn't run (Neo4j down at post-run "
                f"time), or test cases never registered with their "
                f"requirement. Sample: {', '.join(sample)}"
                + (f" + {more} more" if more else "") + ". "
                f"Operator action: inspect /traceability?run_id="
                f"{target_run_id} to confirm the chain rendered."
            ),
            category="traceability",
        )]

    def _check_endpoint_coverage(self, coverage_report: dict) -> list[GateCheck]:
        """R55.13 — endpoint coverage gate row.

        Reads `traceability_chain_health.endpoint_coverage` stamped by the
        post-pipeline `_compute_endpoint_coverage` helper. INFO when
        coverage >=70%; WARN below. Never BLOCKs — cold-start projects
        legitimately have low coverage on early runs, and only Newman
        endpoints are tracked today (R55.7 — PW/k6/axe/zap land in R60+).

        Returns empty list when no Endpoint nodes exist in scope (cold
        start; chain ingestion hasn't fired yet).
        """
        try:
            from ..api.routers.execution import _REAL_RUNS
        except Exception:
            return []
        target_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        if not target_run_id or not isinstance(_REAL_RUNS, dict):
            return []
        run = _REAL_RUNS.get(target_run_id) or {}
        ep_cov = (run.get("traceability_chain_health") or {}).get(
            "endpoint_coverage"
        ) or {}
        total = int(ep_cov.get("total_endpoints") or 0)
        if total == 0:
            return []
        covered = int(ep_cov.get("covered_endpoints") or 0)
        pct = float(ep_cov.get("coverage_pct") or 0.0)
        sample_uncov = ep_cov.get("sample_uncovered") or []
        passed = pct >= 70.0
        severity = "INFO" if passed else "WARN"
        return [GateCheck(
            name="endpoint_coverage",
            passed=passed,
            actual=f"{covered}/{total} endpoints ({pct:.1f}%)",
            expected="≥70% (WARN below)",
            severity=severity,
            detail=(
                f"R55.13 — {covered} of {total} SUT endpoints exercised "
                f"by ≥1 test that ran in this run. Endpoint linkage "
                f"currently captured for Newman only (R55.7); PW/k6/axe/"
                f"zap linkage in R60+. Sample uncovered: "
                f"{sample_uncov[:3] if sample_uncov else '[]'}"
            ),
            category="traceability",
        )]

    def _check_spec_drift(self, coverage_report: dict) -> list[GateCheck]:
        """R29.5 — emit WARN row when generated Playwright/axe specs
        reference API endpoints that don't appear in the project's
        captured-endpoints store.

        R32.3 — reads from the durable_state Redis-backed store first
        (multi-worker correctness), falls back to the in-memory module
        dict when Redis is down. Pre-R32.3 multi-worker deployments
        had each worker carrying its own _SPEC_DRIFT_TARGETS copy and
        the gate decision differed by which worker handled the request.

        WARN-only — never blocks a run because the LLM may have made
        a legitimate guess.
        """
        # R32.3 — try durable store first. Multi-worker safe.
        project_id = coverage_report.get("project_id") or ""
        durable_targets: set[str] = set()
        if project_id:
            try:
                import asyncio as _asyncio
                from ..api.services.durable_state import list_spec_drift as _list_drift
                try:
                    loop = _asyncio.get_event_loop()
                    if loop.is_running():
                        # Caller is async — schedule + read in same coroutine
                        # not possible synchronously here; fall through to
                        # in-memory below. Async callers should use the
                        # async version of this check (future improvement).
                        durable_targets = set()
                    else:
                        durable_targets = loop.run_until_complete(
                            _list_drift(project_id)
                        )
                except RuntimeError:
                    durable_targets = _asyncio.run(_list_drift(project_id))
            except Exception:
                durable_targets = set()

        try:
            from .automation_engineer import _SPEC_DRIFT_TARGETS
        except Exception:
            _SPEC_DRIFT_TARGETS = {}
        # Aggregate across all projects (single-tenant runs); the gate
        # runs per-project so this is precise enough.
        memory_targets: set[str] = set()
        for paths in (_SPEC_DRIFT_TARGETS or {}).values():
            memory_targets.update(paths)
        all_targets = durable_targets | memory_targets
        total = len(all_targets)
        if total < 5:
            return []
        sample = sorted(all_targets)[:5]
        return [GateCheck(
            name="spec_drift_targets",
            passed=False,
            actual=f"{total} unobserved API targets",
            expected="0 (or run discovery to observe them)",
            severity="WARN",
            detail=(
                f"Generated specs reference {total} API endpoint(s) that "
                f"don't appear in the project's captured-endpoints store. "
                f"Likely cause: OpenAPI spec drift, OR discovery hasn't "
                f"yet observed these endpoints. Sample: "
                f"{', '.join(sample[:5])}. Operator action: click "
                f"'Re-run discovery' to observe missing endpoints, or "
                f"verify they exist in the SUT."
            ),
            category="spec_drift",
        )]

    def _check_blocked_count(self, coverage_report: dict) -> list[GateCheck]:
        """R29.3d — separate Configuration-completeness gate row.

        When ≥1 BLOCKED row exists, surface a WARN/FAIL distinct from
        the pass-rate check. BLOCKED reflects operator-fixable gaps
        (missing env vars at dispatch time, R29.3a pre-dispatch filter)
        — gate decision shouldn't punish test quality for what's
        actually a config gap, but operators DO need to see the gap.

        Threshold: <50 BLOCKED → WARN (still investigatable), ≥50
        BLOCKED → FAIL (the config gap is large enough to block the
        run from being meaningful). This matches the operator-action
        cost: a handful of unfilled vars is a quick fix; 50+ means
        Discovery hasn't run for this project.
        """
        try:
            from ..api.routers.execution import _REAL_RUNS
        except Exception:
            return []
        target_run_id = (
            coverage_report.get("run_id")
            or coverage_report.get("build_id")
            or coverage_report.get("target_run_id")
        )
        target_run = None
        if target_run_id and isinstance(_REAL_RUNS, dict):
            cand = _REAL_RUNS.get(target_run_id)
            if isinstance(cand, dict):
                target_run = cand
        if target_run is None and isinstance(_REAL_RUNS, dict):
            runs = [r for r in _REAL_RUNS.values() if isinstance(r, dict)]
            if runs:
                target_run = max(
                    runs, key=lambda r: r.get("started_at") or "",
                )
        if target_run is None:
            return []
        blocked = sum(
            1 for r in (target_run.get("results") or [])
            if isinstance(r, dict) and r.get("status") == "BLOCKED"
        )
        if blocked == 0:
            return []
        # Aggregate the union of blocked_vars across rows so the
        # operator sees WHICH env vars need filling.
        blocked_vars: set[str] = set()
        for r in (target_run.get("results") or []):
            if isinstance(r, dict) and r.get("status") == "BLOCKED":
                for v in (r.get("blocked_vars") or []):
                    blocked_vars.add(str(v))
        sample = sorted(blocked_vars)[:8]
        more = max(0, len(blocked_vars) - len(sample))
        severity = "BLOCK" if blocked >= 50 else "WARN"
        return [GateCheck(
            name="configuration_completeness",
            passed=False,
            actual=f"{blocked} blocked",
            expected="0 blocked",
            severity=severity,
            detail=(
                f"{blocked} test items did NOT dispatch because required "
                f"env vars are unresolved. Operator action: fill via "
                f"Settings → Environments → Variables for the project, "
                f"or click 'Re-run discovery' in the Discovery panel to "
                f"harvest values from a Playwright run. Affected vars "
                f"({len(blocked_vars)}): {', '.join(sample)}"
                + (f" + {more} more" if more else "") + "."
            ),
            category="configuration",
        )]

    def _check_defect_policy(self, defects: list[dict]) -> list[GateCheck]:
        # R-GateDefectPriority — pre-fix this checked `severity == "P0"`,
        # but `severity` is `critical`/`high`/`medium`/`low`; the P0/P1
        # values live in `priority`. The check was tautological — open_p0
        # was always 0 because no defect's `severity` field contains
        # "P0". Verified live against run-c87ee5: 2 priority=P0 defects
        # in DB but the gate badge showed `Open P0 defects: 0 ≤ 0 ✓`.
        # Now check BOTH `priority == "P0"` (operator's escalation level)
        # AND `severity == "critical"` (engineering-severity tag) so
        # either signal blocks the release.
        def _is_p0(d: dict) -> bool:
            if d.get("status") != "open":
                return False
            return d.get("priority") == "P0" or d.get("severity") == "critical"

        def _is_p1(d: dict) -> bool:
            if d.get("status") != "open":
                return False
            return d.get("priority") == "P1" or d.get("severity") == "high"

        open_p0 = sum(1 for d in defects if _is_p0(d))
        open_p1 = sum(1 for d in defects if _is_p1(d))

        return [
            GateCheck(
                name="open_p0_defects",
                passed=open_p0 <= self._thresholds.max_open_p0_defects,
                actual=str(open_p0),
                expected=f"\u2264{self._thresholds.max_open_p0_defects}",
                severity="BLOCK",
                detail="Open P0 defects block all releases",
                category="defect",
            ),
            GateCheck(
                name="open_p1_defects",
                passed=open_p1 <= self._thresholds.max_open_p1_defects,
                actual=str(open_p1),
                expected=f"\u2264{self._thresholds.max_open_p1_defects}",
                severity="WARN",
                category="defect",
            ),
        ]

    def _check_sut_quality(
        self,
        defects: list[dict],
        coverage_report: dict,
    ) -> list[GateCheck]:
        """R37.7 \u2014 gate the build on SUT quality, not just test quality.

        Three rows:
          - new_sut_regressions: how many sut_regression defects were
            opened in the most-recent 24h. Above the threshold = the
            SUT got worse this build.
          - critical_sut_age: oldest open P0 sut_regression's age in
            hours. Above the threshold = SUT team isn't responding to
            critical detected bugs fast enough.
          - sut_health_pct: passing tests / (passing + sut_regression
            failures). 100% means ARTA detected zero SUT bugs in this
            run; lower numbers quantify the bug surface.

        These complement, not replace, the per-tool pass-rate gate
        (R33.7). Both must pass for the gate to be green:
        per-tool \u226595% effective AND SUT didn't regress.
        """
        from datetime import datetime, timedelta, timezone

        def _is_open(d: dict) -> bool:
            return d.get("status") in ("open", "OPEN", None)

        def _triage_cat(d: dict) -> str | None:
            md = d.get("metadata") or {}
            if not isinstance(md, dict):
                md = {}
            return (
                d.get("triage_category")
                or md.get("triage_category")
                or (d.get("triage") or {}).get("triage_category")
            )

        def _is_sut_reg(d: dict) -> bool:
            return _triage_cat(d) == "sut_regression" and _is_open(d)

        # -- new_sut_regressions: opened in last 24h
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        new_sut_count = 0
        oldest_p0_age_h: float | None = None
        for d in defects:
            if not isinstance(d, dict):
                continue
            if not _is_sut_reg(d):
                continue
            created_at = d.get("created_at")
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    created_at = None
            if isinstance(created_at, datetime):
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at >= cutoff:
                    new_sut_count += 1
                age_h = (now - created_at).total_seconds() / 3600.0
                if d.get("priority") == "P0" or d.get("severity") == "critical":
                    if oldest_p0_age_h is None or age_h > oldest_p0_age_h:
                        oldest_p0_age_h = age_h

        # -- sut_health_pct from coverage_report
        passed = int(coverage_report.get("passed") or 0)
        sut_failures = 0
        for d in defects:
            if isinstance(d, dict) and _is_sut_reg(d):
                affected = (d.get("metadata") or {}).get("affected_tests") if isinstance(d.get("metadata"), dict) else None
                if isinstance(affected, list) and affected:
                    sut_failures += len(affected)
                else:
                    sut_failures += 1
        denom = passed + sut_failures
        sut_health_pct = round(100.0 * passed / denom, 1) if denom > 0 else None

        rows: list[GateCheck] = []
        rows.append(GateCheck(
            name="new_sut_regressions",
            passed=new_sut_count <= self._thresholds.max_new_sut_regressions,
            actual=str(new_sut_count),
            expected=f"\u2264{self._thresholds.max_new_sut_regressions}",
            severity="WARN",
            detail=(
                f"{new_sut_count} new sut_regression defect(s) opened in "
                f"the last 24h. Above the threshold means the SUT got "
                f"worse this build \u2014 operator should triage in Defect "
                f"Intelligence."
            ),
            category="sut_quality",
        ))
        if oldest_p0_age_h is not None:
            critical_passed = oldest_p0_age_h <= self._thresholds.max_critical_sut_age_hours
            rows.append(GateCheck(
                name="critical_sut_age",
                passed=critical_passed,
                actual=f"{round(oldest_p0_age_h, 1)}h",
                expected=f"\u2264{self._thresholds.max_critical_sut_age_hours}h",
                severity="WARN" if critical_passed else "BLOCK",
                detail=(
                    "Open P0 sut_regression older than threshold \u2014 SUT "
                    "team has not responded to a critical detected bug. "
                    "Check the linked Jira ticket(s) on the Defect "
                    "Intelligence panel."
                ),
                category="sut_quality",
            ))
        if sut_health_pct is not None:
            rows.append(GateCheck(
                name="sut_health_pct",
                # Informational only \u2014 operator-tunable per project,
                # don't block the gate on this rolling metric.
                passed=True,
                actual=f"{sut_health_pct}%",
                expected="trend \u2191",
                severity="INFO",
                detail=(
                    f"SUT health = {sut_health_pct}% (passing tests / "
                    f"(passing + sut_regression-classified failures)). "
                    "Tracks whether the SUT is getting better over time; "
                    "see /api/sut/quality-score for the 30-day trend."
                ),
                category="sut_quality",
            ))
        return rows

    def _check_nfr_performance(self, nfr: dict) -> list[GateCheck]:
        """NFR Category 1: Performance — p95, p99, error rate."""
        checks = []
        if nfr.get("performance_p95_ms") is not None:
            p95 = nfr["performance_p95_ms"]
            checks.append(GateCheck(
                name="performance_p95",
                passed=p95 <= self._thresholds.performance_p95_ms,
                actual=f"{p95}ms",
                expected=f"\u2264{self._thresholds.performance_p95_ms}ms",
                severity="BLOCK",
                detail="p95 response time must meet SLA",
                category="nfr_performance",
            ))
        if nfr.get("performance_p99_ms") is not None:
            p99 = nfr["performance_p99_ms"]
            checks.append(GateCheck(
                name="performance_p99",
                passed=p99 <= self._thresholds.performance_p99_ms,
                actual=f"{p99}ms",
                expected=f"\u2264{self._thresholds.performance_p99_ms}ms",
                severity="WARN",
                detail="p99 response time should meet extended SLA",
                category="nfr_performance",
            ))
        if nfr.get("error_rate_pct") is not None:
            rate = nfr["error_rate_pct"]
            checks.append(GateCheck(
                name="error_rate",
                passed=rate <= self._thresholds.performance_error_rate_pct,
                actual=f"{rate:.2f}%",
                expected=f"\u2264{self._thresholds.performance_error_rate_pct}%",
                severity="BLOCK",
                detail="Error rate must be below threshold",
                category="nfr_performance",
            ))
        return checks

    def _check_nfr_security(self, nfr: dict) -> list[GateCheck]:
        """NFR Category 2: Security — OWASP, auth bypass, token expiry."""
        checks = []
        if nfr.get("security_critical_findings") is not None:
            findings = nfr["security_critical_findings"]
            checks.append(GateCheck(
                name="critical_security_findings",
                passed=findings <= self._thresholds.max_critical_security_findings,
                actual=str(findings),
                expected=f"\u2264{self._thresholds.max_critical_security_findings}",
                severity="BLOCK",
                category="nfr_security",
            ))
        if nfr.get("security_high_findings") is not None:
            findings = nfr["security_high_findings"]
            checks.append(GateCheck(
                name="high_security_findings",
                passed=findings <= self._thresholds.max_high_security_findings,
                actual=str(findings),
                expected=f"\u2264{self._thresholds.max_high_security_findings}",
                severity="WARN",
                category="nfr_security",
            ))
        if nfr.get("auth_bypass_tested") is not None and self._thresholds.require_auth_bypass_test:
            tested = nfr["auth_bypass_tested"]
            checks.append(GateCheck(
                name="auth_bypass_test",
                passed=bool(tested),
                actual="tested" if tested else "not tested",
                expected="tested",
                severity="BLOCK",
                detail="Auth bypass testing required per OWASP top-10",
                category="nfr_security",
            ))
        if nfr.get("token_expiry_minutes") is not None:
            expiry = nfr["token_expiry_minutes"]
            checks.append(GateCheck(
                name="token_expiry",
                passed=expiry <= 15,
                actual=f"{expiry}min",
                expected="\u226415min",
                severity="WARN",
                detail="BMAD TEA: token expiry should be \u226415 minutes",
                category="nfr_security",
            ))
        return checks

    def _check_nfr_reliability(self, nfr: dict) -> list[GateCheck]:
        """NFR Category 3: Reliability — graceful degradation, retry logic, circuit breakers, health checks."""
        checks = []
        if nfr.get("has_health_checks") is not None and self._thresholds.require_health_checks:
            has_hc = nfr["has_health_checks"]
            checks.append(GateCheck(
                name="health_checks",
                passed=bool(has_hc),
                actual="present" if has_hc else "missing",
                expected="present",
                severity="WARN",
                detail="Health check endpoints required for reliability",
                category="nfr_reliability",
            ))
        if nfr.get("retry_logic") is not None:
            checks.append(GateCheck(
                name="retry_logic",
                passed=bool(nfr["retry_logic"]),
                actual="implemented" if nfr["retry_logic"] else "missing",
                expected="implemented",
                severity="WARN",
                detail="BMAD TEA requires retry logic (3 attempts) for external calls",
                category="nfr_reliability",
            ))
        if nfr.get("circuit_breakers") is not None:
            checks.append(GateCheck(
                name="circuit_breakers",
                passed=bool(nfr["circuit_breakers"]),
                actual="implemented" if nfr["circuit_breakers"] else "missing",
                expected="implemented",
                severity="WARN",
                detail="Circuit breakers required for external dependencies",
                category="nfr_reliability",
            ))
        return checks

    def _check_nfr_maintainability(self, nfr: dict) -> list[GateCheck]:
        """NFR Category 4: Maintainability — code coverage, duplication, structured logging."""
        checks = []
        if nfr.get("code_coverage_pct") is not None:
            cov = nfr["code_coverage_pct"]
            checks.append(GateCheck(
                name="code_coverage",
                passed=cov >= self._thresholds.min_code_coverage_pct,
                actual=f"{cov:.1f}%",
                expected=f"\u2265{self._thresholds.min_code_coverage_pct}%",
                severity="WARN",
                detail="BMAD TEA requires \u226580% code coverage",
                category="nfr_maintainability",
            ))
        if nfr.get("duplication_pct") is not None:
            dup = nfr["duplication_pct"]
            checks.append(GateCheck(
                name="code_duplication",
                passed=dup <= self._thresholds.max_duplication_pct,
                actual=f"{dup:.1f}%",
                expected=f"\u2264{self._thresholds.max_duplication_pct}%",
                severity="WARN",
                detail="Code duplication should be below threshold",
                category="nfr_maintainability",
            ))
        if nfr.get("critical_vulns") is not None:
            vulns = nfr["critical_vulns"]
            checks.append(GateCheck(
                name="critical_vulnerabilities",
                passed=vulns == 0,
                actual=str(vulns),
                expected="0",
                severity="BLOCK",
                detail="Zero critical vulnerabilities allowed",
                category="nfr_maintainability",
            ))
        return checks

    def _check_risk_auto_fail(
        self, risk_profiles: list[dict], coverage_report: dict
    ) -> list[GateCheck]:
        """BMAD TEA: Any requirement with risk_score=9 must have FULL coverage, else auto-FAIL."""
        checks = []
        for rp in risk_profiles:
            score = rp.get("risk_score", 0)
            if score == 9:
                req_id = rp.get("requirement_id", "unknown")
                by_priority = coverage_report.get("coverage_by_priority", {})
                p0_cov = by_priority.get("P0", {}).get("coverage_pct", 0.0)
                checks.append(GateCheck(
                    name=f"auto_fail_score9_{req_id}",
                    passed=p0_cov >= 100.0,
                    actual=f"{p0_cov:.1f}%",
                    expected="=100%",
                    severity="BLOCK",
                    detail=f"Risk score=9 ({req_id}): requires 100% coverage — automatic FAIL if unmet",
                    category="risk",
                ))
        return checks

    def _check_priority_coverage_targets(
        self, coverage_report: dict, risk_profiles: list[dict]
    ) -> list[GateCheck]:
        """BMAD canonical: per-requirement AC coverage must hit P0=100%, P1=90%,
        P2=50%, P3=20%. Reads per-AC coverage_state from coverage_report["acs"]
        (FULL=100, PARTIAL=50, NONE=0), groups by requirement, compares to the
        target stored on each RiskProfile.

        Stricter than the per-priority-bucket check in `_check_coverage` because
        a single P0 requirement at 50% would average out under bucket math but
        is a release blocker under canonical rule.
        """
        acs = coverage_report.get("acs") or []
        if not acs:
            return []  # No per-AC data — nothing to enforce at this granularity
        score_map = {"FULL": 100, "PARTIAL": 50, "NONE": 0}
        by_req: dict[str, list[dict]] = {}
        for ac in acs:
            by_req.setdefault(ac.get("requirement_id"), []).append(ac)
        # Index target % by requirement_id from risk_profiles
        target_by_req = {
            p.get("requirement_id"): (p.get("coverage_target_pct"), p.get("priority", "P3"))
            for p in (risk_profiles or [])
        }
        checks: list[GateCheck] = []
        for req_id, ac_list in by_req.items():
            target, priority = target_by_req.get(req_id, (None, "P3"))
            if target is None:
                continue
            actual_pct = sum(score_map.get(a.get("coverage_state", "NONE"), 0)
                             for a in ac_list) / len(ac_list)
            if actual_pct >= target:
                continue
            severity = "BLOCK" if priority == "P0" else (
                       "WARN" if priority == "P1" else "INFO")
            checks.append(GateCheck(
                name=f"coverage_target_{req_id}",
                passed=False,
                actual=f"{actual_pct:.0f}%",
                expected=f"≥{target}% ({priority})",
                severity=severity,
                detail=(
                    f"BMAD canonical: {priority} requirement {req_id} requires "
                    f"{target}% AC coverage. Actual: {actual_pct:.0f}% across "
                    f"{len(ac_list)} AC(s)."
                ),
                category="coverage",
            ))
        return checks

    def _check_uncovered_acs(
        self, coverage_report: dict, risk_profiles: list[dict]
    ) -> list[GateCheck]:
        """Fix UUU (Phase G) + Phase 5.2/5.3: emit a gate check for any AC that
        has ZERO linked test cases. UUU surfaces the dashed `UNCOVERED` state
        shown in the reference traceability graphic.

        Phase 5.2 — Single-source unification: prefer `coverage_report["gaps"]`
        (output of GAPS_QUERY in traceability_agent — the canonical "AC has no
        :COVERS edge" set in Neo4j) over the derived `coverage_state == "NONE"`
        from PER_AC_COVERAGE_QUERY. The two used to be independently computed
        and could drift; now `gaps` is the single source and the `acs` list
        is only a backwards-compat fallback.

        Phase 5.3 — Degraded coverage handling: when the report has
        `degraded == True` (Neo4j was down at compute time), emit a CONCERNS
        check rather than passing/failing on stub data.

        Severity rules (matches BMAD spec):
        - any P0 AC uncovered → BLOCK (FAIL gate)
        - any P1 AC uncovered → WARN (CONCERNS gate)
        - P2/P3 → INFO (visible, non-gating)
        """
        # Phase 5.3 — Degraded coverage from Neo4j-down: surface CONCERNS so
        # operators investigate rather than rubber-stamping a stub.
        if coverage_report.get("degraded"):
            return [GateCheck(
                name="coverage_degraded",
                passed=False,
                actual=coverage_report.get("degraded_reason", "Neo4j unavailable"),
                expected="Neo4j-backed authoritative coverage",
                severity="WARN",
                detail=(
                    "Coverage report is in stub mode — UNCOVERED detection "
                    "cannot be trusted until Neo4j is restored. Treat as "
                    "CONCERNS until the graph is queryable again."
                ),
                category="coverage",
            )]

        priority_by_req = {
            (p.get("requirement_id") or ""): p.get("priority", "P3")
            for p in (risk_profiles or [])
        }
        checks: list[GateCheck] = []
        uncovered_p0: list[str] = []
        uncovered_p1: list[str] = []
        uncovered_other: list[str] = []

        # Phase 5.2 — Prefer the canonical `gaps` list (GAPS_QUERY output).
        gaps = coverage_report.get("gaps")
        if isinstance(gaps, list) and gaps:
            for g in gaps:
                if not isinstance(g, dict):
                    continue
                req_id = g.get("requirement_id") or ""
                priority = (g.get("priority") or priority_by_req.get(req_id, "P3")).upper()
                for ac in g.get("uncovered_criteria") or []:
                    ac_id = ac.get("id") if isinstance(ac, dict) else (ac if isinstance(ac, str) else "?")
                    label = f"{req_id}::{ac_id}"
                    if priority == "P0":
                        uncovered_p0.append(label)
                    elif priority == "P1":
                        uncovered_p1.append(label)
                    else:
                        uncovered_other.append(label)
        else:
            # Backwards-compat fallback: derive uncovered set from per-AC state.
            acs = coverage_report.get("acs") or []
            if not acs:
                return []
            for ac in acs:
                state = ac.get("coverage_state", "NONE")
                if state != "NONE":
                    continue
                req_id = ac.get("requirement_id", "")
                ac_id = ac.get("id") or ac.get("ac_id") or "?"
                priority = priority_by_req.get(req_id, "P3")
                label = f"{req_id}::{ac_id}"
                if priority == "P0":
                    uncovered_p0.append(label)
                elif priority == "P1":
                    uncovered_p1.append(label)
                else:
                    uncovered_other.append(label)
        if uncovered_p0:
            checks.append(GateCheck(
                name="uncovered_p0_acs",
                passed=False,
                actual=f"{len(uncovered_p0)} P0 AC(s) UNCOVERED: {uncovered_p0[:5]}",
                expected="0",
                severity="BLOCK",
                detail="P0 acceptance criteria must have at least one test case",
                category="coverage",
            ))
        if uncovered_p1:
            checks.append(GateCheck(
                name="uncovered_p1_acs",
                passed=False,
                actual=f"{len(uncovered_p1)} P1 AC(s) UNCOVERED",
                expected="0",
                severity="WARN",
                detail="P1 acceptance criteria should have at least one test case",
                category="coverage",
            ))
        if uncovered_other and not (uncovered_p0 or uncovered_p1):
            # Only surface as INFO when no higher priority gaps exist.
            checks.append(GateCheck(
                name="uncovered_p2p3_acs",
                passed=False,
                actual=f"{len(uncovered_other)} P2/P3 AC(s) UNCOVERED",
                expected="0",
                severity="INFO",
                detail="P2/P3 ACs uncovered (informational)",
                category="coverage",
            ))
        return checks

    def _check_risk_concerns(
        self, risk_profiles: list[dict], strategy: dict | None
    ) -> list[GateCheck]:
        """BMAD canonical: score 6-8 requires a documented mitigation plan;
        absent plan triggers CONCERNS (not BLOCK).

        Reads `strategy["mitigation_plans"]` — a dict keyed by requirement_id.
        Empty/missing plan on a 6-8 score requirement emits a WARN check.
        """
        plans = (strategy or {}).get("mitigation_plans") or {}
        checks: list[GateCheck] = []
        for prof in risk_profiles or []:
            score = prof.get("risk_score", 0)
            if not (6 <= score <= 8):
                continue
            req_id = prof.get("requirement_id", "?")
            plan = plans.get(req_id)
            if plan and str(plan).strip():
                continue
            checks.append(GateCheck(
                name=f"risk_concern_{req_id}",
                passed=False,
                actual="no mitigation plan",
                expected="documented mitigation plan",
                severity="WARN",
                detail=(
                    f"BMAD canonical: risk score {score} ({req_id}) requires a "
                    "documented mitigation plan; record it in the strategy artifact "
                    "(strategy.mitigation_plans[req_id])."
                ),
                category="risk",
            ))
        return checks

    def _check_test_quality(self, coverage_report: dict) -> list[GateCheck]:
        """BMAD RV workflow: surface test-quality anti-patterns (hard waits,
        manual sleeps, conditional assertions) as a CONCERNS dimension.

        Reads `coverage_report["test_quality"]` populated by execution.py
        (or the orchestrator) via AutomationEngineerAgent.scan_test_quality(...).
        Format:
            {"score": float, "scanned_files": int, "violations": [...]}
        Below 80 → WARN (CONCERNS); below 50 → BLOCK. No data → silent skip.
        """
        tq = coverage_report.get("test_quality")
        if not isinstance(tq, dict) or "score" not in tq:
            return []
        score = float(tq.get("score", 100.0))
        viol_count = len(tq.get("violations") or [])
        if score >= 80.0:
            severity = "INFO"
        elif score >= 50.0:
            severity = "WARN"
        else:
            severity = "BLOCK"
        sample = tq.get("violations", [])[:3]
        return [GateCheck(
            name="test_quality_score",
            passed=score >= 80.0,
            actual=f"{score:.0f} ({viol_count} violation(s))",
            expected="≥80",
            severity=severity,
            detail=(
                "BMAD RV: tests must be deterministic, isolated, explicit. "
                "Hard waits / sleeps / conditional assertions reduce reliability. "
                f"Examples: {sample}"
            ),
            category="test_quality",
        )]

    def _check_traceability_smells(self, coverage_report: dict) -> list[GateCheck]:
        """Layer 7: orphan tests and redundant tests are quality smells, not
        release blockers. WARN-severity. Counts come from coverage_report
        populated by TraceabilityAgent (Gap 2 + Gap 2b)."""
        checks: list[GateCheck] = []
        orphan_count = coverage_report.get("orphan_count", 0)
        if orphan_count:
            sample_ids = [o.get("test_id", "?") for o in
                          (coverage_report.get("orphans") or [])[:3]]
            checks.append(GateCheck(
                name="orphan_tests",
                passed=False,
                actual=f"{orphan_count} orphan(s)",
                expected="0 orphans",
                severity="WARN",
                detail=(
                    "TestCases not linked to any AcceptanceCriteria. "
                    f"Examples: {sample_ids}. POST /api/traceability/orphans/cleanup "
                    "to remove."
                ),
                category="coverage",
            ))
        redundant_count = coverage_report.get("redundant_count", 0)
        if redundant_count:
            sample = [(r.get("ac_id"), r.get("tool"), r.get("test_count")) for r in
                      (coverage_report.get("redundant") or [])[:3]]
            checks.append(GateCheck(
                name="redundant_tests",
                passed=False,
                actual=f"{redundant_count} AC(s) over-tested",
                expected="<3 tests of same tool per AC",
                severity="WARN",
                detail=(
                    "Same AC covered by ≥3 TestCases of one tool — wasted "
                    f"compute + drift surface. Examples: {sample}."
                ),
                category="coverage",
            ))
        return checks

    def _check_recipe_verified(self, risk_profiles: list[dict]) -> list[GateCheck]:
        """Phase 4.5 — Verify the dataset recipe's promises hold.

        After Phase 4 wired closed-loop verification into DatasetRecipeAgent.design(),
        each persisted recipe carries `verification_attempts`, `verification_strategy`,
        and `verification_failed`. This check pulls those flags from
        .arta/recipes/{req_slug}_v{ver}.json sidecars (one per analytics
        requirement) and flags any recipe that couldn't be verified within
        the iteration budget.

        Severity: BLOCK on `verification_failed=true` for P0 reqs (data
        provably can't satisfy assertions); WARN for P1+; pass-through when
        no recipe sidecar exists (non-analytics req)."""
        checks: list[GateCheck] = []
        try:
            import json as _json
            from pathlib import Path as _Path
            recipes_dir = _Path(".arta/recipes")
            if not recipes_dir.is_dir():
                return checks
            for prof in risk_profiles or []:
                if not isinstance(prof, dict):
                    continue
                req_id = prof.get("requirement_id") or prof.get("id")
                if not req_id:
                    continue
                # Find any sidecar for this req (try common version slugs)
                req_slug = sanitize_req_id(str(req_id))
                # Phase 5 follow-up #3 — numeric version sort. Alphabetical
                # `sorted()` puts `req_v10_0_0` BEFORE `req_v9_0_0` lexically,
                # so the gate would silently read v9's verification metadata
                # for any project that bumped to v10+. Convert each filename
                # stem to a tuple of ints for proper semver-ish ordering.
                def _version_key(p):
                    try:
                        ver = p.stem.rsplit("_v", 1)[1]  # 'req_v1_2_3' → '1_2_3'
                        return tuple(int(x) for x in ver.split("_"))
                    except (IndexError, ValueError):
                        return (0,)
                candidates = sorted(
                    recipes_dir.glob(f"{req_slug}_v*.json"),
                    key=_version_key,
                )
                if not candidates:
                    continue
                try:
                    recipe = _json.loads(candidates[-1].read_text())
                except Exception:
                    continue
                strategy = recipe.get("verification_strategy", "")
                attempts = recipe.get("verification_attempts", 0)
                failed = bool(recipe.get("verification_failed"))
                priority = (prof.get("priority") or "P2").upper()
                gap_count = len(recipe.get("verification_residual_gaps") or [])
                if failed:
                    checks.append(GateCheck(
                        name=f"recipe_verified_{req_id}",
                        passed=False,
                        actual=f"{strategy} after {attempts} attempt(s), {gap_count} residual gap(s)",
                        expected="recipe data produces declared expected_outputs",
                        severity="BLOCK" if priority == "P0" else "WARN",
                        detail=(
                            f"DatasetRecipe for {req_id} could not be corrected to satisfy "
                            f"its expected_outputs within {attempts} attempts. The Gherkin "
                            f"will assert on values the data cannot produce. Inspect "
                            f"{candidates[-1].name} verification_residual_gaps for details."
                        ),
                        category="recipe",
                    ))
                else:
                    checks.append(GateCheck(
                        name=f"recipe_verified_{req_id}",
                        passed=True,
                        actual=f"verified via {strategy} in {attempts} attempt(s)",
                        expected="recipe data produces declared expected_outputs",
                        severity="WARN",
                        detail=f"Closed-loop verification confirmed recipe→data→pipeline alignment",
                        category="recipe",
                    ))
        except Exception as exc:
            log.debug("recipe_verified check skipped: %s", exc)
        return checks

    def _check_call_sequence_integrity(self, coverage_report: dict) -> list[GateCheck]:
        """Phase F1 — surface chain causality: distinguish cascade failures
        (downstream skips because a provider test failed) from real defects.

        Reads from `coverage_report["sequence_integrity"]` which the
        traceability layer populates after a run. Shape:

            {
              "cascade_failures": [{test_id, root_cause_test_id, via_var}, ...],
              "provider_contract_violations": [
                  {test_id, var_name, expected_jsonpath, observed_shape, ...},
              ],
              "unresolved_chain_starts": [{chain_id, head_endpoint}, ...],
              "degraded": bool,
              "degraded_reason": str,
            }

        Three GateCheck variants:
          - cascade_failure_count → INFO (these don't count against pass-rate
            on their own; they're collapsed into a single root-cause defect
            by Phase G)
          - provider_contract_violations → BLOCK (the SUT response shape
            drifted from what an earlier discovery saw — real regression)
          - unresolved_chain_starts → WARN (the chain has a head endpoint
            that no current test exercises, so downstream consumers will
            cascade-fail until coverage catches up)

        Per DR-3: graph traversal happens upstream (traceability_agent runs
        a single `MATCH (failed)-[:DEPENDS_ON*1..5]->(prov)` query); this
        check just consumes the resulting structured report. Neo4j-down →
        `degraded: True` → all variants downgrade to WARN with
        `degraded_reason` cited.
        """
        checks: list[GateCheck] = []
        try:
            seq = coverage_report.get("sequence_integrity") or {}
            cascade = seq.get("cascade_failures") or []
            pcv = seq.get("provider_contract_violations") or []
            unresolved = seq.get("unresolved_chain_starts") or []
            degraded = bool(seq.get("degraded"))
            degraded_reason = str(seq.get("degraded_reason") or "")

            # Cascade failures — INFO with root-cause linkage. BLOCK severity
            # would double-count: these tests failed because their PROVIDER
            # failed, and the provider failure already counts.
            if cascade:
                root_causes = sorted({
                    c.get("root_cause_test_id") for c in cascade
                    if isinstance(c, dict) and c.get("root_cause_test_id")
                })
                sample = [
                    f"{c.get('test_id', '?')} ← {c.get('root_cause_test_id', '?')} (via {c.get('via_var', '?')})"
                    for c in cascade[:3] if isinstance(c, dict)
                ]
                checks.append(GateCheck(
                    name="cascade_failures",
                    passed=False,
                    actual=f"{len(cascade)} cascade failure(s) from {len(root_causes)} root-cause test(s): {sample}",
                    expected="0 cascading failures (root causes counted separately)",
                    severity="INFO",
                    detail=(
                        f"These tests SKIPPED/FAILED because a provider test "
                        f"didn't supply a required env var. Root-cause tests "
                        f"({root_causes[:5]}) carry the actual defect. "
                        f"Phase G defect-intel collapses cascades into one "
                        f"ticket per root cause."
                    ),
                    category="sequence_integrity",
                ))

            # Provider contract violations — BLOCK (SUT response shape drift)
            if pcv:
                # Phase F1: BLOCK unless Neo4j is degraded (no graph traversal
                # to verify the violations) — per DR-3, downgrade to WARN.
                severity = "WARN" if degraded else "BLOCK"
                sample = [
                    f"{p.get('test_id', '?')}.{p.get('var_name', '?')} "
                    f"(expected {p.get('expected_jsonpath', '?')})"
                    for p in pcv[:3] if isinstance(p, dict)
                ]
                checks.append(GateCheck(
                    name="provider_contract_violations",
                    passed=False,
                    actual=f"{len(pcv)} provider contract violation(s): {sample}",
                    expected="0 provider contract violations",
                    severity=severity,
                    detail=(
                        f"A provider test PASSED but did not return the "
                        f"expected jsonpath. The SUT's response shape has "
                        f"drifted from what discovery captured. Either the "
                        f"SUT regressed or discovery was stale — "
                        f"check the Discovery panel and re-harvest."
                        + (f" [Neo4j {degraded_reason} — confidence reduced]"
                           if degraded else "")
                    ),
                    category="sequence_integrity",
                ))

            # Unresolved chain starts — WARN (a head endpoint with no test)
            if unresolved:
                sample = [
                    f"{u.get('head_endpoint', '?')} (chain {u.get('chain_id', '?')[:12]})"
                    for u in unresolved[:3] if isinstance(u, dict)
                ]
                checks.append(GateCheck(
                    name="unresolved_chain_starts",
                    passed=False,
                    actual=f"{len(unresolved)} chain head(s) lack a covering test: {sample}",
                    expected="0 chains with uncovered head endpoints",
                    severity="WARN",
                    detail=(
                        "A captured chain begins with an endpoint no test "
                        "exercises. Downstream consumers will cascade-fail "
                        "until a test covers the head. Operator: add a "
                        "happy-path test for the head endpoint OR mark the "
                        "chain stale via the Discovery panel."
                    ),
                    category="sequence_integrity",
                ))

            # Degraded mode signal — even with no cascades/PCVs, surface that
            # the graph wasn't queryable so operators know coverage is lower.
            if degraded and not (cascade or pcv or unresolved):
                checks.append(GateCheck(
                    name="sequence_integrity_degraded",
                    passed=False,
                    actual=f"sequence-integrity check degraded: {degraded_reason}",
                    expected="graph available for cascade attribution",
                    severity="WARN",
                    detail=(
                        "Phase F1 graph traversal couldn't run (Neo4j "
                        "unavailable). Cascade-vs-defect classification is "
                        "currently per-test only. Restore Neo4j to recover."
                    ),
                    category="sequence_integrity",
                ))
        except Exception as exc:
            log.debug("call_sequence_integrity check skipped: %s", exc)
        return checks

    def _check_unresolved_path_params(self, coverage_report: dict) -> list[GateCheck]:
        """run-dea20e follow-up — surface Newman path-params the test data
        couldn't resolve as a CONCERNS-level finding. Without this, a run
        like run-dea20e (2,200 SKIPs across 22 unresolved vars) silently
        passes when the small subset of executable tests pass.

        Phase F2 upgrade: for each unresolved param, look up the chain's
        provider linkage from `coverage_report["sequence_integrity"]
        ["param_provenance"]` so the message names the producer test +
        whether it passed. Falls back to the flat list when provenance
        isn't available (Neo4j down or pre-Phase C runs).

        Severity: WARN (CONCERNS) — these aren't quality failures, but the
        operator needs to know that >X% of contract tests were never run."""
        checks: list[GateCheck] = []
        try:
            params = coverage_report.get("unresolved_path_params") or []
            if isinstance(params, set):
                params = sorted(params)
            if not params:
                return []

            # Phase F2 — provenance lookup. Each entry is:
            #   {var_name: {"provider_test_id", "provider_endpoint",
            #               "provider_status", "expected_jsonpath"}}
            seq = coverage_report.get("sequence_integrity") or {}
            provenance: dict = seq.get("param_provenance") or {}

            sample_lines: list[str] = []
            for p in list(params)[:6]:
                prov = provenance.get(p)
                if isinstance(prov, dict) and prov.get("provider_test_id"):
                    pstatus = prov.get("provider_status", "?")
                    pep = prov.get("provider_endpoint", "?")
                    note = (
                        f"  - {p}: expected from {prov['provider_test_id']} "
                        f"({pep}, status={pstatus})"
                    )
                else:
                    note = f"  - {p}: no observed provider in chains"
                sample_lines.append(note)

            checks.append(GateCheck(
                name="unresolved_path_params",
                passed=False,
                actual=f"{len(params)} env var(s) unresolved",
                expected="0 unresolved path-params",
                severity="WARN",
                detail=(
                    "Newman tests reference path parameters whose env-var "
                    "values aren't populated in this project's environment.\n"
                    + "\n".join(sample_lines)
                    + "\n\nPhase B UI Discovery harvests these automatically — "
                    "trigger /api/discovery/refresh OR populate manually via "
                    "Settings → Environments → Variables."
                ),
                category="environment",
            ))
        except Exception as exc:
            log.debug("unresolved_path_params check skipped: %s", exc)
        return checks

    def _check_pending_heal_rerun(self) -> list[GateCheck]:
        """Phase 5.6 — Block / warn when there are healing proposals carrying
        `requires_rerun=True`. The current run's results are stale because
        inline-heal patched test files mid-flight; the gate must not approve
        until a re-run reflects the patched state.

        WARN-severity (CONCERNS, not FAIL) — the heal MAY have been correct,
        but the proof requires a re-run. Operator can WAIVE temporarily
        through the existing waiver path."""
        checks: list[GateCheck] = []
        try:
            from ..api.routers.healing import PENDING_APPROVALS  # type: ignore
            stale = [
                p for p in PENDING_APPROVALS.values()
                if isinstance(p, dict) and p.get("requires_rerun")
                and p.get("status", "pending") == "pending"
            ]
            if stale:
                sample = [p.get("test_id", "?") for p in stale[:3]]
                checks.append(GateCheck(
                    name="pending_heal_rerun",
                    passed=False,
                    actual=f"{len(stale)} healing proposal(s) await re-run: {sample}",
                    expected="0 stale-after-heal proposals",
                    severity="WARN",
                    detail=(
                        "Inline-heal patched test files mid-run; the run's "
                        "results don't reflect the patches. Re-run the suite "
                        "before approving — or WAIVE if you've manually "
                        "verified the heal."
                    ),
                    category="self_healing",
                ))
        except Exception as exc:
            log.debug("pending_heal_rerun check skipped: %s", exc)
        return checks

    def _check_a11y(self, nfr: dict) -> list[GateCheck]:
        """F3-3: WCAG 2.1 AA violation gating from axe-core results.

        Expects nfr keys (populated by execution router after axe runs):
            a11y_violations_critical: int  (serious + critical impact)
            a11y_violations_moderate: int  (moderate impact only)
        Skipped silently when neither key is present (a11y was not measured).
        """
        checks = []
        if nfr.get("a11y_violations_critical") is not None:
            crit = int(nfr["a11y_violations_critical"])
            checks.append(GateCheck(
                name="a11y_violations_critical",
                passed=crit <= self._thresholds.max_a11y_violations_critical,
                actual=str(crit),
                expected=f"\u2264{self._thresholds.max_a11y_violations_critical}",
                severity="BLOCK",
                detail="WCAG 2.1 AA serious/critical violations block release",
                category="nfr_accessibility",
            ))
        if nfr.get("a11y_violations_moderate") is not None:
            mod = int(nfr["a11y_violations_moderate"])
            checks.append(GateCheck(
                name="a11y_violations_moderate",
                passed=mod <= self._thresholds.max_a11y_violations_moderate,
                actual=str(mod),
                expected=f"\u2264{self._thresholds.max_a11y_violations_moderate}",
                severity="WARN",
                detail="WCAG 2.1 AA moderate violations should be remediated",
                category="nfr_accessibility",
            ))
        return checks

    def _check_flakiness(self, execution_history: list[dict]) -> list[GateCheck]:
        """F3-7: Detect flaky tests across runs and emit a flakiness_score check.

        flakiness_score = 100 * (1 - flaky_count / total_distinct_tests)
        100 = perfect (no flaky tests). Score < warn → CONCERNS; < fail → FAIL.

        Algorithm mirrors DefectIntelAgent.detect_flakiness so the gate is
        consistent with what self-healing surfaces in the UI.
        """
        from collections import defaultdict
        per_test: dict[str, list[str]] = defaultdict(list)
        for r in execution_history:
            tid = r.get("test_id")
            status = r.get("status")
            if tid and status:
                per_test[tid].append(str(status).upper())

        analysed = [tid for tid, statuses in per_test.items()
                    if len(statuses) >= self._thresholds.flakiness_min_runs_per_test]
        if not analysed:
            return []  # not enough history to call flakiness — skip the check

        flaky = 0
        for tid in analysed:
            statuses = per_test[tid]
            unique = set(statuses)
            if "PASS" in unique and "FAIL" in unique:
                fail_rate = statuses.count("FAIL") / len(statuses)
                if 0.1 < fail_rate < 0.9:
                    flaky += 1

        score = 100.0 * (1.0 - flaky / len(analysed)) if analysed else 100.0

        if score < self._thresholds.flakiness_fail_score:
            severity = "BLOCK"
        elif score < self._thresholds.flakiness_warn_score:
            severity = "WARN"
        else:
            severity = "INFO"

        return [GateCheck(
            name="flakiness_score",
            passed=score >= self._thresholds.flakiness_warn_score,
            actual=f"{score:.1f} ({flaky}/{len(analysed)} flaky)",
            expected=f"\u2265{self._thresholds.flakiness_warn_score:.0f}",
            severity=severity,
            detail=(
                "Flakiness score: 100 = no flaky tests. "
                f"BLOCK below {self._thresholds.flakiness_fail_score:.0f}; "
                f"WARN below {self._thresholds.flakiness_warn_score:.0f}."
            ),
            category="reliability",
        )]

    def _check_compliance(self, strategy: dict | None) -> list[GateCheck]:
        """F7-1: Compliance attestation gate.

        Reads `compliance_markers` (list of strings) from the strategy artifact
        and confirms every entry in `required_compliance_attestations` is present.
        Missing markers BLOCK the release. Returns one check per required marker
        plus an aggregate summary check.
        """
        required = list(self._thresholds.required_compliance_attestations or [])
        if not required:
            return []
        claimed = set((strategy or {}).get("compliance_markers") or [])
        checks: list[GateCheck] = []
        missing: list[str] = []
        for marker in required:
            present = marker in claimed
            if not present:
                missing.append(marker)
            checks.append(GateCheck(
                name=f"compliance_{marker}",
                passed=present,
                actual="claimed" if present else "missing",
                expected="claimed",
                severity="BLOCK",
                detail=(
                    f"Strategy artifact must declare compliance with {marker}. "
                    "Add it to the requirement's compliance_markers list and re-run "
                    "risk scoring so the strategy artifact is regenerated."
                ),
                category="compliance",
            ))
        # Single roll-up so dashboards show one row even when many markers are required.
        checks.append(GateCheck(
            name="compliance_attestations_present",
            passed=not missing,
            actual=f"{len(required) - len(missing)}/{len(required)}",
            expected=f"{len(required)}/{len(required)}",
            severity="BLOCK",
            detail="All required compliance attestations must be claimed in the strategy artifact",
            category="compliance",
        ))
        return checks

    def _check_reproducibility(self, execution_history: list[dict]) -> list[GateCheck]:
        """F7-1: Reproducibility gate.

        For each test that has run at least `repro_window` times, mark it
        reproducible iff the last `repro_window` outcomes are all identical.
        repro_score = 100 * reproducible_tests / total_tests_with_window.

        Distinct from flakiness: a test that fails CONSISTENTLY is reproducible
        (known-bad), but a test that passes AND fails is both flaky AND
        non-reproducible. Reproducibility surfaces the broader category of
        "outcome cannot be predicted from inputs" — including environment
        sensitivity that flakiness alone won't catch.
        """
        from collections import defaultdict
        per_test: dict[str, list[str]] = defaultdict(list)
        for r in execution_history:
            tid = r.get("test_id")
            status = r.get("status")
            if tid and status:
                per_test[tid].append(str(status).upper())

        window = max(2, self._thresholds.repro_window)
        windowed = {tid: statuses[-window:]
                    for tid, statuses in per_test.items()
                    if len(statuses) >= window}
        if not windowed:
            return []  # not enough history to gate on reproducibility — skip

        reproducible = sum(1 for statuses in windowed.values()
                           if len(set(statuses)) == 1)
        score = 100.0 * reproducible / len(windowed)

        if score < self._thresholds.repro_fail_score:
            severity = "BLOCK"
        elif score < self._thresholds.repro_warn_score:
            severity = "WARN"
        else:
            severity = "INFO"

        return [GateCheck(
            name="reproducibility_score",
            passed=score >= self._thresholds.repro_warn_score,
            actual=f"{score:.1f} ({reproducible}/{len(windowed)} reproducible over last {window} runs)",
            expected=f"\u2265{self._thresholds.repro_warn_score:.0f}",
            severity=severity,
            detail=(
                "Reproducibility = identical outcome over last N runs. "
                f"BLOCK below {self._thresholds.repro_fail_score:.0f}, "
                f"WARN below {self._thresholds.repro_warn_score:.0f}, "
                f"window={window}. Distinct from flakiness — catches consistent "
                "but environment-sensitive failures too."
            ),
            category="reproducibility",
        )]

    # ── Waiver Logic ────────────────────────────────────────────────────────

    def _find_active_waiver(
        self, waivers: list[dict], blocking: list[GateCheck]
    ) -> GateWaiver | None:
        """LEGACY (pre-R134.B.1): returned the FIRST waiver matching ANY blocker.

        DEPRECATED in favor of `_r134_b_1_match_waivers_per_blocker` —
        kept for backward compat (existing tests rely on this surface).
        Returns the first matching active waiver. The caller should now
        invoke the per-blocker matcher for the actual gate decision.
        """
        if not blocking or not waivers:
            return None

        now = datetime.now(timezone.utc)
        blocking_names = {c.name for c in blocking}

        for w in waivers:
            expires = w.get("expires_at")
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires)
            if expires and expires > now and w.get("waived_check") in blocking_names:
                return GateWaiver(
                    waived_check=w["waived_check"],
                    rationale=w.get("rationale", ""),
                    approved_by=w.get("approved_by", ""),
                    expires_at=expires,
                )
        return None

    # ── R134.B.1 KEYSTONE — per-blocker waiver matching ─────────────────────
    @staticmethod
    def _r134_b_1_is_expired(waiver: dict) -> bool:
        """Returns True when the waiver's `expires_at` has passed.
        Treats missing/unparseable expiry as 'expired' for safety —
        a waiver without a deadline is not a valid suppress signal."""
        expires = waiver.get("expires_at")
        if not expires:
            return True
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires)
            except (ValueError, TypeError):
                return True
        try:
            return expires <= datetime.now(timezone.utc)
        except TypeError:
            # Tz-naive datetime — coerce
            return expires.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)

    def _r134_b_1_match_waivers_per_blocker(
        self, waivers: list[dict], blocking_checks: list[GateCheck],
    ) -> tuple[list[GateCheck], list[GateCheck]]:
        """R134.B.1 KEYSTONE — match waivers PER blocker.

        Pre-R134.B.1: a single active waiver covering ANY blocking check
        was enough to set the entire release to WAIVED — silently
        suppressing OTHER unwaived blockers. Pillar 4 truthfulness
        violation: operator sees green release when 2-3 blockers are
        actually active.

        Post-R134.B.1: returns (waived_checks, still_blocking_checks).
        A check is waived only when an active waiver explicitly names it
        (`waiver.waived_check == check.name`). Unwaived blockers keep
        blocking; decision = WAIVED only when ALL blockers are
        individually waived.
        """
        waived: list[GateCheck] = []
        still_blocking: list[GateCheck] = []
        for check in blocking_checks:
            matching = next(
                (
                    w for w in (waivers or [])
                    if w.get("waived_check") == check.name
                    and not self._r134_b_1_is_expired(w)
                ),
                None,
            )
            if matching:
                waived.append(check)
            else:
                still_blocking.append(check)
        return waived, still_blocking

    # ── Helpers ────────────────────────────────────────────────────────────

    def _build_summary(
        self,
        decision: str,
        blocking: list[GateCheck],
        warnings: list[GateCheck],
        all_checks: list[GateCheck],
    ) -> str:
        passed = sum(1 for c in all_checks if c.passed)
        total = len(all_checks)

        if decision == "PASS":
            return (
                f"RELEASE APPROVED \u2014 {passed}/{total} checks passed"
                + (f", {len(warnings)} advisory warnings" if warnings else "")
            )
        if decision == "CONCERNS":
            concern_names = ", ".join(c.name for c in warnings[:3])
            return (
                f"RELEASE CONCERNS \u2014 {passed}/{total} checks passed, "
                f"{len(warnings)} concern(s) require mitigation: {concern_names}"
            )
        if decision == "WAIVED":
            return (
                f"RELEASE WAIVED \u2014 {len(blocking)} blocking check(s) waived by authorized exception"
            )
        # FAIL
        reasons = "; ".join(f"{c.name} ({c.actual} vs {c.expected})" for c in blocking[:5])
        return f"RELEASE FAILED \u2014 {len(blocking)} critical check(s) failed: {reasons}"
