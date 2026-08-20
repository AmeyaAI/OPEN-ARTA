"""F7-2 (M2 partial) — test-case version-history sub-router.

Carved out of `tests.py` so the version + rollback feature lives on its own.
Endpoints mount under `/api/tests/{test_id}/...` via include_router. URLs are
unchanged from before the split.

Endpoints:
  GET  /{test_id}/versions                  — list all versions
  GET  /{test_id}/versions/{v1}/diff/{v2}   — unified diff between two versions
  POST /{test_id}/versions                  — create a new version snapshot
  POST /{test_id}/rollback                  — F3-2 rollback (default: one-back)
  POST /{test_id}/versions/{version}/revert — explicit revert to a specific version
  POST /requirements/{req_id}/restore-previous-generation — undo the last force-regen

State (MOCK_VERSIONS) is shared via tests_state.py so cross-router code keeps
working.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as _sql

from ..dependencies import require_api_key as _require_api_key
from .tests_state import MOCK_VERSIONS

log = logging.getLogger("arta.tests_versions")

versions_router = APIRouter()


# ── Versioning helpers (snapshot before destroy + atomic revert) ─────────────
# The versioning stack (test_case_versions + TestCaseVersionRepo + the revert
# endpoints) pre-dates these; it was never FED. These two helpers feed it and
# make revert restore ALL of a script's stores (disk = canonical/executable,
# DB script_content = UI cache, sidecar = durable, metadata = traceability).

# Repo root + traversal-guard boundary, derived from THIS module's location (not
# Path.cwd(), which silently breaks the guard if the service starts elsewhere).
# tests_versions.py = <root>/src/api/routers/ → parents[3] is <root>.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AUTOMATION_ROOT = (_REPO_ROOT / "src" / "automation").resolve()


def _canonical_script(script_path: str | None, script_content_cache: str | None) -> str:
    """Canonical script = the on-disk file at script_path (source of truth /
    what actually executes), falling back to the DB cache when the file is
    absent/unreadable — a hand-edit on disk must not be snapshotted as a stale
    cache."""
    if script_path:
        try:
            p = Path(script_path)
            if not p.is_absolute():
                p = _REPO_ROOT / p
            if p.is_file():
                return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return script_content_cache or ""


def _snapshot_hash(gherkin, script, metadata, scaffold) -> str:
    """Stable content fingerprint for the dedup gate — includes gherkin, script,
    the traceability metadata (what migration 014 preserves) AND the row scaffold
    (title/priority/tags/... — what migration 015 resurrects). Hashing only
    gherkin+script would skip a metadata-only or scaffold-only change as a
    'duplicate' and leave the resurrection scaffold stale."""
    payload = json.dumps({
        "g": gherkin or "", "s": script or "",
        "m": metadata if isinstance(metadata, dict) else None,
        "r": scaffold if isinstance(scaffold, dict) else None,
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


async def snapshot_test_cases(db, test_ids, batch_id: str, *,
                              reason_prefix: str = "pre_regen",
                              hash_gate: bool = True) -> int:
    """Snapshot the CURRENT test_cases rows into test_case_versions BEFORE they
    are overwritten/deleted — feeding the existing revert stack. Captures the
    canonical disk script + gherkin + metadata (traceability spine). Content-hash
    gated (skip dup vs latest) unless hash_gate=False. Fail-open + killswitch
    ARTA_TESTCASE_VERSIONING_DISABLE — a snapshot failure must NEVER block regen
    (but logs LOUD: history was skipped). Returns number of versions created."""
    test_ids = [t for t in (test_ids or []) if t]
    if not test_ids or os.environ.get("ARTA_TESTCASE_VERSIONING_DISABLE") == "1":
        return 0
    try:
        from ...db.repository import TestCaseVersionRepo
        ver_repo = TestCaseVersionRepo(db)
        rows = (await db.execute(_sql(
            "SELECT test_id, gherkin_scenario, script_content, script_path, metadata, "
            "title, automation_tool::text AS tool, priority::text AS priority, "
            "test_type::text AS test_type, requirement_id::text AS requirement_id, "
            "ac_id::text AS ac_id, project_id::text AS project_id, tags, "
            "is_automated, is_flaky, last_status::text AS last_status "
            "FROM test_cases WHERE test_id = ANY(:ids)"), {"ids": test_ids})).all()
        made = 0
        for r in rows:
            gherkin = r.gherkin_scenario or ""
            canonical = _canonical_script(r.script_path, r.script_content)
            metadata = r.metadata if isinstance(r.metadata, dict) else None
            # Full-row scaffold — enough to RESURRECT this row if a later regen
            # deletes it outright (test_cases has NOT-NULL title/type/tool/priority).
            row_scaffold = {
                "test_id": r.test_id, "title": r.title, "automation_tool": r.tool,
                "priority": r.priority, "test_type": r.test_type,
                "requirement_id": r.requirement_id, "ac_id": r.ac_id,
                "project_id": r.project_id, "script_path": r.script_path,
                "tags": list(r.tags) if r.tags else [],
                "is_automated": r.is_automated, "is_flaky": r.is_flaky,
                "last_status": r.last_status,
            }
            if hash_gate:
                h = _snapshot_hash(gherkin, canonical, metadata, row_scaffold)
                latest = await ver_repo.list_versions(r.test_id)
                if latest:
                    lv = latest[0]
                    lh = _snapshot_hash(lv.gherkin_snapshot, lv.script_snapshot,
                                        getattr(lv, "metadata_snapshot", None),
                                        getattr(lv, "row_snapshot", None))
                    if lh == h:
                        continue  # unchanged since last snapshot — no dup row
            # Per-row SAVEPOINT: one row's failure (e.g. a version-number race)
            # rolls back ONLY that row and leaves the txn healthy for the rest —
            # so a single bad row can't poison the caller's transaction or lose
            # the whole batch's history.
            try:
                async with db.begin_nested():
                    await ver_repo.create_version({
                        "test_id": r.test_id,
                        "change_reason": f"{reason_prefix}:{batch_id}",
                        "gherkin_snapshot": gherkin,
                        "script_snapshot": canonical,
                        "metadata_snapshot": metadata,
                        "row_snapshot": row_scaffold,
                        "changed_by": "force_regen" if reason_prefix == "pre_regen" else "arta-revert",
                    })
                made += 1
            except Exception:
                log.error("snapshot_test_cases: FAILED to version test_id=%s batch=%s "
                          "(history NOT saved for this test)", r.test_id, batch_id, exc_info=True)
        return made
    except Exception:
        # Fail-open (never block regen), but LOUD — this is history loss of the
        # very data the feature protects. Name the affected ids so the operator
        # can recover them from disk/.r215.bak if needed.
        log.error("snapshot_test_cases FAILED — history NOT saved (unrecoverable) "
                  "batch=%s test_ids=%s", batch_id, test_ids, exc_info=True)
        return 0


def _resolve_under_automation(script_path: str) -> Path | None:
    """Resolve a (possibly relative) DB-sourced script_path and return it ONLY if
    it stays under src/automation/ — else None (a DB-sourced path must not enable
    traversal writes). Security boundary for revert's disk write."""
    p = Path(script_path)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    rp = p.resolve()
    root = str(_AUTOMATION_ROOT)
    if str(rp) == root or str(rp).startswith(root + os.sep):
        return rp
    return None


