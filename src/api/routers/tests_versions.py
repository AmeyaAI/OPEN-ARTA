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

State (MOCK_VERSIONS) is shared via tests_state.py so cross-router code keeps
working.
"""
from __future__ import annotations

import difflib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..dependencies import require_api_key as _require_api_key
from .tests_state import MOCK_VERSIONS


versions_router = APIRouter()


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
                    updates: dict = {}
                    if target.gherkin_snapshot:
                        updates["gherkin_scenario"] = target.gherkin_snapshot
                    if target.script_snapshot:
                        updates["script_content"] = target.script_snapshot
                    if updates:
                        await tc_repo.update(tid, updates)

                    new_ver = await ver_repo.create_version({
                        "test_id": tid,
                        "change_reason": (
                            f"Rollback to v{target.version}"
                            + (f" (trace {body.to_trace_id[:8]})" if body.to_trace_id else "")
                        ),
                        "gherkin_snapshot": target.gherkin_snapshot or "",
                        "script_snapshot": target.script_snapshot or "",
                        "changed_by": "arta-rollback",
                    })

                    payload = {
                        "test_id": tid,
                        "rolled_back_to_version": target.version,
                        "new_version": _to_dict(new_ver),
                        "applied": list(updates.keys()),
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

            # Update test case with version's content
            updates: dict = {}
            if target.gherkin_snapshot:
                updates["gherkin_scenario"] = target.gherkin_snapshot
            if target.script_snapshot:
                updates["script_content"] = target.script_snapshot
            if updates:
                await tc_repo.update(tid, updates)

            # Create new version entry recording the revert
            new_ver = await ver_repo.create_version({
                "test_id": tid,
                "change_reason": f"Reverted to version {version}",
                "gherkin_snapshot": target.gherkin_snapshot or "",
                "script_snapshot": target.script_snapshot or "",
                "changed_by": "arta-agent",
            })

            return {
                "reverted_to": version,
                "new_version": _to_dict(new_ver),
                "test_id": tid,
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
