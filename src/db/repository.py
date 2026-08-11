"""
ARTA Platform — Repository Layer
Typed CRUD helpers wrapping SQLAlchemy async sessions.
Each repository class operates on a single entity table.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

_log = logging.getLogger("arta.repository")

from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import (
    AcceptanceCriterion,
    Defect,
    DefectSeverity,
    DefectStatusEnum,
    ExecutionResult,
    ExecutionStatus,
    GateDecisionEnum,
    GateWaiver,
    HealingProposal,
    InviteToken,
    Project,
    ProjectRole,
    QualityGate,
    RefreshToken,
    Requirement,
    RequirementVersion,
    RiskPriority,
    RunStatus,
    TestBlock,
    TestCase,
    TestCaseVersion,
    TestDataFixture,
    TestRun,
    User,
    ExploratorySession,
    ExploratoryFinding,
    TestCorrection,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_dict(obj: Any, exclude: set[str] | None = None) -> dict:
    """Convert an ORM instance to a plain dict (serialisation helper).

    Uses the mapper's column_attrs to look up the *Python* attribute name for
    each column rather than the SQL column name. Models that use
    `mapped_column("metadata", ...)` to avoid the reserved word collision
    expose the data as `obj.metadata_` while the SQL column is `metadata` —
    the previous version did `getattr(obj, "metadata")` and got SQLAlchemy's
    class-level `MetaData` instance back, which then fed into FastAPI's
    `jsonable_encoder` and recursed forever (manifesting as the
    `/api/defects` recursion bug surfaced by F7-5 router tests).
    """
    exclude = exclude or set()
    d = {}
    # Build a one-shot lookup: SQL column key → Python attribute name.
    try:
        attr_lookup = {
            prop.columns[0].key: prop.key
            for prop in obj.__class__.__mapper__.column_attrs
        }
    except Exception as exc:
        # F15-6: Log instead of silent fallback. A failure here means
        # SQLAlchemy mapper introspection broke, which produces wrong
        # serialisation downstream — exactly the F7-5 `/api/defects`
        # recursion-bug shape. Surface in logs immediately so the next
        # variant of the same class isn't silent for hours.
        _log.warning(
            "_to_dict: failed to introspect SQLAlchemy mapper for %s "
            "(%s: %s) — falling back to raw column.key lookup; downstream "
            "serialisation may misbehave (see F7-5 / F12-10 for context)",
            obj.__class__.__name__, type(exc).__name__, exc,
        )
        attr_lookup = {}
    for col in obj.__table__.columns:
        if col.key in exclude:
            continue
        attr_name = attr_lookup.get(col.key, col.key)
        val = getattr(obj, attr_name, None)
        if isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, uuid.UUID):
            val = str(val)
        elif hasattr(val, "value"):  # enum
            val = val.value
        d[col.key] = val
    return d


# ── Requirement Repository ───────────────────────────────────────────────────


class RequirementRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(
        self,
        project_id: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[Requirement], int]:
        q = select(Requirement)
        count_q = select(func.count(Requirement.id))
        if project_id:
            # F4-3: A non-UUID project_id (e.g. "unknown" or a slug from URL) cannot
            # match any DB row. Short-circuit instead of letting uuid.UUID() raise
            # ValueError — the contract is "unknown project → empty list".
            try:
                pid = uuid.UUID(project_id)
            except (ValueError, AttributeError):
                return [], 0
            q = q.where(Requirement.project_id == pid)
            count_q = count_q.where(Requirement.project_id == pid)
        if priority:
            q = q.where(Requirement.priority == RiskPriority(priority))
            count_q = count_q.where(Requirement.priority == RiskPriority(priority))
        if status:
            q = q.where(Requirement.status == status)
            count_q = count_q.where(Requirement.status == status)

        total = (await self.db.execute(count_q)).scalar() or 0
        q = q.options(selectinload(Requirement.acceptance_criteria))
        q = q.order_by(Requirement.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(q)).scalars().all()
        return rows, total

    async def get(self, req_id: str) -> Requirement | None:
        q = select(Requirement).where(Requirement.req_id == req_id)
        q = q.options(selectinload(Requirement.acceptance_criteria))
        return (await self.db.execute(q)).scalars().first()

    async def get_by_uuid(self, uid: uuid.UUID) -> Requirement | None:
        q = select(Requirement).where(Requirement.id == uid)
        q = q.options(selectinload(Requirement.acceptance_criteria))
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> Requirement:
        obj = Requirement(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, req_id: str, data: dict) -> Requirement | None:
        obj = await self.get(req_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj

    async def delete(self, req_id: str) -> bool:
        obj = await self.get(req_id)
        if not obj:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True


    async def upsert_with_change_detection(
        self,
        data: dict,
        content_hash: str,
        changed_by: str = "arta-agent",
    ) -> tuple[Requirement, str]:
        """
        Insert or update a requirement with change detection.

        Returns (requirement, change_type) where change_type is one of:
        'created', 'modified', or 'unchanged'.
        """
        req_id = data.get("req_id") or data.get("id", "")
        existing = await self.get(req_id)

        if not existing:
            # New requirement
            data["content_hash"] = content_hash
            data["version"] = 1
            obj = await self.create(data)
            # Snapshot the initial version
            ver_repo = RequirementVersionRepo(self.db)
            await ver_repo.create_version({
                "req_id": req_id,
                "title_snapshot": data.get("title", ""),
                "description_snapshot": data.get("description", ""),
                "ac_snapshot": data.get("acceptance_criteria_snapshot", []),
                "content_hash": content_hash,
                "change_type": "created",
                "changed_by": changed_by,
            })
            return obj, "created"

        if existing.content_hash == content_hash:
            # No changes
            return existing, "unchanged"

        # Modified — snapshot the OLD state before updating
        ver_repo = RequirementVersionRepo(self.db)
        old_acs = [
            {"id": ac.ac_id, "statement": ac.title, "covered": ac.is_covered}
            for ac in (existing.acceptance_criteria or [])
        ]
        await ver_repo.create_version({
            "req_id": req_id,
            "title_snapshot": existing.title,
            "description_snapshot": existing.description,
            "ac_snapshot": old_acs,
            "content_hash": existing.content_hash or "",
            "change_type": "modified",
            "change_reason": f"Content changed (hash {(existing.content_hash or '')[:8]}... → {content_hash[:8]}...)",
            "changed_by": changed_by,
        })

        # Update the requirement
        update_fields = {k: v for k, v in data.items() if k not in ("req_id", "id")}
        update_fields["content_hash"] = content_hash
        update_fields["version"] = (existing.version or 1) + 1
        for k, v in update_fields.items():
            if hasattr(existing, k):
                setattr(existing, k, v)
        await self.db.flush()
        return existing, "modified"

    async def detect_deletions(
        self,
        incoming_req_ids: set[str],
        source_type: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        """
        Find requirements in DB from same source/project NOT in incoming set.
        Marks them as 'deprecated' status. Returns list of deprecated req_ids.
        """
        from .models import ReqStatus, SourceType as SourceTypeEnum
        q = select(Requirement).where(
            Requirement.req_id.notin_(incoming_req_ids),
            Requirement.status != ReqStatus.deprecated,
        )
        if source_type:
            try:
                q = q.where(Requirement.source_type == SourceTypeEnum(source_type))
            except ValueError:
                pass
        if project_id:
            try:
                q = q.where(Requirement.project_id == uuid.UUID(project_id))
            except (ValueError, AttributeError):
                return []

        rows = (await self.db.execute(q)).scalars().all()
        deprecated_ids = []
        for r in rows:
            r.status = ReqStatus.deprecated
            deprecated_ids.append(r.req_id)
        if deprecated_ids:
            await self.db.flush()
        return deprecated_ids


# ── RequirementVersion Repository ──────────────────────────────────────────


class RequirementVersionRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list_versions(self, req_id: str) -> Sequence[RequirementVersion]:
        q = select(RequirementVersion).where(
            RequirementVersion.req_id == req_id
        ).order_by(RequirementVersion.version.desc())
        return (await self.db.execute(q)).scalars().all()

    async def get_version(self, req_id: str, version: int) -> RequirementVersion | None:
        q = select(RequirementVersion).where(
            RequirementVersion.req_id == req_id,
            RequirementVersion.version == version,
        )
        return (await self.db.execute(q)).scalars().first()

    async def create_version(self, data: dict) -> RequirementVersion:
        # Auto-increment version number
        latest = await self.db.execute(
            select(func.max(RequirementVersion.version)).where(
                RequirementVersion.req_id == data["req_id"]
            )
        )
        max_ver = latest.scalar() or 0
        data["version"] = max_ver + 1
        obj = RequirementVersion(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj


# ── AcceptanceCriterion Repository ───────────────────────────────────────────


class AcceptanceCriterionRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list_for_requirement(self, requirement_uuid: uuid.UUID) -> Sequence[AcceptanceCriterion]:
        q = select(AcceptanceCriterion).where(
            AcceptanceCriterion.requirement_id == requirement_uuid
        ).order_by(AcceptanceCriterion.ac_id)
        return (await self.db.execute(q)).scalars().all()

    async def get(self, ac_id: str) -> AcceptanceCriterion | None:
        q = select(AcceptanceCriterion).where(AcceptanceCriterion.ac_id == ac_id)
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> AcceptanceCriterion:
        obj = AcceptanceCriterion(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def mark_covered(self, ac_id: str, covered: bool = True) -> None:
        await self.db.execute(
            update(AcceptanceCriterion)
            .where(AcceptanceCriterion.ac_id == ac_id)
            .values(is_covered=covered, coverage_pct=100.0 if covered else 0.0)
        )


# ── TestCase Repository ──────────────────────────────────────────────────────


class TestCaseRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(
        self,
        project_id: str | None = None,
        priority: str | None = None,
        tool: str | None = None,
        status: str | None = None,
        flaky_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[TestCase], int]:
        q = select(TestCase)
        count_q = select(func.count(TestCase.id))
        filters = []
        if project_id:
            try:
                filters.append(TestCase.project_id == uuid.UUID(project_id))
            except (ValueError, AttributeError):
                return [], 0
        if priority:
            filters.append(TestCase.priority == RiskPriority(priority))
        if tool:
            filters.append(TestCase.automation_tool == tool)
        if status:
            filters.append(TestCase.last_status == ExecutionStatus(status))
        if flaky_only:
            filters.append(TestCase.is_flaky == True)
        for f in filters:
            q = q.where(f)
            count_q = count_q.where(f)

        total = (await self.db.execute(count_q)).scalar() or 0
        q = q.order_by(TestCase.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(q)).scalars().all()
        return rows, total

    async def get(self, test_id: str) -> TestCase | None:
        q = select(TestCase).where(TestCase.test_id == test_id)
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> TestCase:
        obj = TestCase(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, test_id: str, data: dict) -> TestCase | None:
        obj = await self.get(test_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj

    async def delete(self, test_id: str) -> bool:
        obj = await self.get(test_id)
        if not obj:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True


# ── TestRun Repository ───────────────────────────────────────────────────────


class TestRunRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(
        self,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[Sequence[TestRun], int]:
        from sqlalchemy import or_
        q = select(TestRun)
        count_q = select(func.count(TestRun.id))
        if project_id:
            try:
                pid = uuid.UUID(project_id)
            except (ValueError, AttributeError):
                return [], 0
            q = q.where(TestRun.project_id == pid)
            count_q = count_q.where(TestRun.project_id == pid)
        if status:
            q = q.where(TestRun.status == RunStatus(status))
            count_q = count_q.where(TestRun.status == RunStatus(status))
        total = (await self.db.execute(count_q)).scalar() or 0
        q = q.order_by(TestRun.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(q)).scalars().all()
        return rows, total

    async def get(self, run_id: str) -> TestRun | None:
        q = select(TestRun).where(TestRun.run_id == run_id)
        q = q.options(selectinload(TestRun.execution_results))
        row = (await self.db.execute(q)).scalars().first()
        if row:
            return row
        # Fallback: try as UUID primary key
        try:
            q2 = select(TestRun).where(TestRun.id == uuid.UUID(run_id))
            q2 = q2.options(selectinload(TestRun.execution_results))
            return (await self.db.execute(q2)).scalars().first()
        except (ValueError, Exception):
            return None

    async def create(self, data: dict) -> TestRun:
        obj = TestRun(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, run_id: str, data: dict) -> TestRun | None:
        obj = await self.get(run_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj


# ── ExecutionResult Repository ───────────────────────────────────────────────


class ExecutionResultRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list_for_run(self, run_uuid: uuid.UUID) -> Sequence[ExecutionResult]:
        q = select(ExecutionResult).where(
            ExecutionResult.run_id == run_uuid
        ).order_by(ExecutionResult.executed_at)
        return (await self.db.execute(q)).scalars().all()

    async def create(self, data: dict) -> ExecutionResult:
        obj = ExecutionResult(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def bulk_create(self, items: list[dict]) -> list[ExecutionResult]:
        objs = [ExecutionResult(**d) for d in items]
        self.db.add_all(objs)
        await self.db.flush()
        return objs


# ── Defect Repository ────────────────────────────────────────────────────────


class DefectRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(
        self,
        project_id: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        run_id: str | None = None,
        triage_category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Defect], int]:
        """List defects, optionally filtered.

        R28.4 — added `priority` and `run_id` parameters. The `run_id`
        parameter accepts either:
          - the test_runs.id UUID directly, OR
          - the operator-facing external run_id string (e.g.
            "46ff0bd0-4cd5-4728-a44c-194ea5afa7a6") which is resolved
            to test_runs.id via a SELECT.

        Both forms map to `Defect.run_id` (FK to test_runs.id).

        R30.8 — added `triage_category` filter. Backed by the indexed
        `triage_category` column added in migration 011. The /api/triage
        queue uses this filter to fetch operator_review defects via an
        index scan instead of post-fetch JSONB filtering.
        """
        q = select(Defect)
        count_q = select(func.count(Defect.id))
        if project_id:
            try:
                pid = uuid.UUID(project_id)
            except (ValueError, AttributeError):
                return [], 0
            q = q.where(Defect.project_id == pid)
            count_q = count_q.where(Defect.project_id == pid)
        if severity:
            q = q.where(Defect.severity == DefectSeverity(severity))
            count_q = count_q.where(Defect.severity == DefectSeverity(severity))
        if status:
            q = q.where(Defect.status == DefectStatusEnum(status))
            count_q = count_q.where(Defect.status == DefectStatusEnum(status))
        if triage_category:
            q = q.where(Defect.triage_category == triage_category)
            count_q = count_q.where(Defect.triage_category == triage_category)
        if priority:
            try:
                from .models import RiskPriority
                q = q.where(Defect.priority == RiskPriority(priority))
                count_q = count_q.where(Defect.priority == RiskPriority(priority))
            except (ValueError, AttributeError):
                # Unknown priority enum value → empty result
                return [], 0
        if run_id:
            # R28.4 — resolve external run_id (e.g. "46ff0bd0-...") to
            # test_runs.id UUID. The Defect.run_id FK points at
            # test_runs.id, but operators query by the user-facing
            # run_id string.
            run_uuid: uuid.UUID | None = None
            try:
                run_uuid = uuid.UUID(run_id)
            except (ValueError, AttributeError):
                run_uuid = None
            if run_uuid is None:
                # Try external run_id string lookup in test_runs
                from .models import TestRun
                tr_q = select(TestRun.id).where(TestRun.run_id == run_id).limit(1)
                tr_row = (await self.db.execute(tr_q)).scalar_one_or_none()
                if tr_row is None:
                    # Unknown run_id → empty (vs. matching all)
                    return [], 0
                run_uuid = tr_row
            else:
                # UUID could be either test_runs.id directly OR external
                # run_id. Try external first, fall back to direct.
                from .models import TestRun
                tr_q = select(TestRun.id).where(TestRun.run_id == run_id).limit(1)
                tr_row = (await self.db.execute(tr_q)).scalar_one_or_none()
                if tr_row is not None:
                    run_uuid = tr_row
            q = q.where(Defect.run_id == run_uuid)
            count_q = count_q.where(Defect.run_id == run_uuid)
        total = (await self.db.execute(count_q)).scalar() or 0
        q = q.order_by(Defect.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(q)).scalars().all()
        return rows, total

    async def get(self, defect_id: str) -> Defect | None:
        q = select(Defect).where(Defect.defect_id == defect_id)
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> Defect:
        obj = Defect(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, defect_id: str, data: dict) -> Defect | None:
        obj = await self.get(defect_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj


# ── QualityGate Repository ───────────────────────────────────────────────────


class QualityGateRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def get_for_build(self, build_id: str) -> QualityGate | None:
        q = select(QualityGate).where(QualityGate.build_id == build_id)
        q = q.order_by(QualityGate.assessed_at.desc())
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> QualityGate:
        obj = QualityGate(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj


# ── Project Repository ───────────────────────────────────────────────────────


class ProjectRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(self, limit: int = 50) -> Sequence[Project]:
        q = select(Project).order_by(Project.name).limit(limit)
        return (await self.db.execute(q)).scalars().all()

    async def get(self, project_id: str) -> Project | None:
        try:
            pid = uuid.UUID(project_id)
        except (ValueError, AttributeError):
            return None
        q = select(Project).where(Project.id == pid)
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> Project:
        obj = Project(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, project_id: str, data: dict) -> Project | None:
        obj = await self.get(project_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj

    async def delete(self, project_id: str) -> bool:
        obj = await self.get(project_id)
        if not obj:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True

    async def summary(self, project_id: str) -> dict:
        """Quick dashboard summary for one project."""
        pid = uuid.UUID(project_id)
        req_count = (await self.db.execute(
            select(func.count(Requirement.id)).where(Requirement.project_id == pid)
        )).scalar() or 0
        tc_count = (await self.db.execute(
            select(func.count(TestCase.id)).where(TestCase.project_id == pid)
        )).scalar() or 0
        defect_count = (await self.db.execute(
            select(func.count(Defect.id)).where(Defect.project_id == pid)
        )).scalar() or 0
        open_p0 = (await self.db.execute(
            select(func.count(Defect.id)).where(
                Defect.project_id == pid,
                Defect.status == DefectStatusEnum.open,
                Defect.priority == RiskPriority.P0,
            )
        )).scalar() or 0
        return {
            "project_id": project_id,
            "requirements": req_count,
            "test_cases": tc_count,
            "defects": defect_count,
            "open_p0_defects": open_p0,
        }


# ── User Repository ──────────────────────────────────────────────────────────


class UserRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(self, limit: int = 100) -> Sequence[User]:
        q = select(User).order_by(User.email).limit(limit)
        return (await self.db.execute(q)).scalars().all()

    async def get_by_email(self, email: str) -> User | None:
        q = select(User).where(User.email == email)
        return (await self.db.execute(q)).scalars().first()

    async def get_by_id(self, user_id: str) -> User | None:
        q = select(User).where(User.id == uuid.UUID(user_id))
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> User:
        obj = User(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, user_id: str, data: dict) -> User | None:
        obj = await self.get_by_id(user_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj

    async def deactivate(self, user_id: str) -> bool:
        obj = await self.get_by_id(user_id)
        if not obj:
            return False
        obj.is_active = False
        await self.db.flush()
        return True

    async def reactivate(self, user_id: str) -> bool:
        """Inverse of deactivate — flips a soft-deleted user back to active.
        The DELETE /api/users/{id} endpoint only sets is_active=False (it
        never hard-deletes), so reactivation restores the account intact:
        password, project roles, and refresh tokens are untouched."""
        obj = await self.get_by_id(user_id)
        if not obj:
            return False
        obj.is_active = True
        await self.db.flush()
        return True

    async def count_active_admins(self) -> int:
        """Number of active platform admins — used to refuse deactivating the
        last one (which would leave the platform with no administrator)."""
        q = (
            select(func.count())
            .select_from(User)
            .where(User.is_admin.is_(True), User.is_active.is_(True))
        )
        return int((await self.db.execute(q)).scalar_one())


# ── ProjectRole Repository ───────────────────────────────────────────────────


class ProjectRoleRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def get_role(self, user_id: str, project_id: str) -> ProjectRole | None:
        q = select(ProjectRole).where(
            ProjectRole.user_id == uuid.UUID(user_id),
            ProjectRole.project_id == uuid.UUID(project_id),
        )
        return (await self.db.execute(q)).scalars().first()

    async def list_for_user(self, user_id: str) -> Sequence[ProjectRole]:
        q = select(ProjectRole).where(ProjectRole.user_id == uuid.UUID(user_id))
        q = q.options(selectinload(ProjectRole.project))
        return (await self.db.execute(q)).scalars().all()

    async def list_for_project(self, project_id: str) -> Sequence[ProjectRole]:
        q = select(ProjectRole).where(ProjectRole.project_id == uuid.UUID(project_id))
        q = q.options(selectinload(ProjectRole.user))
        return (await self.db.execute(q)).scalars().all()

    async def assign(self, user_id: str, project_id: str, role: str, granted_by: str | None = None) -> ProjectRole:
        existing = await self.get_role(user_id, project_id)
        if existing:
            existing.role = role
            await self.db.flush()
            return existing
        obj = ProjectRole(
            user_id=uuid.UUID(user_id),
            project_id=uuid.UUID(project_id),
            role=role,
            granted_by=uuid.UUID(granted_by) if granted_by else None,
        )
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def revoke(self, user_id: str, project_id: str) -> bool:
        result = await self.db.execute(
            delete(ProjectRole).where(
                ProjectRole.user_id == uuid.UUID(user_id),
                ProjectRole.project_id == uuid.UUID(project_id),
            )
        )
        return result.rowcount > 0


# ── RefreshToken Repository ──────────────────────────────────────────────────


class RefreshTokenRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def create(self, user_id: str, token_hash: str, expires_at: datetime) -> RefreshToken:
        obj = RefreshToken(
            user_id=uuid.UUID(user_id),
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        q = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return (await self.db.execute(q)).scalars().first()

    async def delete_by_hash(self, token_hash: str) -> bool:
        result = await self.db.execute(
            delete(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        return result.rowcount > 0

    async def delete_for_user(self, user_id: str) -> int:
        result = await self.db.execute(
            delete(RefreshToken).where(RefreshToken.user_id == uuid.UUID(user_id))
        )
        return result.rowcount


# ── InviteToken Repository ──────────────────────────────────────────────────


class InviteTokenRepo:
    """Persistence for the Admin → Invite User flow.

    Tokens are stored as bcrypt hashes (the raw token is shown to the admin
    once and never persisted). Lookup is by full table scan over the small
    set of unaccepted, unexpired rows — bcrypt has no equality search.
    """

    def __init__(self, session: AsyncSession):
        self.db = session

    async def create(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        invited_by: str | None = None,
        project_id: str | None = None,
        project_role: str | None = None,
    ) -> InviteToken:
        obj = InviteToken(
            user_id=uuid.UUID(user_id),
            token_hash=token_hash,
            expires_at=expires_at,
            invited_by=uuid.UUID(invited_by) if invited_by else None,
            project_id=uuid.UUID(project_id) if project_id else None,
            project_role=project_role,
        )
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def get_by_hash(self, token_hash: str) -> InviteToken | None:
        q = select(InviteToken).where(InviteToken.token_hash == token_hash)
        return (await self.db.execute(q)).scalars().first()

    async def mark_accepted(self, invite_id: uuid.UUID) -> None:
        await self.db.execute(
            update(InviteToken)
            .where(InviteToken.id == invite_id)
            .values(accepted_at=_utcnow())
        )
        await self.db.flush()

    async def purge_expired(self) -> int:
        """Best-effort cleanup. Not currently scheduled — list_active filters
        these out at query time, so the table just grows slowly."""
        result = await self.db.execute(
            delete(InviteToken).where(InviteToken.expires_at < _utcnow())
        )
        return result.rowcount


# ── TestCaseVersion Repository ───────────────────────────────────────────────


class TestCaseVersionRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list_versions(self, test_id: str) -> Sequence[TestCaseVersion]:
        q = select(TestCaseVersion).where(
            TestCaseVersion.test_id == test_id
        ).order_by(TestCaseVersion.version.desc())
        return (await self.db.execute(q)).scalars().all()

    async def get_version(self, test_id: str, version: int) -> TestCaseVersion | None:
        q = select(TestCaseVersion).where(
            TestCaseVersion.test_id == test_id,
            TestCaseVersion.version == version,
        )
        return (await self.db.execute(q)).scalars().first()

    async def create_version(self, data: dict) -> TestCaseVersion:
        # Auto-increment version number
        latest = await self.db.execute(
            select(func.max(TestCaseVersion.version)).where(
                TestCaseVersion.test_id == data["test_id"]
            )
        )
        max_ver = latest.scalar() or 0
        data["version"] = max_ver + 1
        obj = TestCaseVersion(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj


# ── TestDataFixture Repository ───────────────────────────────────────────────


class TestDataFixtureRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list_for_test(self, test_id: str) -> Sequence[TestDataFixture]:
        q = select(TestDataFixture).where(TestDataFixture.test_id == test_id)
        return (await self.db.execute(q)).scalars().all()

    async def create(self, data: dict) -> TestDataFixture:
        obj = TestDataFixture(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj


# ── Dashboard Repository ─────────────────────────────────────────────────────


class DashboardRepo:
    """Aggregation queries for the dashboard endpoint."""

    def __init__(self, session: AsyncSession):
        self.db = session

    async def quality_metrics(self, project_id: str | None = None) -> dict:
        """Compute quality_score, coverage_pct, pass_rate, open_defects."""
        from sqlalchemy import text

        # Use the v_coverage_summary view if available, else compute inline
        filters = ""
        params: dict[str, Any] = {}
        if project_id:
            filters = "WHERE r.project_id = :pid"
            params["pid"] = project_id

        coverage_sql = text(f"""
            SELECT
                COALESCE(ROUND(
                    COUNT(ac.id) FILTER (WHERE ac.is_covered) * 100.0
                    / NULLIF(COUNT(ac.id), 0), 1
                ), 0) AS coverage_pct,
                COUNT(DISTINCT r.id) AS total_reqs
            FROM requirements r
            LEFT JOIN acceptance_criteria ac ON ac.requirement_id = r.id
            {filters}
        """)
        cov_row = (await self.db.execute(coverage_sql, params)).first()

        pass_rate_sql = text(f"""
            SELECT
                COALESCE(ROUND(
                    COUNT(tc.id) FILTER (WHERE tc.last_status = 'PASS') * 100.0
                    / NULLIF(COUNT(tc.id), 0), 1
                ), 0) AS pass_rate,
                COUNT(tc.id) AS total_scenarios
            FROM test_cases tc
            {"JOIN requirements r ON tc.requirement_id = r.id " + filters if project_id else ""}
        """)
        pr_row = (await self.db.execute(pass_rate_sql, params)).first()

        defect_sql = text(f"""
            SELECT
                COUNT(*) FILTER (WHERE d.status = 'open') AS open_defects,
                COUNT(*) FILTER (WHERE d.status = 'open' AND d.priority = 'P0') AS p0_defects
            FROM defects d
            {filters.replace('r.', 'd.') if project_id else ""}
        """)
        def_row = (await self.db.execute(defect_sql, params)).first()

        coverage = float(cov_row[0]) if cov_row else 0
        pass_rate = float(pr_row[0]) if pr_row else 0
        open_defects = int(def_row[0]) if def_row else 0
        p0_defects = int(def_row[1]) if def_row else 0
        total_scenarios = int(pr_row[1]) if pr_row else 0

        risk_factor = min(p0_defects / max(total_scenarios, 1), 1.0)
        quality_score = round(0.4 * coverage + 0.4 * pass_rate + 0.2 * (1 - risk_factor) * 100, 1)

        return {
            "quality_score": quality_score,
            "coverage_pct": coverage,
            "pass_rate": pass_rate,
            "open_defects": open_defects,
            "p0_defects": p0_defects,
            "total_scenarios": total_scenarios,
        }


# ── HealingProposal Repository ─────────────────────────────────────────────


class HealingProposalRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def list(
        self,
        status: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
    ) -> tuple[list, int]:
        q = select(HealingProposal)
        if status:
            q = q.where(HealingProposal.status == status)
        if project_id and hasattr(HealingProposal, "project_id"):
            q = q.where(HealingProposal.project_id == uuid.UUID(project_id))
        q = q.order_by(HealingProposal.created_at.desc()).limit(limit)
        rows = list((await self.db.execute(q)).scalars().all())
        return rows, len(rows)

    async def get(self, proposal_id: str) -> HealingProposal | None:
        q = select(HealingProposal).where(HealingProposal.proposal_id == proposal_id)
        return (await self.db.execute(q)).scalars().first()

    async def create(self, data: dict) -> HealingProposal:
        obj = HealingProposal(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def update(self, proposal_id: str, data: dict) -> HealingProposal | None:
        obj = await self.get(proposal_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj


# ── Exploratory Testing ───────────────────────────────────────────────


class ExploratorySessionRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, data: dict) -> ExploratorySession:
        obj = ExploratorySession(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def list(self, status: str | None = None, limit: int = 50) -> Sequence[ExploratorySession]:
        q = select(ExploratorySession)
        if status:
            q = q.where(ExploratorySession.status == status)
        q = q.order_by(ExploratorySession.started_at.desc()).limit(limit)
        return (await self.db.execute(q)).scalars().all()

    async def get(self, session_id: str) -> ExploratorySession | None:
        q = select(ExploratorySession).where(ExploratorySession.session_id == session_id)
        return (await self.db.execute(q)).scalars().first()

    async def complete(self, session_id: str, completed_at: str) -> ExploratorySession | None:
        obj = await self.get(session_id)
        if not obj:
            return None
        obj.completed_at = completed_at
        obj.status = "completed"
        await self.db.flush()
        return obj


class ExploratoryFindingRepo:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, data: dict) -> ExploratoryFinding:
        obj = ExploratoryFinding(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def list_for_session(self, session_id: str) -> Sequence[ExploratoryFinding]:
        q = (
            select(ExploratoryFinding)
            .where(ExploratoryFinding.session_id == session_id)
            .order_by(ExploratoryFinding.created_at.asc())
        )
        return (await self.db.execute(q)).scalars().all()


class TestCorrectionRepo:
    """R320 — CRUD for tester corrections (refinement copilot audit spine)."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _sanitize(data: dict) -> dict:
        """Coerce project_id str→UUID (avoids the asyncpg type-mismatch flush
        error) and DROP `created_at` if a string was passed (let the model
        default fire — never persist an ISO string into a TIMESTAMPTZ column)."""
        d = dict(data)
        pid = d.get("project_id")
        if isinstance(pid, str):
            try:
                d["project_id"] = uuid.UUID(pid)
            except (ValueError, TypeError):
                d["project_id"] = None
        if isinstance(d.get("created_at"), str):
            d.pop("created_at", None)
        return d

    async def create(self, data: dict) -> TestCorrection:
        obj = TestCorrection(**self._sanitize(data))
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def get(self, correction_id: str) -> TestCorrection | None:
        q = select(TestCorrection).where(TestCorrection.correction_id == correction_id)
        return (await self.db.execute(q)).scalars().first()

    async def list(self, project_id: str | None = None, limit: int = 100) -> Sequence[TestCorrection]:
        q = select(TestCorrection)
        if project_id:
            try:
                q = q.where(TestCorrection.project_id == uuid.UUID(project_id))
            except (ValueError, TypeError):
                pass
        q = q.order_by(TestCorrection.created_at.desc()).limit(limit)
        return (await self.db.execute(q)).scalars().all()

    async def set_verify_status(self, correction_id: str, status: str) -> TestCorrection | None:
        obj = await self.get(correction_id)
        if obj:
            obj.verify_status = status
            await self.db.flush()
        return obj

    async def delete(self, correction_id: str) -> TestCorrection | None:
        obj = await self.get(correction_id)
        if obj:
            await self.db.delete(obj)
            await self.db.flush()
        return obj