async def apply_version_to_test_case(db, test_id: str, target, *,
                                     changed_by: str = "arta-revert") -> dict:
    """Atomic + REVERSIBLE restore of a test case to a snapshot's content:
    (1) snapshot the CURRENT row first (pre_revert) so the revert is itself
    undoable — today the state you revert away from is lost; (2) DB
    gherkin/script/metadata; (3) the executable disk file at script_path
    (traversal-guarded under src/automation); (4) the .arta/generated_tests.json
    sidecar. Each side effect is fail-open + logged. Reused by per-test revert
    AND bulk restore. Returns {applied, disk, sidecar}."""
    from ...db.repository import TestCaseRepo
    tc_repo = TestCaseRepo(db)

    # (1) reversibility — snapshot current before we overwrite it (always).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    await snapshot_test_cases(db, [test_id], batch_id=stamp,
                              reason_prefix="pre_revert", hash_gate=False)

    cur = (await db.execute(_sql(
        "SELECT script_path FROM test_cases WHERE test_id = :tid"), {"tid": test_id})).first()

    gherkin = target.gherkin_snapshot
    script = target.script_snapshot
    metadata = getattr(target, "metadata_snapshot", None)
    scaffold = getattr(target, "row_snapshot", None) or {}

    status = {"applied": [], "disk": False, "sidecar": False, "resurrected": False}

    if cur is not None:
        # (2a) row still exists → restore its content in place.
        script_path = cur.script_path
        updates: dict = {}
        if gherkin:
            updates["gherkin_scenario"] = gherkin
        if script:
            updates["script_content"] = script
        if isinstance(metadata, dict):
            updates["metadata_"] = metadata
        if updates:
            await tc_repo.update(test_id, updates)
        status["applied"] = list(updates.keys())
    elif scaffold:
        # (2b) row was DELETED by a regen → RESURRECT it from the scaffold. This
        # is what makes "undo my force-generate" restore the WHOLE prior gen, not
        # just the tests that still exist.
        script_path = scaffold.get("script_path")
        _ins = await db.execute(_sql("""
            INSERT INTO test_cases (test_id, title, gherkin_scenario, script_content,
                script_path, automation_tool, priority, test_type, is_automated, is_flaky,
                requirement_id, ac_id, project_id, metadata, tags)
            VALUES (:test_id, :title, :gherkin, :script, :script_path,
                CAST(:tool AS automation_tool), CAST(:priority AS risk_priority),
                CAST(:test_type AS test_type), :is_automated, :is_flaky,
                CAST(NULLIF(:requirement_id,'') AS uuid), CAST(NULLIF(:ac_id,'') AS uuid),
                CAST(NULLIF(:project_id,'') AS uuid), CAST(:metadata AS jsonb), :tags)
            ON CONFLICT (test_id) DO NOTHING
        """), {
            "test_id": test_id, "title": scaffold.get("title") or test_id,
            "gherkin": gherkin or "", "script": script or "", "script_path": script_path,
            "tool": scaffold.get("automation_tool") or "playwright",
            "priority": scaffold.get("priority") or "P2",
            "test_type": scaffold.get("test_type") or "integration",
            # preserve automation/flaky state so a manual (non-automated) test is
            # not resurrected as automated (default True only for pre-015 scaffolds).
            "is_automated": True if scaffold.get("is_automated") is None else bool(scaffold.get("is_automated")),
            "is_flaky": bool(scaffold.get("is_flaky")),
            "requirement_id": scaffold.get("requirement_id") or "",
            "ac_id": scaffold.get("ac_id") or "",
            "project_id": scaffold.get("project_id") or "",
            "metadata": json.dumps(metadata if isinstance(metadata, dict) else {}),
            "tags": scaffold.get("tags") or [],
        })
        if _ins.rowcount == 1:
            status["applied"] = ["resurrected"]
            status["resurrected"] = True
        else:
            # a concurrent regen re-created the row between our existence check
            # and the INSERT → ON CONFLICT DO NOTHING skipped; do NOT claim success.
            status["applied"] = ["already_exists"]
            log.warning("revert: %s already re-created concurrently — resurrect skipped", test_id)
    else:
        # deleted AND no scaffold (pre-migration snapshot) — content preserved in
        # history but the row can't be rebuilt. Honest no-op.
        log.warning("revert: %s was deleted and has no row_snapshot — cannot resurrect", test_id)
        script_path = None

    # (3) disk (canonical/executable) — guarded against path traversal.
    if script and script_path:
        try:
            rp = _resolve_under_automation(script_path)
            if rp is not None:
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text(script, encoding="utf-8")
                status["disk"] = True
            else:
                log.warning("revert disk write REFUSED (escapes src/automation): %s", script_path)
        except Exception as exc:
            log.warning("revert disk write failed test=%s: %s", test_id, exc)

    # (4) sidecar (.arta/generated_tests.json) — best-effort.
    try:
        from .tests_state import GENERATED_TESTS
        from .tests import _save_tests_json
        for t in GENERATED_TESTS:
            if str(t.get("id", "")).upper() == test_id.upper():
                if script:
                    t["script_content"] = script
                if gherkin:
                    t["gherkin"] = gherkin
                    t["gherkin_scenario"] = gherkin
                status["sidecar"] = True
        _save_tests_json()
    except Exception as exc:
        log.debug("revert sidecar sync skipped test=%s: %s", test_id, exc)

    return status


