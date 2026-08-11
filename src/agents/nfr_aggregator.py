"""Phase 3.4 — Layer 8 (Non-Functional Validation) results aggregator.

The audit found that k6/axe/ZAP/pytest results were appended to `_REAL_RESULTS`
as flat per-test entries, but no aggregator produced the structured
`run.nfr = {a11y_violations_critical: ..., security_critical_findings: ...,
perf_p95_ms: ...}` summary the Quality Gate expects to read. So gate's NFR
checks all silently skipped because the dict they queried was empty.

This module walks per-tool result dicts and emits the aggregated `run.nfr`
shape the gate can directly consume — zero LLM calls, deterministic, fast.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("arta.nfr")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _aggregate_axe(axe_results: list[dict]) -> dict[str, Any]:
    """Per-axe-violation severity counters.

    axe results land as one execution_result per Playwright spec that ran the
    axe-core scan; violations are typically nested under `metadata.violations`
    or surfaced as test status (FAIL when violations found). The shape can
    vary by generator version; we tolerate both.
    """
    critical = serious = moderate = minor = 0
    affected_specs = 0
    for r in axe_results:
        if not isinstance(r, dict):
            continue
        affected_specs += 1
        violations = (
            (r.get("metadata") or {}).get("violations")
            or r.get("violations")
            or []
        )
        if not isinstance(violations, list):
            continue
        for v in violations:
            if not isinstance(v, dict):
                continue
            impact = (v.get("impact") or "").lower()
            if impact == "critical":
                critical += 1
            elif impact == "serious":
                serious += 1
            elif impact == "moderate":
                moderate += 1
            elif impact == "minor":
                minor += 1
    return {
        "specs_scanned": affected_specs,
        "violations_critical": critical,
        "violations_serious": serious,
        "violations_moderate": moderate,
        "violations_minor": minor,
        "total_violations": critical + serious + moderate + minor,
    }


def _aggregate_zap(zap_results: list[dict]) -> dict[str, Any]:
    """ZAP findings counters by risk level.

    ZAP results typically have `risk` ∈ {High, Medium, Low, Informational}
    on each `metadata.alerts` entry; or may be flattened with the risk on
    each execution_result row when the generator emitted one row per alert.
    """
    high = medium = low = info = 0
    for r in zap_results:
        if not isinstance(r, dict):
            continue
        alerts = (r.get("metadata") or {}).get("alerts") or r.get("alerts") or []
        # Some generators put the risk on the row itself when there's one
        # alert per row — handle that as a single virtual alert.
        if not alerts and r.get("risk"):
            alerts = [{"risk": r.get("risk")}]
        if not isinstance(alerts, list):
            continue
        for a in alerts:
            if not isinstance(a, dict):
                continue
            risk = (a.get("risk") or "").lower()
            if risk == "high":
                high += 1
            elif risk == "medium":
                medium += 1
            elif risk == "low":
                low += 1
            elif risk in ("informational", "info"):
                info += 1
    return {
        "specs_scanned": len(zap_results),
        "findings_high": high,
        "findings_medium": medium,
        "findings_low": low,
        "findings_informational": info,
        # Gate-facing alias matches what _check_security_findings reads.
        "security_critical_findings": high,
    }


_K6_METRIC_RE = re.compile(
    r"(?:p\(95\)|p95)[\s=:]+(\d+(?:\.\d+)?)\s*(ms|s)?",
    re.IGNORECASE,
)


def _aggregate_k6(k6_results: list[dict]) -> dict[str, Any]:
    """k6 perf summary. Pulls p95 + error_rate from `metadata.summary` when
    available (k6 emits a structured JSON summary); falls back to regex-
    parsing per-test stdout for the common `p(95)=X ms` format.
    """
    p95_values_ms: list[float] = []
    error_rates: list[float] = []
    threshold_violations: list[dict[str, Any]] = []
    iterations_total = 0

    for r in k6_results:
        if not isinstance(r, dict):
            continue
        meta = r.get("metadata") or {}
        summary = meta.get("summary") or meta.get("k6_summary") or {}
        # Structured path (preferred when generator emitted summary.json)
        if isinstance(summary, dict):
            metrics = summary.get("metrics") or {}
            iter_count = metrics.get("iterations", {}).get("count")
            if isinstance(iter_count, (int, float)):
                iterations_total += int(iter_count)
            http_dur = metrics.get("http_req_duration") or metrics.get("http_req_duration{expected_response:true}") or {}
            if isinstance(http_dur, dict):
                p95 = http_dur.get("p(95)") or http_dur.get("p95")
                if p95 is not None:
                    p95_values_ms.append(_safe_float(p95))
            err = metrics.get("http_req_failed") or {}
            if isinstance(err, dict) and err.get("rate") is not None:
                error_rates.append(_safe_float(err.get("rate")))
            # Threshold violations the generator captured
            for tname, tres in (summary.get("thresholds") or {}).items():
                if isinstance(tres, dict) and tres.get("ok") is False:
                    threshold_violations.append({"threshold": tname, "violated": True})
        # Regex fallback against stdout
        stdout = r.get("stdout") or meta.get("stdout") or ""
        if isinstance(stdout, str):
            m = _K6_METRIC_RE.search(stdout)
            if m:
                v = _safe_float(m.group(1))
                unit = (m.group(2) or "ms").lower()
                if unit == "s":
                    v *= 1000
                p95_values_ms.append(v)
        # Tests with status FAIL where tool=k6 imply a threshold breach
        if r.get("status") == "FAIL" and (r.get("tool") == "k6" or r.get("automation_tool") == "k6"):
            threshold_violations.append({
                "test_id": r.get("test_id"),
                "violated": True,
                "title": r.get("title", ""),
            })

    # Aggregate p95 across all k6 specs — max is the most conservative answer
    # (worst SLA breach in the batch) since each spec is its own scenario.
    p95_max = max(p95_values_ms) if p95_values_ms else None
    p95_mean = round(sum(p95_values_ms) / len(p95_values_ms), 2) if p95_values_ms else None
    error_rate_mean = round(sum(error_rates) / len(error_rates), 4) if error_rates else None

    return {
        "specs_run": len(k6_results),
        "iterations_total": iterations_total,
        "perf_p95_max_ms": p95_max,
        "perf_p95_mean_ms": p95_mean,
        "error_rate_mean": error_rate_mean,
        "threshold_violations": threshold_violations,
        "threshold_violation_count": len(threshold_violations),
    }


def _filter_by_tool(results: list[dict], *names: str) -> list[dict]:
    """Filter execution results by automation_tool / tool. Tolerates either
    field since the codebase has both."""
    out: list[dict] = []
    target = {n.lower() for n in names}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        t = (r.get("automation_tool") or r.get("tool") or "").lower()
        if t in target:
            out.append(r)
    return out


# Phase 3.5 — Compare k6 NFR results to requirement-derived measurables.
# Layer 1.2 stamps `acceptance_criteria.measurable = {value, unit, comparator}`
# on every AC that carries a quantitative threshold. The k6 generator picks
# up its threshold from the requirement, but there's no verification that the
# script's threshold actually MATCHES what the requirement promised. A test
# can pass `p95<5000ms` while the requirement said `p95<500ms` — the gate
# would then approve a 10x SLA breach.
#
# This comparator surfaces those mismatches as findings the gate consumes.

# Convert any threshold to a canonical millisecond/dimensionless quantity so
# we can compare apples-to-apples. Recipe-level units only need to handle
# common time + percent forms; richer units (rps, MB/s) come later.
_UNIT_MS_MULTIPLIER: dict[str, float] = {
    "ms": 1.0, "s": 1000.0, "m": 60_000.0, "h": 3_600_000.0,
}


def _measurable_to_ms(measurable: dict[str, Any]) -> float | None:
    """Returns the threshold in milliseconds when the unit is time-shaped,
    or None when the comparator/unit isn't perf-related."""
    if not isinstance(measurable, dict):
        return None
    value = measurable.get("value")
    unit = (measurable.get("unit") or "").lower()
    if not isinstance(value, (int, float)) or unit not in _UNIT_MS_MULTIPLIER:
        return None
    return float(value) * _UNIT_MS_MULTIPLIER[unit]


