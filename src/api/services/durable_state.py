"""R32 — Durable state store (Redis with in-memory fallback).

ARTA's three load-bearing module-level dicts (`_REAL_RUNS`,
`PENDING_APPROVALS`, `_SPEC_DRIFT_TARGETS`) survived a single-worker
dev environment by accident. Production deployments running Gunicorn
with multiple workers see inconsistent state across workers (each
worker has its own copy). Container restarts wipe the state entirely.

This module provides Redis-backed CRUD for those three structures with
an in-memory fallback when Redis is unavailable. Existing module-level
dicts in the agents stay as a write-through cache so reads don't hit
Redis on the hot path; writes go to BOTH the dict AND Redis.

Why a single module: keeps Redis-key naming (``arta:run:{id}``,
``arta:approvals:{pid}``, ``arta:spec_drift:{pid}``) and the fallback
contract centralized. Each agent imports `get_run`, `set_run`, etc.
without learning Redis primitives.

Graceful degradation: when Redis is down, falls back to in-memory dicts
for the duration of the process. Stamps `degraded=True` on writes so
operators can surface the volatile state. Existing Phase 5.3 stub-mode
pattern.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("arta.durable_state")

# In-memory fallbacks. Used when Redis is unreachable AND as a
# write-through cache so hot-path reads don't round-trip Redis.
_RUN_FALLBACK: dict[str, dict] = {}
_APPROVALS_FALLBACK: dict[str, list[dict]] = {}
_SPEC_DRIFT_FALLBACK: dict[str, set[str]] = {}

_RUN_TTL_SECONDS = 30 * 24 * 3600   # 30 days — matches artifact retention
_APPROVAL_TTL_SECONDS = 14 * 24 * 3600   # 14 days — operator window


def _redis_client() -> Any:
    """Return the app-level Redis client, or None if unavailable.

    Uses `app.state.redis` populated by main.py at startup. Falls back
    to None when Redis init failed; callers handle that path gracefully.
    """
    try:
        from ..main import app as _app
        return getattr(_app.state, "redis", None)
    except Exception:
        return None


# ─── _REAL_RUNS — per-run state (R32.1) ─────────────────────────────────────


async def set_run(run_id: str, payload: dict) -> dict:
    """Write the run-state hash. Always writes to in-memory fallback;
    also writes to Redis when reachable (degraded=False) else stamps
    degraded=True on the returned echo.
    """
    if not run_id:
        return {"degraded": True, "reason": "no_run_id"}
    _RUN_FALLBACK[run_id] = dict(payload)
    client = _redis_client()
    if client is None:
        return {"degraded": True, "reason": "redis_unavailable"}
    try:
        # Store as JSON blob in a single key (simpler than HSET-per-field;
        # the payload contains nested lists/dicts that don't fit Redis
        # hash's flat string-only contract).
        await client.set(
            f"arta:run:{run_id}",
            json.dumps(payload, default=str),
            ex=_RUN_TTL_SECONDS,
        )
        return {"degraded": False}
    except Exception as exc:
        log.debug("R32.1: redis SET failed for run %s: %s", run_id, exc)
        return {"degraded": True, "reason": f"redis_error:{type(exc).__name__}"}


async def get_run(run_id: str) -> dict | None:
    """Read run-state. Prefers in-memory cache (hot-path); falls back
    to Redis when cache misses (e.g., post-restart re-read).
    """
    if not run_id:
        return None
    cached = _RUN_FALLBACK.get(run_id)
    if cached is not None:
        return cached
    client = _redis_client()
    if client is None:
        return None
    try:
        raw = await client.get(f"arta:run:{run_id}")
        if not raw:
            return None
        payload = json.loads(raw)
        _RUN_FALLBACK[run_id] = payload   # warm cache for next read
        return payload
    except Exception as exc:
        log.debug("R32.1: redis GET failed for run %s: %s", run_id, exc)
        return None


# ─── PENDING_APPROVALS — heal proposals (R32.2) ─────────────────────────────


async def queue_approval(project_id: str, proposal: dict) -> dict:
    """Append a heal proposal to the project's queue. Survives restart
    when Redis is reachable.
    """
    pid = project_id or "_default"
    _APPROVALS_FALLBACK.setdefault(pid, []).append(dict(proposal))
    client = _redis_client()
    if client is None:
        return {"degraded": True, "reason": "redis_unavailable"}
    try:
        await client.rpush(
            f"arta:approvals:{pid}",
            json.dumps(proposal, default=str),
        )
        await client.expire(f"arta:approvals:{pid}", _APPROVAL_TTL_SECONDS)
        return {"degraded": False}
    except Exception as exc:
        log.debug("R32.2: redis RPUSH failed for project %s: %s", pid, exc)
        return {"degraded": True, "reason": f"redis_error:{type(exc).__name__}"}


async def list_approvals(project_id: str) -> list[dict]:
    """Read all queued heal proposals for a project. Falls back to
    in-memory list when Redis is down.
    """
    pid = project_id or "_default"
    client = _redis_client()
    if client is None:
        return list(_APPROVALS_FALLBACK.get(pid) or [])
    try:
        raws = await client.lrange(f"arta:approvals:{pid}", 0, -1) or []
        out: list[dict] = []
        for r in raws:
            try:
                out.append(json.loads(r))
            except Exception:
                continue
        # Hydrate in-memory cache so subsequent reads don't hit Redis.
        if out:
            _APPROVALS_FALLBACK[pid] = out
        return out
    except Exception as exc:
        log.debug("R32.2: redis LRANGE failed for project %s: %s", pid, exc)
        return list(_APPROVALS_FALLBACK.get(pid) or [])


# ─── _SPEC_DRIFT_TARGETS — per-project drift accumulator (R32.3) ────────────


async def add_spec_drift(project_id: str, targets: list[str]) -> dict:
    """Add drift targets to the project's set. Idempotent (set semantics).
    Replaces R29.5's module-level dict mutation.
    """
    pid = project_id or "_default"
    if not targets:
        return {"degraded": False}
    bucket = _SPEC_DRIFT_FALLBACK.setdefault(pid, set())
    bucket.update(targets)
    client = _redis_client()
    if client is None:
        return {"degraded": True, "reason": "redis_unavailable"}
    try:
        await client.sadd(f"arta:spec_drift:{pid}", *targets)
        return {"degraded": False}
    except Exception as exc:
        log.debug("R32.3: redis SADD failed for project %s: %s", pid, exc)
        return {"degraded": True, "reason": f"redis_error:{type(exc).__name__}"}


async def list_spec_drift(project_id: str) -> set[str]:
    """Return the union of in-memory + Redis drift targets for a
    project. Read-side prefers Redis (multi-worker correctness) but
    falls back to the in-memory set when Redis is down.
    """
    pid = project_id or "_default"
    client = _redis_client()
    if client is None:
        return set(_SPEC_DRIFT_FALLBACK.get(pid) or set())
    try:
        members = await client.smembers(f"arta:spec_drift:{pid}")
        if isinstance(members, set):
            return {str(m) for m in members}
        return set(members or [])
    except Exception as exc:
        log.debug("R32.3: redis SMEMBERS failed for project %s: %s", pid, exc)
        return set(_SPEC_DRIFT_FALLBACK.get(pid) or set())


async def clear_spec_drift(project_id: str) -> int:
    """Reset drift accumulator for a project. Called from
    `post_discovery_regen_pass` (R30.7-C) when discovery refreshes the
    catalog — prior targets are stale.
    """
    pid = project_id or "_default"
    cleared = len(_SPEC_DRIFT_FALLBACK.get(pid) or set())
    _SPEC_DRIFT_FALLBACK.pop(pid, None)
    client = _redis_client()
    if client is not None:
        try:
            await client.delete(f"arta:spec_drift:{pid}")
        except Exception as exc:
            log.debug("R32.3: redis DEL failed for project %s: %s", pid, exc)
    return cleared


# ─── Health check (R32.4) ───────────────────────────────────────────────────


async def health() -> dict:
    """Return whether Redis is reachable for the durable state. Used
    by the gate / dashboard to render a `degraded` banner when Redis
    is down — operators see "state will not survive restart".
    """
    client = _redis_client()
    if client is None:
        return {"redis": "unavailable", "degraded": True}
    try:
        pong = await client.ping()
        return {"redis": "ok" if pong else "unhealthy", "degraded": not pong}
    except Exception as exc:
        return {"redis": f"error:{type(exc).__name__}", "degraded": True}


__all__ = [
    "set_run", "get_run",
    "queue_approval", "list_approvals",
    "add_spec_drift", "list_spec_drift", "clear_spec_drift",
    "health",
]