class VersionCreateRequest(BaseModel):
    change_reason: str = ""
    gherkin_snapshot: str = ""
    script_snapshot: str = ""


class RollbackRequest(BaseModel):
    """F3-2: Body for POST /tests/{test_id}/rollback.

    Resolution priority: to_version (explicit) > to_trace_id (find by stamped trace) > one-back.
    """
    to_version: int | None = None
    to_trace_id: str | None = None


@versions_router.get("/{test_id}/versions", dependencies=[Depends(_require_api_key)])
async def list_versions(test_id: str):
    """List version history for a test case."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseVersionRepo, _to_dict
            repo = TestCaseVersionRepo(db)
            rows = await repo.list_versions(test_id.upper())
            versions = [_to_dict(r) for r in rows]
            return {"test_id": test_id.upper(), "versions": versions, "total": len(versions)}

    versions = MOCK_VERSIONS.get(test_id.upper(), [])
    return {"test_id": test_id.upper(), "versions": versions, "total": len(versions)}


@versions_router.get("/{test_id}/versions/{v1}/diff/{v2}", dependencies=[Depends(_require_api_key)])
async def version_diff(test_id: str, v1: int, v2: int):
    """Return a unified diff of Gherkin between two versions."""
    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseVersionRepo
            repo = TestCaseVersionRepo(db)
            ver_old = await repo.get_version(test_id.upper(), v2)
            ver_new = await repo.get_version(test_id.upper(), v1)
            old_text = (ver_old.gherkin_snapshot or "") if ver_old else ""
            new_text = (ver_new.gherkin_snapshot or "") if ver_new else ""
            diff = list(difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"v{v2}", tofile=f"v{v1}", lineterm="",
            ))
            return {"test_id": test_id.upper(), "v1": v1, "v2": v2, "diff": "".join(diff)}

    versions = MOCK_VERSIONS.get(test_id.upper(), [])
    vmap = {v["version"]: v for v in versions}
    old = vmap.get(v2, {}).get("gherkin", "")
    new = vmap.get(v1, {}).get("gherkin", "")
    diff = list(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"v{v2}",
        tofile=f"v{v1}",
        lineterm="",
    ))
    return {"test_id": test_id.upper(), "v1": v1, "v2": v2, "diff": "".join(diff)}


@versions_router.post("/{test_id}/versions", dependencies=[Depends(_require_api_key)])
async def create_version(test_id: str, body: VersionCreateRequest):
    """Create a new version snapshot for a test case."""
    from ..db_adapter import try_db

    tid = test_id.upper()
    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseVersionRepo, _to_dict
            repo = TestCaseVersionRepo(db)
            ver = await repo.create_version({
                "test_id": tid,
                "change_reason": body.change_reason,
                "gherkin_snapshot": body.gherkin_snapshot,
                "script_snapshot": body.script_snapshot,
                "changed_by": "arta-agent",
            })
            return _to_dict(ver)

    # Mock fallback: append to in-memory versions
    from datetime import date
    versions = MOCK_VERSIONS.setdefault(tid, [])
    new_ver = max((v["version"] for v in versions), default=0) + 1
    entry = {
        "version": new_ver, "date": str(date.today()),
        "reason": body.change_reason, "by": "arta-agent",
        "gherkin": body.gherkin_snapshot,
    }
    versions.insert(0, entry)
    return entry


@versions_router.post("/{test_id}/rollback", dependencies=[Depends(_require_api_key)])
async def rollback_test(test_id: str, body: RollbackRequest | None = None):
    """F3-2: Roll back a test case.

    Default behaviour (no body): restore the most recent prior version.
    With `to_version=N`: restore that exact version.
    With `to_trace_id=<uuid>`: locate the version whose change_reason contains the trace_id.

    Returns 404 when no rollback target exists (fewer than 2 versions and no trace match).
    """
    from ..db_adapter import try_db
    body = body or RollbackRequest()
    tid = test_id.upper()

    # try_db context manager swallows in-block exceptions — collect outcome here
    # and raise/return AFTER the block exits cleanly.
    error: tuple[int, str] | None = None
    payload: dict | None = None

    async with try_db() as db:
        if not db:
            error = (503, "Database unavailable — rollback requires version history")
        else:
            from ...db.repository import TestCaseVersionRepo, TestCaseRepo, _to_dict
            ver_repo = TestCaseVersionRepo(db)
            tc_repo = TestCaseRepo(db)

            versions = list(await ver_repo.list_versions(tid))  # desc by version
            if not versions:
                error = (404, f"No version history for {tid} — nothing to roll back to")
            else:
                target = None
                if body.to_version is not None:
                    target = next((v for v in versions if v.version == body.to_version), None)
                    if not target:
                        error = (404, f"Version {body.to_version} not found for {tid}")
                elif body.to_trace_id:
                    needle = body.to_trace_id.strip()
                    target = next((v for v in versions if v.change_reason and needle in v.change_reason), None)
                    if not target:
                        error = (404, f"No version stamped with trace_id={needle[:8]} for {tid}")
                else:
                    # One-back: skip the current (latest) version, return the next one.
                    if len(versions) < 2:
                        error = (409, f"{tid} only has 1 version — no prior state to restore")
                    else:
                        target = versions[1]

                if target and not error:
                    # Atomic + reversible restore: DB + disk + sidecar + metadata.
                    status = await apply_version_to_test_case(db, tid, target)

                    new_ver = await ver_repo.create_version({
                        "test_id": tid,
                        "change_reason": (
                            f"Rollback to v{target.version}"
                            + (f" (trace {body.to_trace_id[:8]})" if body.to_trace_id else "")
                        ),
                        "gherkin_snapshot": target.gherkin_snapshot or "",
                        "script_snapshot": target.script_snapshot or "",
                        "metadata_snapshot": getattr(target, "metadata_snapshot", None),
                        "row_snapshot": getattr(target, "row_snapshot", None),
                        "changed_by": "arta-rollback",
                    })

                    payload = {
                        "test_id": tid,
                        "rolled_back_to_version": target.version,
                        "new_version": _to_dict(new_ver),
                        "applied": status.get("applied", []),
                        "restored": status,
                    }

    if error:
        raise HTTPException(error[0], error[1])
    return payload


@versions_router.post("/{test_id}/versions/{version}/revert", dependencies=[Depends(_require_api_key)])
async def revert_to_version(test_id: str, version: int):
    """Revert a test case to a previous version."""
    from ..db_adapter import try_db

    tid = test_id.upper()
    async with try_db() as db:
        if db:
            from ...db.repository import TestCaseVersionRepo, TestCaseRepo, _to_dict
            ver_repo = TestCaseVersionRepo(db)
            tc_repo = TestCaseRepo(db)

            # Get target version
            target = await ver_repo.get_version(tid, version)
            if not target:
                raise HTTPException(404, f"Version {version} not found for {tid}")

            # Atomic + reversible restore: DB + disk + sidecar + metadata.
            status = await apply_version_to_test_case(db, tid, target)

            # Create new version entry recording the revert
            new_ver = await ver_repo.create_version({
                "test_id": tid,
                "change_reason": f"Reverted to version {version}",
                "gherkin_snapshot": target.gherkin_snapshot or "",
                "script_snapshot": target.script_snapshot or "",
                "metadata_snapshot": getattr(target, "metadata_snapshot", None),
                "row_snapshot": getattr(target, "row_snapshot", None),
                "changed_by": "arta-agent",
            })

            return {
                "reverted_to": version,
                "new_version": _to_dict(new_ver),
                "test_id": tid,
                "restored": status,
            }

    # Mock fallback
    versions = MOCK_VERSIONS.get(tid, [])
    target = next((v for v in versions if v.get("version") == version), None)
    if not target:
        raise HTTPException(404, f"Version {version} not found for {tid}")
    from datetime import date
    new_ver_num = max((v["version"] for v in versions), default=0) + 1
    entry = {
        "version": new_ver_num,
        "date": str(date.today()),
        "reason": f"Reverted to version {version}",
        "by": "arta-agent",
        "gherkin": target.get("gherkin", ""),
    }
    versions.insert(0, entry)
    return {"reverted_to": version, "new_version": entry, "test_id": tid}


@versions_router.post("/requirements/{req_id}/restore-previous-generation",
                      dependencies=[Depends(_require_api_key)])
async def restore_previous_generation(req_id: str):
    """One-click "undo my force-generate": restore EVERY test case of a
    requirement to the state captured just before the LAST force-regen (its most
    recent `pre_regen:` snapshot). Reuses the same atomic revert helper as the
    per-test path (DB + disk + sidecar + metadata). Fail-open per test — one
    unrevertable test does not abort the batch (reported in `skipped`)."""
    from ..db_adapter import try_db

    req = req_id.upper()
    # Reject LIKE metacharacters (%/_) and stray chars — a crafted req_id like
    # "REQ-%" would otherwise match every test and restore the globally-latest
    if not re.match(r"^[A-Z0-9][A-Z0-9-]*$", req):
        raise HTTPException(400, f"Invalid requirement id {req_id!r} — expected e.g. REQ-XY-016")
    async with try_db() as db:
        if not db:
            raise HTTPException(503, "Database unavailable — restore requires version history")
        from ...db.repository import TestCaseVersionRepo
        ver_repo = TestCaseVersionRepo(db)

        # Identify the LATEST force-regen batch for this requirement — the
        # pre_regen snapshots it wrote ARE "the generation before the last
        # force-generate". Scope by test_id shape (version rows carry no
        # requirement FK). Enumerating from the VERSION table (not current
        # test_cases) is what lets us resurrect tests a regen deleted outright.
        tc_pat = req.replace("REQ-", "TC-")
        latest = (await db.execute(_sql(
            "SELECT change_reason FROM test_case_versions "
            "WHERE change_reason LIKE 'pre_regen:%' "
            "AND (test_id LIKE :p1 OR test_id LIKE :p2 OR test_id LIKE :p3) "
            "ORDER BY created_at DESC LIMIT 1"),
            {"p1": f"{tc_pat}-%", "p2": f"{req}-%", "p3": f"{req}%"})).first()
        if not latest:
            raise HTTPException(404, f"No prior-generation snapshot for requirement {req}")
        batch = latest.change_reason  # "pre_regen:<job_id>"
        tid_rows = (await db.execute(_sql(
            "SELECT DISTINCT test_id FROM test_case_versions WHERE change_reason = :batch"),
            {"batch": batch})).all()
        test_ids = sorted({r.test_id for r in tid_rows})

        reverted, skipped = [], []
        for tid in test_ids:
            try:
                versions = await ver_repo.list_versions(tid)  # desc by version
                # the snapshot THIS batch captured for this test (its prior state)
                target = next((v for v in versions if v.change_reason == batch), None)
                if not target:
                    skipped.append({"test_id": tid, "reason": "no snapshot in batch"})
                    continue
                # H1 — per-test SAVEPOINT: under Postgres the first failing
                # statement poisons the whole transaction. Without this, one bad
                # test would make every later iteration fail AND the final commit
                # roll back the tests that already succeeded. begin_nested() rolls
                # back ONLY this test on failure, so the loop truly continues.
                async with db.begin_nested():
                    status = await apply_version_to_test_case(db, tid, target)
                    await ver_repo.create_version({
                        "test_id": tid,
                        "change_reason": f"Restored previous generation (v{target.version})",
                        "gherkin_snapshot": target.gherkin_snapshot or "",
                        "script_snapshot": target.script_snapshot or "",
                        "metadata_snapshot": getattr(target, "metadata_snapshot", None),
                        "row_snapshot": getattr(target, "row_snapshot", None),
                        "changed_by": "arta-restore",
                    })
                reverted.append({"test_id": tid, "to_version": target.version, "restored": status})
            except Exception as exc:
                log.warning("restore-previous-generation: %s skipped: %s", tid, exc)
                skipped.append({"test_id": tid, "reason": f"{type(exc).__name__}: {exc}"})

        if not test_ids:
            raise HTTPException(404, f"No test cases found for requirement {req}")
        resurrected = sum(1 for r in reverted if (r.get("restored") or {}).get("resurrected"))
        return {"requirement_id": req, "batch": batch, "reverted": len(reverted),
                "resurrected": resurrected, "skipped": len(skipped),
                "reverted_tests": reverted, "skipped_tests": skipped}