class GateWaiverRepo:
    """CRUD for gate waivers — BMAD TEA authorized exceptions."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create(self, data: dict) -> dict:
        obj = GateWaiver(**data)
        self.db.add(obj)
        await self.db.flush()
        return _to_dict(obj)

    async def list_active(self, gate_id: str | None = None) -> Sequence[GateWaiver]:
        q = select(GateWaiver).where(GateWaiver.expires_at > _utcnow())
        if gate_id:
            q = q.where(GateWaiver.gate_id == gate_id)
        q = q.order_by(GateWaiver.created_at.desc())
        return (await self.db.execute(q)).scalars().all()

    async def list_expired(self) -> Sequence[GateWaiver]:
        q = (
            select(GateWaiver)
            .where(GateWaiver.expires_at <= _utcnow())
            .order_by(GateWaiver.expires_at.desc())
        )
        return (await self.db.execute(q)).scalars().all()


# ── TestBlock Repository ────────────────────────────────────────────────────


class TestBlockRepo:
    def __init__(self, session: AsyncSession):
        self.db = session

    async def create(self, data: dict) -> TestBlock:
        obj = TestBlock(**data)
        self.db.add(obj)
        await self.db.flush()
        return obj

    async def list(
        self,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[TestBlock], int]:
        q = select(TestBlock)
        count_q = select(func.count(TestBlock.id))
        if project_id:
            q = q.where(TestBlock.project_id == uuid.UUID(project_id))
            count_q = count_q.where(TestBlock.project_id == uuid.UUID(project_id))
        total = (await self.db.execute(count_q)).scalar() or 0
        q = q.order_by(TestBlock.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.db.execute(q)).scalars().all()
        return rows, total

    async def get(self, block_id: str) -> TestBlock | None:
        q = select(TestBlock).where(TestBlock.block_id == block_id)
        return (await self.db.execute(q)).scalars().first()

    async def update(self, block_id: str, data: dict) -> TestBlock | None:
        obj = await self.get(block_id)
        if not obj:
            return None
        for k, v in data.items():
            if hasattr(obj, k):
                setattr(obj, k, v)
        await self.db.flush()
        return obj

    async def delete(self, block_id: str) -> bool:
        obj = await self.get(block_id)
        if not obj:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True

    async def increment_usage(self, block_id: str) -> TestBlock | None:
        obj = await self.get(block_id)
        if not obj:
            return None
        obj.usage_count = (obj.usage_count or 0) + 1
        await self.db.flush()
        return obj
