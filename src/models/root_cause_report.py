"""RootCauseReport — the structured 'fail with a detailed explanation' artifact.

The platform directive (Fail-Fast, Explain-Clearly) requires that when ARTA
cannot proceed correctly it does NOT switch to a silent alternate path — it
fails loudly with a structured explanation. Every such failure carries:
  - a 5-level deep-dive: symptom → immediate_cause → upstream_cause →
    architectural_cause → process_cause
  - {root_cause, impact, severity, recommended_fix, preventive_action}

This is the canonical carrier emitted by the RetryLadder's final rung and by
each fail-fast gen stage (recipe / ATDD / risk / upstream-gate). It reuses the
shape already established by defect_intel's report dicts so the dashboard /
mission report can render both uniformly. Persisted per-project to
`.arta/root_cause/<pid>/<failure_id>.json` and aggregated by `read_root_causes`
(mirrors upstream_quality / traceability_gate sidecar patterns).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

_RC_DIR = Path(".arta/root_cause")

# 5-level deep-dive keys (directive's continuous deep-dive).
DEEP_DIVE_LEVELS = (
    "symptom", "immediate_cause", "upstream_cause",
    "architectural_cause", "process_cause",
)
SEVERITIES = ("critical", "high", "medium", "low")


@dataclass
class DeepDive:
    """5-level root-cause descent. Each level is a one-line explanation."""
    symptom: str = ""               # what the operator/runtime observes
    immediate_cause: str = ""       # the direct failure point
    upstream_cause: str = ""        # why that component produced it
    architectural_cause: str = ""   # the design/contract that allowed it
    process_cause: str = ""         # how it escaped earlier validation


@dataclass
class ImpactRadius:
    """Scope of the failure's propagation."""
    affected_test_ids: list = field(default_factory=list)
    affected_features: list = field(default_factory=list)
    affected_files: list = field(default_factory=list)
    affected_layers: list = field(default_factory=list)   # api | ui | auth | data | ...
    cascade_depth: int = 0


@dataclass
class RootCauseReport:
    """Structured 'fail with detailed explanation' record.

    `stage` is the pipeline stage that failed (e.g. dataset_recipe, atdd,
    risk_scoring, upstream_gherkin_gate, playwright). `ladder_trace` records
    which retry rungs were attempted before the failure (observability — the
    directive forbids silent failure)."""
    failure_id: str
    stage: str
    title: str = ""
    root_cause: str = ""
    failure_type: str = "TEST_GEN"          # TEST_GEN | ENV | SUT | CONTRACT | SECURITY
    severity: str = "high"                   # critical | high | medium | low
    confidence: float = 0.6
    deep_dive: DeepDive = field(default_factory=DeepDive)
    impact: ImpactRadius = field(default_factory=ImpactRadius)
    recommended_fix: str = ""
    fix_effort: str = "operator_review"      # minutes | hours | operator_review
    preventive_action: str = ""
    ladder_trace: list = field(default_factory=list)   # ["context","evidence",...]
    violations: list = field(default_factory=list)
    defect_class: str = "gen_quality_fail_fast"
    defect_subclass: str | None = None
    project_id: str | None = None
    requirement_id: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.severity not in SEVERITIES:
            d["severity"] = "high"
        return d

    def one_line(self) -> str:
        """Operator-facing one-liner for logs / BLOCKED-row error_message."""
        return (f"[{self.stage}] {self.title or self.root_cause} "
                f"(severity={self.severity}; tried={'+'.join(self.ladder_trace) or 'none'}; "
                f"fix: {self.recommended_fix[:120]})")


def build_report(
    *, failure_id: str, stage: str, root_cause: str,
    severity: str = "high", failure_type: str = "TEST_GEN",
    deep_dive: dict | DeepDive | None = None,
    recommended_fix: str = "", preventive_action: str = "",
    ladder_trace: list | None = None, project_id: str | None = None,
    requirement_id: str | None = None, **extra,
) -> RootCauseReport:
    """Convenience constructor — accepts a deep_dive dict or DeepDive."""
    dd = deep_dive if isinstance(deep_dive, DeepDive) else DeepDive(
        **{k: v for k, v in (deep_dive or {}).items() if k in DEEP_DIVE_LEVELS})
    return RootCauseReport(
        failure_id=failure_id, stage=stage, root_cause=root_cause,
        title=extra.get("title", root_cause[:80]),
        severity=severity, failure_type=failure_type, deep_dive=dd,
        recommended_fix=recommended_fix, preventive_action=preventive_action,
        ladder_trace=ladder_trace or [], project_id=project_id,
        requirement_id=requirement_id,
        confidence=extra.get("confidence", 0.6),
        fix_effort=extra.get("fix_effort", "operator_review"),
        impact=extra.get("impact") or ImpactRadius(
            affected_test_ids=[requirement_id] if requirement_id else []),
        violations=extra.get("violations", []),
        defect_subclass=extra.get("defect_subclass"),
    )


# ── persistence + aggregation ────────────────────────────────────────────────

def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(s))[:120]


def persist_root_cause(report: RootCauseReport) -> None:
    """Best-effort per-project sidecar write."""
    try:
        pid = report.project_id or "_unknown"
        d = _RC_DIR / _safe(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{_safe(report.failure_id)}.json").write_text(
            json.dumps(report.to_dict(), indent=2, default=str))
    except Exception:
        pass


def read_root_causes(project_id: str | None = None) -> dict:
    """Aggregate per-project RootCauseReports → counts by stage/severity + rows."""
    rows: list[dict] = []
    base = _RC_DIR / _safe(project_id) if project_id else _RC_DIR
    paths = []
    if project_id and base.is_dir():
        paths = sorted(base.glob("*.json"))
    elif not project_id and _RC_DIR.is_dir():
        paths = sorted(_RC_DIR.glob("*/*.json"))
    for p in paths:
        try:
            rows.append(json.loads(p.read_text()))
        except Exception:
            continue
    by_stage: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for r in rows:
        by_stage[r.get("stage", "?")] = by_stage.get(r.get("stage", "?"), 0) + 1
        sv = r.get("severity", "high")
        by_severity[sv] = by_severity.get(sv, 0) + 1
    return {
        "project_id": project_id,
        "count": len(rows),
        "by_stage": by_stage,
        "by_severity": by_severity,
        "reports": [
            {"failure_id": r.get("failure_id"), "stage": r.get("stage"),
             "requirement_id": r.get("requirement_id"), "severity": r.get("severity"),
             "root_cause": r.get("root_cause"), "recommended_fix": r.get("recommended_fix"),
             # FE-sync P1 — the projection dropped preventive_action even though the
             # RootCauseReport carries it; surface it for the dashboard.
             "preventive_action": r.get("preventive_action"),
             "ladder_trace": r.get("ladder_trace"), "deep_dive": r.get("deep_dive")}
            for r in rows[:100]
        ],
    }
