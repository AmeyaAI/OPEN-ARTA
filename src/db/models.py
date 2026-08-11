"""
ARTA Platform — SQLAlchemy 2.0 ORM Models
Maps to schema.sql: requirements, acceptance_criteria, test_cases, test_runs,
execution_results, defects, quality_gates, projects, users, project_roles,
refresh_tokens, test_data_fixtures, test_case_versions.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


# ── Enums ────────────────────────────────────────────────────────────────────


class RiskPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ReqStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    in_progress = "in_progress"
    done = "done"
    deprecated = "deprecated"


class TestType(str, enum.Enum):
    unit = "unit"
    integration = "integration"
    ui = "ui"
    api = "api"
    performance = "performance"
    security = "security"
    accessibility = "accessibility"


class AutomationTool(str, enum.Enum):
    playwright = "playwright"
    selenium = "selenium"
    cypress = "cypress"
    appium = "appium"
    newman = "newman"
    k6 = "k6"
    zap = "zap"
    manual = "manual"
    # F20-15: `axe` was added to the PG enum in F20-13 (migration 003) so
    # INSERTs with `CAST('axe' AS automation_tool)` could succeed. But the
    # ORM read path (list_tests → _to_dict → enum coercion) uses THIS
    # Python class to convert the raw DB string back to an enum member,
    # so it raised `LookupError: 'axe' is not among the defined enum
    # values` → HTTP 500 → Test Explorer silently rendered "No tests
    # found". Keeping both layers in sync is the fix.
    axe = "axe"
    # F20-18: `pytest` covers the analytics-layer + adversarial test family
    # written by AnalyticsTestAgent (5 layer tests + 7 adversarial scenarios
    # per analytics-keyword-matched req). Without this entry, every analytics
    # result row failed to persist in run-c5ed67 with `invalid input value
    # for enum automation_tool: "pytest"` and (pre-F20-20) one bad row
    # aborted the whole persist transaction.
    pytest = "pytest"
    # R9: `preflight` covers the SUT-reachability probe row
    # (src/api/routers/execution.py "Pre-flight: SUT unreachable").
    # Migration 005 added it to PG; this ORM entry mirrors that so the
    # READ path (GET /api/execution/runs/{run_id} → _to_dict enum coerce)
    # doesn't crash with `LookupError: 'preflight' is not among the
    # defined enum values`. F20-15 pattern: both layers must agree.
    preflight = "preflight"


class ExecutionStatus(str, enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    # R33.1 — config-gap state (R29.3a pre-dispatch filter for items
    # whose required env vars are unresolvable). Distinct from SKIP
    # (suite-selection-driven) and FAIL (real test failure). Excluded
    # from effective pass-rate denominator (R29.3d + R33.6).
    BLOCKED = "BLOCKED"
    FLAKY = "FLAKY"
    PENDING = "PENDING"
    RUNNING = "RUNNING"


class RunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class GateDecisionEnum(str, enum.Enum):
    PASS = "PASS"
    CONCERNS = "CONCERNS"
    FAIL = "FAIL"
    WAIVED = "WAIVED"


class CoverageLevel(str, enum.Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class DefectSeverity(str, enum.Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class DefectStatusEnum(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    wont_fix = "wont_fix"
    duplicate = "duplicate"


class SourceType(str, enum.Enum):
    jira = "jira"
    github_issue = "github_issue"
    confluence = "confluence"
    openapi = "openapi"
    plain_text = "plain_text"
    db_schema = "db_schema"


# ── Base ─────────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# Use native PG enums matching the schema.sql CREATE TYPE names
_risk_priority = Enum(RiskPriority, name="risk_priority", create_type=False)
_req_status = Enum(ReqStatus, name="req_status", create_type=False)
_test_type = Enum(TestType, name="test_type", create_type=False)
_automation_tool = Enum(AutomationTool, name="automation_tool", create_type=False)
_execution_status = Enum(ExecutionStatus, name="execution_status", create_type=False)
_run_status = Enum(RunStatus, name="run_status", create_type=False)
_gate_decision = Enum(GateDecisionEnum, name="gate_decision", create_type=False)
_defect_severity = Enum(DefectSeverity, name="defect_severity", create_type=False)
_defect_status = Enum(DefectStatusEnum, name="defect_status", create_type=False)
_source_type = Enum(SourceType, name="source_type", create_type=False)
_coverage_level = Enum(CoverageLevel, name="coverage_level", create_type=False)


# ── Tables ───────────────────────────────────────────────────────────────────


class Requirement(Base):
    __tablename__ = "requirements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    req_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_type: Mapped[SourceType] = mapped_column(_source_type, nullable=False, default=SourceType.plain_text)
    source_url: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[RiskPriority] = mapped_column(_risk_priority, nullable=False, default=RiskPriority.P2)
    risk_score: Mapped[int | None] = mapped_column(Integer)  # BMAD TEA: 1-9
    impact: Mapped[int | None] = mapped_column(Integer)      # 1=Minor, 2=Degraded, 3=Critical
    probability: Mapped[int | None] = mapped_column(Integer)  # 1=Unlikely, 2=Possible, 3=Likely
    status: Mapped[ReqStatus] = mapped_column(_req_status, nullable=False, default=ReqStatus.draft)
    sprint_id: Mapped[str | None] = mapped_column(Text)
    epic_id: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    content_hash: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    acceptance_criteria: Mapped[list[AcceptanceCriterion]] = relationship(back_populates="requirement", cascade="all, delete-orphan")
    test_cases: Mapped[list[TestCase]] = relationship(back_populates="requirement")
    defects: Mapped[list[Defect]] = relationship(back_populates="requirement")
    project: Mapped[Project | None] = relationship(back_populates="requirements")


class AcceptanceCriterion(Base):
    __tablename__ = "acceptance_criteria"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    ac_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    given: Mapped[str | None] = mapped_column(Text)
    when_: Mapped[str | None] = mapped_column("when_", Text)
    then_: Mapped[str | None] = mapped_column("then_", Text)
    priority: Mapped[RiskPriority] = mapped_column(_risk_priority, nullable=False, default=RiskPriority.P2)
    is_covered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    coverage_level: Mapped[CoverageLevel] = mapped_column(_coverage_level, default=CoverageLevel.NONE)
    coverage_pct: Mapped[float | None] = mapped_column(Numeric(5, 2), default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    # Phase 1.2: structured measurable threshold (see migration 008). Triple of
    # {value, unit, comparator} so downstream agents cite the exact numeric
    # promise instead of re-inferring from prose. NULL when the AC is qualitative.
    measurable: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    requirement: Mapped[Requirement] = relationship(back_populates="acceptance_criteria")
    test_cases: Mapped[list[TestCase]] = relationship(back_populates="acceptance_criterion")

    __table_args__ = (
        Index("idx_ac_requirement", "requirement_id"),
        Index("idx_ac_priority", "priority"),
        Index("idx_ac_covered", "is_covered"),
    )


class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    test_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("requirements.id", ondelete="SET NULL"))
    ac_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("acceptance_criteria.id", ondelete="SET NULL"))
    test_type: Mapped[TestType] = mapped_column(_test_type, nullable=False, default=TestType.integration)
    automation_tool: Mapped[AutomationTool] = mapped_column(_automation_tool, nullable=False, default=AutomationTool.playwright)
    priority: Mapped[RiskPriority] = mapped_column(_risk_priority, nullable=False, default=RiskPriority.P2)
    gherkin_scenario: Mapped[str | None] = mapped_column(Text)
    script_path: Mapped[str | None] = mapped_column(Text)
    script_content: Mapped[str | None] = mapped_column(Text)
    is_automated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_flaky: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flaky_rate: Mapped[float | None] = mapped_column(Numeric(5, 2), default=0)
    last_status: Mapped[ExecutionStatus | None] = mapped_column(_execution_status, default=ExecutionStatus.PENDING)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    avg_duration_ms: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Gap-3 — Traceability + analytics columns (added by migration 002_traceability.sql).
    # Mapped here so ORM reads/writes round-trip these fields correctly.
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model_version: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    red_phase_status: Mapped[str | None] = mapped_column(Text, default="PENDING_VERIFICATION")
    judge_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    dataset_version: Mapped[str | None] = mapped_column(Text)
    analytics_layer: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[int | None] = mapped_column(Integer)
    fixture: Mapped[dict | None] = mapped_column(JSONB)
    eval_rubric: Mapped[dict | None] = mapped_column(JSONB)
    adversarial_input: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    generation_source: Mapped[str | None] = mapped_column(Text)

    requirement: Mapped[Requirement | None] = relationship(back_populates="test_cases")
    acceptance_criterion: Mapped[AcceptanceCriterion | None] = relationship(back_populates="test_cases")
    execution_results: Mapped[list[ExecutionResult]] = relationship(back_populates="test_case")
    defects: Mapped[list[Defect]] = relationship(back_populates="test_case")
    project: Mapped[Project | None] = relationship(back_populates="test_cases")

    __table_args__ = (
        Index("idx_tc_requirement", "requirement_id"),
        Index("idx_tc_priority", "priority"),
        Index("idx_tc_status", "last_status"),
        Index("idx_tc_tool", "automation_tool"),
        # Gap-3: mirrors indexes declared in migration 002_traceability.sql
        Index("idx_tc_trace_id_orm", "trace_id"),
        Index("idx_tc_red_phase_orm", "red_phase_status"),
    )


class TestRun(Base):
    __tablename__ = "test_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    run_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    build_id: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False, default="staging")
    branch: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(_run_status, nullable=False, default=RunStatus.queued)
    gate_decision: Mapped[GateDecisionEnum | None] = mapped_column(_gate_decision)
    gate_summary: Mapped[str | None] = mapped_column(Text)
    total_tests: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    flaky: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float | None] = mapped_column(Numeric(5, 2))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    execution_results: Mapped[list[ExecutionResult]] = relationship(back_populates="test_run", cascade="all, delete-orphan")
    quality_gates: Mapped[list[QualityGate]] = relationship(back_populates="test_run", cascade="all, delete-orphan")
    defects: Mapped[list[Defect]] = relationship(back_populates="test_run")
    project: Mapped[Project | None] = relationship(back_populates="test_runs")

    __table_args__ = (
        Index("idx_run_build", "build_id"),
        Index("idx_run_status", "status"),
        Index("idx_run_env", "environment"),
    )


class ExecutionResult(Base):
    __tablename__ = "execution_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False)
    test_case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("test_cases.id", ondelete="SET NULL"))
    test_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ExecutionStatus] = mapped_column(_execution_status, nullable=False)
    automation_tool: Mapped[AutomationTool | None] = mapped_column(_automation_tool)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    stack_trace: Mapped[str | None] = mapped_column(Text)
    screenshot_url: Mapped[str | None] = mapped_column(Text)
    video_url: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    is_flaky: Mapped[bool] = mapped_column(Boolean, default=False)
    browser: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    test_run: Mapped[TestRun] = relationship(back_populates="execution_results")
    test_case: Mapped[TestCase | None] = relationship(back_populates="execution_results")

    __table_args__ = (
        Index("idx_er_run_id", "run_id"),
        Index("idx_er_test_id", "test_id"),
        Index("idx_er_status", "status"),
    )


class Defect(Base):
    __tablename__ = "defects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    # R28.3 — defect_id is NO LONGER globally unique. The composite
    # `(defect_id, run_id)` pair is unique (see __table_args__ below).
    # This enables per-run defect rows: the same DEF-HTTP_500 cluster
    # signature can appear in multiple runs as separate rows, so
    # operators can navigate back to a prior run and see ITS defects.
    defect_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[DefectSeverity] = mapped_column(_defect_severity, nullable=False, default=DefectSeverity.medium)
    priority: Mapped[RiskPriority] = mapped_column(_risk_priority, nullable=False, default=RiskPriority.P2)
    status: Mapped[DefectStatusEnum] = mapped_column(_defect_status, nullable=False, default=DefectStatusEnum.open)
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("requirements.id", ondelete="SET NULL"))
    test_case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("test_cases.id", ondelete="SET NULL"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="SET NULL"))
    root_cause: Mapped[str | None] = mapped_column(Text)
    root_cause_category: Mapped[str | None] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    impacted_files: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    jira_key: Mapped[str | None] = mapped_column(Text)
    jira_url: Mapped[str | None] = mapped_column(Text)
    assignee: Mapped[str | None] = mapped_column(Text)
    reporter: Mapped[str] = mapped_column(Text, default="ARTA")
    is_flaky: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    # R30.8 — dedicated triage columns, populated alongside metadata
    # JSONB for backward compatibility. Indexed via 011_defect_triage_columns.
    triage_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    triage_signals: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    requirement: Mapped[Requirement | None] = relationship(back_populates="defects")
    test_case: Mapped[TestCase | None] = relationship(back_populates="defects")
    test_run: Mapped[TestRun | None] = relationship(back_populates="defects")
    project: Mapped[Project | None] = relationship(back_populates="defects")

    __table_args__ = (
        # R28.3 — composite unique replaces the legacy `defect_id UNIQUE`
        # constraint. Per-run defect rows allowed.
        UniqueConstraint("defect_id", "run_id", name="uq_def_did_runid"),
        Index("idx_def_severity", "severity"),
        Index("idx_def_priority", "priority"),
        Index("idx_def_status", "status"),
        Index("idx_def_requirement", "requirement_id"),
        Index("idx_def_run", "run_id"),
        # R28.3 — speeds the common GET /api/defects?run_id=&priority=
        # query (R28.4). Partial index excludes legacy NULL-run rows.
        Index("idx_def_run_priority", "run_id", "priority"),
    )


class QualityGate(Base):
    __tablename__ = "quality_gates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False)
    build_id: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False, default="staging")
    decision: Mapped[GateDecisionEnum] = mapped_column(_gate_decision, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    blocking_checks: Mapped[dict] = mapped_column(JSONB, default=list)
    warnings: Mapped[dict] = mapped_column(JSONB, default=list)
    passed_checks: Mapped[dict] = mapped_column(JSONB, default=list)
    p0_coverage_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    p1_coverage_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    overall_coverage_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    p0_pass_rate_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    overall_pass_rate_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))
    open_p0_defects: Mapped[int | None] = mapped_column(Integer)
    performance_p95_ms: Mapped[int | None] = mapped_column(Integer)
    security_critical_findings: Mapped[int | None] = mapped_column(Integer)
    evidence_package_id: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    test_run: Mapped[TestRun] = relationship(back_populates="quality_gates")
    project: Mapped[Project | None] = relationship(back_populates="quality_gates_rel")

    __table_args__ = (
        Index("idx_gate_run", "run_id"),
        Index("idx_gate_build", "build_id"),
        Index("idx_gate_decision", "decision"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    color: Mapped[str] = mapped_column(String(7), default="#6366f1")
    icon: Mapped[str] = mapped_column(String(50), default="🧪")
    llm_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality_gates: Mapped[dict] = mapped_column(JSONB, default=dict)
    integrations: Mapped[dict] = mapped_column(JSONB, default=dict)
    engagement_model: Mapped[str | None] = mapped_column(Text)
    stack_type: Mapped[str | None] = mapped_column(Text)
    is_greenfield: Mapped[bool] = mapped_column(Boolean, default=True)
    # Phase B5 — when True, Stage 2.5 UI Discovery is skipped (no UI surface
    # to crawl; existing OpenAPI + operator env-var flow handles the project).
    is_api_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Phase I7 — per-project feature flags + runtime tunables for discovery.
    # Schema documented in 009_project_discovery_settings.sql.
    discovery_settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # M2 — per-environment config (base_url, auth profile, variables). Previously
    # persisted ONLY to .arta/projects.json (gitignored, host-local), so a lost
    # projects.json / fresh container erased a SUT's solved auth. Now durable in
    # Postgres so it survives redeploy. Schema documented in 013_project_environments.sql.
    environments: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    requirements: Mapped[list[Requirement]] = relationship(back_populates="project")
    test_cases: Mapped[list[TestCase]] = relationship(back_populates="project")
    test_runs: Mapped[list[TestRun]] = relationship(back_populates="project")
    defects: Mapped[list[Defect]] = relationship(back_populates="project")
    quality_gates_rel: Mapped[list[QualityGate]] = relationship(back_populates="project")
    roles: Mapped[list[ProjectRole]] = relationship(back_populates="project", cascade="all, delete-orphan")

    __table_args__ = (Index("idx_projects_name", "name"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    hashed_password: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oauth_provider: Mapped[str | None] = mapped_column(Text)
    oauth_id: Mapped[str | None] = mapped_column(Text)

    project_roles: Mapped[list[ProjectRole]] = relationship(back_populates="user", cascade="all, delete-orphan", foreign_keys="[ProjectRole.user_id]")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_users_email", "email"),
        UniqueConstraint("oauth_provider", "oauth_id", name="uq_users_oauth"),
    )


class ProjectRole(Base):
    __tablename__ = "project_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="project_roles", foreign_keys=[user_id])
    project: Mapped[Project] = relationship(back_populates="roles")

    __table_args__ = (
        UniqueConstraint("user_id", "project_id"),
        Index("idx_project_roles_user_id", "user_id"),
        Index("idx_project_roles_project_id", "project_id"),
        CheckConstraint("role IN ('admin', 'test_architect', 'qa_lead', 'tester', 'developer', 'viewer')", name="ck_project_role_valid"),
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (Index("idx_refresh_tokens_user_id", "user_id"),)


class InviteToken(Base):
    """Single-use invite tokens for the Admin → Invite User flow.

    The raw token is shown to the admin once (via API response) and never
    persisted; we store only its bcrypt hash, mirroring how RefreshToken
    works. project_id + project_role are optional invite-time hints — the
    actual project_roles row is created at acceptance, so we don't leave
    orphan role grants pointing at inactive users.
    """
    __tablename__ = "invite_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"))
    project_role: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_invite_tokens_user", "user_id"),
        Index("idx_invite_tokens_expires", "expires_at"),
    )


class TestDataFixture(Base):
    __tablename__ = "test_data_fixtures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    test_id: Mapped[str] = mapped_column(Text, nullable=False)
    fixture_id: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    columns: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    rows: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    examples: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (Index("idx_test_data_fixtures_test_id", "test_id"),)


class TestCaseVersion(Base):
    __tablename__ = "test_case_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    test_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    gherkin_snapshot: Mapped[str | None] = mapped_column(Text)
    script_snapshot: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(Text, default="arta-agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("test_id", "version"),
        Index("idx_test_case_versions_test_id", "test_id"),
    )


class RequirementVersion(Base):
    __tablename__ = "requirement_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    req_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title_snapshot: Mapped[str | None] = mapped_column(Text)
    description_snapshot: Mapped[str | None] = mapped_column(Text)
    ac_snapshot: Mapped[dict] = mapped_column(JSONB, default=list)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(Text, default="arta-agent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("req_id", "version"),
        Index("idx_requirement_versions_req_id", "req_id"),
    )


class HealingProposal(Base):
    __tablename__ = "healing_proposals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    proposal_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    test_id: Mapped[str] = mapped_column(Text, nullable=False)
    defect_id: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ai_reasoning: Mapped[str | None] = mapped_column(Text)
    current_selector: Mapped[str | None] = mapped_column(Text)
    proposed_selector: Mapped[str | None] = mapped_column(Text)
    diff_preview: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    applied_selector: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_healing_proposals_status", "status"),
        Index("idx_healing_proposals_test_id", "test_id"),
    )


class GateWaiver(Base):
    __tablename__ = "gate_waivers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    gate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("quality_gates.id", ondelete="CASCADE"), nullable=False)
    waived_check: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        Index("idx_gate_waivers_gate_id", "gate_id"),
        Index("idx_gate_waivers_expires", "expires_at"),
    )


class ExploratorySession(Base):
    __tablename__ = "exploratory_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    session_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    charter: Mapped[str] = mapped_column(Text, nullable=False)
    tagged_requirements: Mapped[dict] = mapped_column(JSONB, default=list)
    time_limit_minutes: Mapped[int] = mapped_column(Integer, default=60)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="active")
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    __table_args__ = (Index("idx_exploratory_sessions_status", "status"),)


class ExploratoryFinding(Base):
    __tablename__ = "exploratory_findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    finding_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, default="medium")
    screenshot_url: Mapped[str | None] = mapped_column(Text)
    converted_to_type: Mapped[str | None] = mapped_column(Text)
    converted_to_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("idx_exploratory_findings_session", "session_id"),)


class TestBlock(Base):
    __tablename__ = "test_blocks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    block_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    __table_args__ = (Index("idx_test_blocks_project", "project_id"),)


class TestCorrection(Base):
    """R320 — a tester's correction of an AI-generated test (refinement copilot).

    Records the correction + its provenance verdict (arta_knew vs human_knowledge)
    + the grounding fact it produced + the verify result. `created_at` uses the
    `_utcnow` default — callers must NEVER pass an ISO string (the exact type
    mismatch that 500'd the Exploratory router); let the default fire or pass a
    datetime object."""
    __tablename__ = "test_corrections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_new_uuid)
    correction_id: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    test_id: Mapped[str | None] = mapped_column(Text)
    requirement_id: Mapped[str | None] = mapped_column(Text)
    ac_id: Mapped[str | None] = mapped_column(Text)
    tool: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)             # endpoint|field_value|shape|intent
    field: Mapped[str | None] = mapped_column(Text)                     # field name (field_value corrections)
    correction_text: Mapped[str] = mapped_column(Text, nullable=False)
    from_value: Mapped[str | None] = mapped_column(Text)
    to_value: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(Text)                   # arta_knew|human_knowledge
    grounding_fact_ref: Mapped[str | None] = mapped_column(Text)        # "METHOD:path" written
    verify_status: Mapped[str | None] = mapped_column(Text)             # pending|passed|failed|not_run
    verify_run_id: Mapped[str | None] = mapped_column(Text)             # run that verifies this correction
    corrected_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (Index("idx_test_corrections_project", "project_id"),)