def compare_perf_vs_requirement(
    nfr: dict[str, Any],
    requirement: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Find perf SLA mismatches between the requirement and the actual run.

    Returns a list of finding dicts the Quality Gate can append directly to
    its `findings` array. Each finding has level/category/message/evidence so
    the gate can decide whether to PASS / CONCERNS / FAIL the perf check.

    Examples returned:
        [{"level":"error", "category":"perf",
          "message":"Requirement promised p95 < 500ms; observed p95 = 1240ms",
          "evidence":{"requirement_value":500,"observed":1240,"comparator":"<="}}]
    """
    if not isinstance(requirement, dict):
        return []
    findings: list[dict[str, Any]] = []
    observed_p95 = nfr.get("perf_p95_max_ms")
    if observed_p95 is None:
        return []  # k6 didn't run or returned nothing — separate gate concern

    # Walk every AC's measurable triple; filter to those that look perf-shaped
    # (a time-unit comparator, e.g. `<= 500 ms`). Other measurables (categorical
    # equality, percentages without a "%" floor) aren't perf SLAs.
    acs = requirement.get("acceptance_criteria") or []
    for ac in acs:
        if not isinstance(ac, dict):
            continue
        m = ac.get("measurable")
        threshold_ms = _measurable_to_ms(m)
        if threshold_ms is None:
            continue
        comparator = (m.get("comparator") or "<=").strip()
        violated = False
        if comparator in {"<=", "<"}:
            violated = observed_p95 > threshold_ms
        elif comparator in {">=", ">"}:
            violated = observed_p95 < threshold_ms
        elif comparator in {"==", "~"}:
            # Tolerance-style — accept ±20% as a sensible default
            violated = abs(observed_p95 - threshold_ms) / max(threshold_ms, 1) > 0.2
        if violated:
            findings.append({
                "level": "error",
                "category": "perf",
                "ac_id": ac.get("id"),
                "message": (
                    f"AC {ac.get('id')} promised p95 {comparator} {threshold_ms:.0f}ms "
                    f"({m.get('value')}{m.get('unit') or ''}); observed p95 "
                    f"{observed_p95:.0f}ms — SLA breach"
                ),
                "evidence": {
                    "requirement_value": m.get("value"),
                    "requirement_unit": m.get("unit"),
                    "observed_p95_ms": observed_p95,
                    "comparator": comparator,
                },
            })
    if findings:
        log.info(
            "perf-vs-requirement: %d SLA violation(s) flagged for %s",
            len(findings), requirement.get("id", "?"),
        )
    return findings


def aggregate_nfr(execution_results: list[dict]) -> dict[str, Any]:
    """Walk all execution results and produce the structured `run.nfr`
    summary the Quality Gate consumes.

    Output shape (only keys the gate references):
        {
            "perf": {...},            # k6
            "security": {...},        # zap
            "a11y": {...},            # axe
            # Flattened gate-facing aliases:
            "perf_p95_max_ms": float|None,
            "a11y_violations_critical": int,
            "security_critical_findings": int,
        }

    All numbers are deterministic functions of the inputs — no LLM, no
    network, no clock-dependent value. Safe to call multiple times.
    """
    perf = _aggregate_k6(_filter_by_tool(execution_results, "k6"))
    security = _aggregate_zap(_filter_by_tool(execution_results, "zap"))
    a11y = _aggregate_axe(_filter_by_tool(execution_results, "axe"))

    # Flat gate-facing aliases match the keys quality_gate_agent reads.
    nfr: dict[str, Any] = {
        "perf": perf,
        "security": security,
        "a11y": a11y,
        "perf_p95_max_ms": perf.get("perf_p95_max_ms"),
        "a11y_violations_critical": a11y.get("violations_critical", 0),
        "a11y_violations_serious": a11y.get("violations_serious", 0),
        "security_critical_findings": security.get("security_critical_findings", 0),
    }
    log.info(
        "NFR aggregated: perf p95=%s, a11y_critical=%d, security_high=%d, "
        "k6_violations=%d",
        nfr.get("perf_p95_max_ms"),
        nfr["a11y_violations_critical"],
        nfr["security_critical_findings"],
        perf.get("threshold_violation_count", 0),
    )
    return nfr
