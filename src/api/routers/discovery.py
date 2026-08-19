"""Phase H — discovery / chain / step API surface.

Exposes the new Phase B-G data structures to the frontend:

  GET  /api/discovery/projects/{project_id}/summary
       — Discovery panel overview (last harvest stats, env-vars filled, drift)

  GET  /api/discovery/projects/{project_id}/chains
       — Captured chains for the project (Phase C — used by Chain View DAG)

  GET  /api/discovery/projects/{project_id}/endpoints
       — Captured endpoints with request/response shapes

  GET  /api/discovery/runs/{run_id}/steps
       — Per-step execution timeline (Phase E — used by Run-detail timeline)

  POST /api/discovery/refresh
       — Trigger an on-demand discovery run (Phase B1 on-demand path)

All endpoints are read-only except `/refresh` and tolerate missing
discovery data gracefully (empty array / 404).
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

log = logging.getLogger("arta.api.discovery")
from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
@router.get("/projects/{project_id}/summary")
async def discovery_summary(project_id: str) -> dict:
    """Discovery panel summary for the admin page.

    Returns last_discovery_at, envvars_harvested_count, captured_endpoint_count,
    captured_chain_count, multi_value_warnings (from the most recent run),
    discovery_pending flag.

    Falls back to inspecting `.arta/discovered_*` JSON sidecars when the
    DB row is missing — the operator can tell if the discovery_executor
    ever ran for this project.
    """
    from ...agents import api_discovery as ad
    try:
        from ...agents import discovery_settings as ds
    except ImportError:
        ds = None  # type: ignore

    summary: dict[str, Any] = {
        "project_id": project_id,
        "last_discovery_at": None,
        "envvars_harvested_count": 0,
        "captured_endpoint_count": 0,
        "captured_chain_count": 0,
        "discovery_pending": False,
        "stage_2_5_enabled": False,
    }

    # 1. Project-level settings via DB.
    try:
        from ..db_adapter import try_db
        from ...db import models
        import sqlalchemy as sa
        # try_db() is async context manager (matches post_run_chain_pipeline
        # + exploratory pattern). Pre-existing bug: this site used
        # async-for-as-generator which raises TypeError at runtime.
        async with try_db() as session:   # type: ignore
            if session is not None:
                stmt = sa.select(models.Project).where(models.Project.id == project_id)
                result = await session.execute(stmt)
                project = result.scalar_one_or_none()
                if project is not None and ds is not None:
                    settings = getattr(project, "discovery_settings", None) or {}
                    summary["last_discovery_at"] = ds.get(settings, ds.KEY_LAST_DISCOVERY_AT)
                    summary["envvars_harvested_count"] = ds.get(settings, ds.KEY_ENVVARS_HARVESTED_COUNT)
                    summary["discovery_pending"] = ds.is_discovery_pending(settings)
                    summary["stage_2_5_enabled"] = ds.is_stage_2_5_enabled(settings)
                    summary["is_api_only"] = bool(getattr(project, "is_api_only", False))
    except Exception as exc:
        log.debug("discovery_summary DB read failed: %s", exc)

    # 2. Sidecar JSON inspection — works even when DB row is incomplete.
    try:
        endpoints = ad._load_captured_endpoints(project_id)   # type: ignore
        summary["captured_endpoint_count"] = len(endpoints)
    except Exception as exc:
        log.debug("captured_endpoint count failed: %s", exc)
    try:
        chains = ad.load_chains(project_id)
        summary["captured_chain_count"] = len(chains)
    except Exception as exc:
        log.debug("captured_chain count failed: %s", exc)

    return summary


@router.get("/projects/{project_id}/discovery-status")
async def discovery_status(project_id: str, environment: str = "staging") -> dict:
    """R39.3 — operator-facing auth-state + harvest diagnostic.

    Returns a single source-of-truth payload aggregating the signals
    needed to decide whether the operator can run a successful pipeline:
      - cookie_status: redacted_placeholder | set | missing
      - storage_state_file_present: bool (`.arta/environments/{env}-storage.json`)
      - auth_state_present: True iff cookie OR storage state usable
      - last_har_mtime / auth_failed_flag_present: most recent harvest signal
      - unfilled_count / unfilled_vars: which vars still need values

    The frontend (R39.2 RunPipelineModal banner, Settings → Discovery
    panel) calls this single endpoint instead of probing 4 different
    backend signals individually.
    """
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    from .projects import _PROJECTS as _PROJECTS_REF

    project = _PROJECTS_REF.get(project_id) or {}
    # R39.3 fix — match the gate's `_select_env_block` semantics so the
    # frontend banner sees the same env_block the backend's R36.2 gate
    # for an env named `staging`; the endpoint reported auth_state_present
    # = true (from a stale storage state file) AND unfilled_count = 0
    # while the gate suffix-matched to `staging`'s 22 unfilled vars and
    # 409'd dispatch. The R40.4 banner never rendered because the
    # endpoint disagreed with reality.
    try:
        from ...agents.auth_refresher import _select_env_block as _sel_env
        resolved_env_name, env_block = _sel_env(project, environment)
    except Exception:
        resolved_env_name = environment
        env_block = (project.get("environments") or {}).get(environment) or {}
    if hasattr(env_block, "model_dump"):
        env_block = env_block.model_dump()
    env_block = env_block or {}

    creds = (env_block.get("auth") or {}).get("credentials") or {}
    cookie_value = creds.get("cookie_value")
    bearer_value = creds.get("bearer_token")
    placeholders = ("***", "REDACTED", "REPLACE_ME", "")

    def _classify(v: Any) -> str:
        if v is None:
            return "missing"
        if isinstance(v, str) and v in placeholders:
            return "redacted_placeholder"
        return "set"

    cookie_status = _classify(cookie_value)
    bearer_status = _classify(bearer_value)

    # R39.3 fix — try BOTH the operator's selected env name and the
    # resolved env name (suffix match). storage state files are keyed
    # by the env that issued them — `_select_env_block` may resolve
    # either name.
    storage_path = _Path(f".arta/environments/{environment}-storage.json")
    if not storage_path.is_file() and resolved_env_name and resolved_env_name != environment:
        alt = _Path(f".arta/environments/{resolved_env_name}-storage.json")
        if alt.is_file():
            storage_path = alt
    storage_exists = storage_path.is_file()
    storage_mtime = storage_path.stat().st_mtime if storage_exists else None

    har_dir = _Path(".arta/discovery") / project_id
    auth_flag_path = next(
        (p for p in (har_dir.rglob("auth_failed.flag") if har_dir.is_dir() else [])),
        None,
    )
    auth_flag_diagnosis: dict | None = None
    if auth_flag_path is not None:
        try:
            auth_flag_diagnosis = _json.loads(auth_flag_path.read_text())
        except Exception:
            auth_flag_diagnosis = {"reason": "unreadable_flag"}

    har_path = next(
        (p for p in (har_dir.rglob("discovery.har") if har_dir.is_dir() else [])),
        None,
    )
    last_har_mtime = har_path.stat().st_mtime if har_path else None

    vars_dict = env_block.get("variables") or {}
    unfilled = [
        k for k, v in vars_dict.items()
        if isinstance(v, str) and v in placeholders
    ]
    filled_count = sum(
        1 for v in vars_dict.values()
        if isinstance(v, str) and v not in placeholders and v
    )

    auth_state_present = (
        cookie_status == "set"
        or bearer_status == "set"
        or storage_exists
    )

    return {
        "project_id": project_id,
        "environment": environment,
        "auth_state_present": auth_state_present,
        "cookie_status": cookie_status,
        "bearer_status": bearer_status,
        "storage_state_file_present": storage_exists,
        "storage_state_path": str(storage_path),
        "storage_state_mtime": storage_mtime,
        "last_har_mtime": last_har_mtime,
        "auth_failed_flag_present": auth_flag_path is not None,
        "auth_failed_diagnosis": auth_flag_diagnosis,
        "unfilled_count": len(unfilled),
        "unfilled_vars": sorted(unfilled),
        "filled_count": filled_count,
    }


@router.get("/projects/{project_id}/auth-staleness")
async def auth_staleness(project_id: str, environment: str = "staging") -> dict:
    """R72.5 — pre-emptive auth-staleness signal.

    Decodes the project's stored cookie JWT and reports time-to-expiry
    so the operator gets advance notice BEFORE the cookie expires +
    discovery starts silently failing. The user's stated goal includes
    autonomous loop; this endpoint surfaces the manual touchpoint at
    the right time (6h advance) rather than after the fact.

    States:
      - fresh: >25% TTL remaining (no action needed)
      - stale_soon: ≤25% TTL but not yet expired (refresh in next ~6h)
      - expired: TTL elapsed (refresh required immediately)
      - unknown: no cookie / not a JWT / cannot decode

    Frontend polls this on every page load and renders:
      - fresh → no badge
      - stale_soon → amber "Auth refreshing soon (~{hours_remaining}h)"
      - expired → red "Auth expired — refresh required"
      - unknown → no badge (silent; covers cookie-less project configs)
    """
    import time as _time
    from pathlib import Path as _Path
    from .projects import _PROJECTS as _PROJECTS_REF

    project = _PROJECTS_REF.get(project_id) or {}
    try:
        from ...agents.auth_refresher import _select_env_block as _sel_env
        from ...agents.auth_refresher import _decode_jwt_payload as _decode_jwt
        resolved_env_name, env_block = _sel_env(project, environment)
    except Exception:
        resolved_env_name = environment
        env_block = (project.get("environments") or {}).get(environment) or {}
        _decode_jwt = None
    if hasattr(env_block, "model_dump"):
        env_block = env_block.model_dump()
    env_block = env_block or {}

    creds = (env_block.get("auth") or {}).get("credentials") or {}
    cookie_value = creds.get("cookie_value")
    placeholders = ("***", "REDACTED", "REPLACE_ME", "")

    # R306.C — auth-method-aware gate. The pasted cookie is the RUNTIME auth
    # credential only for cookie-auth SUTs. For bearer/api_key/token SUTs the
    # Bearer via refresh_token/api_key), so a stale DISCOVERY cookie does NOT
    # block pipelines. run-26aa5f proved it — 87 PASS + working destructive
    # Emitting a red "Auth cookie EXPIRED — pipelines will BLOCK" banner there
    # was a false alarm; suppress it so only genuinely cookie-dependent SUTs
    auth_method = str((env_block.get("auth") or {}).get("method") or "cookie").lower()
    pipeline_blocking = auth_method in ("cookie", "", "none")

    out: dict = {
        "project_id": project_id,
        "environment": environment,
        "resolved_env_name": resolved_env_name,
        "state": "unknown",
        "auth_method": auth_method,
        "pipeline_blocking": pipeline_blocking,
        "ttl_remaining_seconds": None,
        "ttl_remaining_hours": None,
        "ttl_pct_remaining": None,
        "expires_at_iso": None,
        "hint": None,
    }

    def _finalize(o: dict) -> dict:
        """R306.C — for non-cookie-auth SUTs, downgrade a cookie-derived
        expired/stale_soon to `unknown` (badge renders nothing) so the runtime
        pipeline's independence from the discovery cookie is reflected. The TTL
        fields + a truthful informational hint are preserved under
        `cookie_state` for a future discovery-status panel. Killswitch
        ARTA_R306_C_AUTH_METHOD_GATE_DISABLE=1 → pre-R306.C behavior."""
        if (not o.get("pipeline_blocking", True)
                and o.get("state") in ("expired", "stale_soon")
                and os.environ.get("ARTA_R306_C_AUTH_METHOD_GATE_DISABLE") != "1"):
            o["cookie_state"] = o["state"]
            o["state"] = "unknown"
            o["hint"] = (
                f"Discovery cookie {o.get('cookie_state')} — runtime auth is "
                f"'{o.get('auth_method')}' (self-minted / refreshed), so pipelines "
                "are unaffected. Re-paste only to refresh discovery DOM/endpoint "
                "capture."
            )
        return o

    if not cookie_value or (isinstance(cookie_value, str) and cookie_value in placeholders):
        out["hint"] = (
            "No cookie configured. Paste a fresh cookie via the Refresh "
            "Auth modal to enable the autonomous discovery loop."
        )
        return out

    payload = _decode_jwt(cookie_value) if _decode_jwt else None
    if not payload:
        # R75.2 — opaque (non-JWT) cookie fallback. Pre-R75.2 these
        # always returned state=unknown because there was no `exp` to
        # reason about. With R75.2's `last_paste_at` stamp + per-project
        # `ttl_hours` config, we can compute synthetic TTL:
        #   elapsed = now - last_paste_at
        #   ttl_remaining = ttl_hours - elapsed_hours
        # the JWT branch's iat-missing fallback).
        last_paste_at_str = creds.get("last_paste_at")
        ttl_hours_configured = float(creds.get("ttl_hours") or 24)
        if last_paste_at_str:
            try:
                from datetime import datetime as _dt_75_2
                last_paste = _dt_75_2.fromisoformat(last_paste_at_str.replace("Z", "+00:00"))
                elapsed_hours = (
                    _dt_75_2.now(last_paste.tzinfo).timestamp() - last_paste.timestamp()
                ) / 3600.0
                remaining_hours = ttl_hours_configured - elapsed_hours
                out["ttl_remaining_seconds"] = max(0, int(remaining_hours * 3600))
                out["ttl_remaining_hours"] = max(0.0, round(remaining_hours, 1))
                pct_remaining = max(0.0, 100.0 * remaining_hours / ttl_hours_configured)
                out["ttl_pct_remaining"] = round(pct_remaining, 1)
                out["expires_at_iso"] = (
                    _dt_75_2.fromtimestamp(last_paste.timestamp() + ttl_hours_configured * 3600)
                    .replace(tzinfo=last_paste.tzinfo).isoformat()
                )
                if remaining_hours <= 0:
                    out["state"] = "expired"
                    out["hint"] = (
                        f"Opaque cookie EXPIRED (pasted {int(elapsed_hours)}h ago, "
                        f"configured TTL {int(ttl_hours_configured)}h). Refresh now."
                    )
                elif pct_remaining <= 25.0:
                    out["state"] = "stale_soon"
                    out["hint"] = (
                        f"Opaque cookie expires in ~{out['ttl_remaining_hours']}h "
                        f"({out['ttl_pct_remaining']}% TTL remaining, "
                        f"configured TTL {int(ttl_hours_configured)}h). Refresh BEFORE "
                        "expiry to keep the autonomous loop running."
                    )
                else:
                    out["state"] = "fresh"
                return _finalize(out)
            except Exception as _r75_2_exc:
                log.debug("R75.2: opaque-cookie fallback failed: %s", _r75_2_exc)
        # No last_paste_at OR fallback failed — stay unknown.
        out["hint"] = (
            "Cookie present but not a decodable JWT AND no `last_paste_at` "
            "stamp on the project — cannot determine TTL. R75.2: re-paste "
            "the cookie via Refresh Auth to enable opaque-cookie staleness "
            "detection, OR set `auth.credentials.ttl_hours` if the cookie "
            "has a known TTL."
        )
        return out

    exp = payload.get("exp")
    iat = payload.get("iat")
    if not isinstance(exp, (int, float)):
        out["hint"] = "JWT has no `exp` claim — cannot determine TTL."
        return out

    now = _time.time()
    remaining = float(exp) - now
    out["ttl_remaining_seconds"] = max(0, int(remaining))
    out["ttl_remaining_hours"] = max(0.0, round(remaining / 3600.0, 1))
    out["expires_at_iso"] = (
        # ISO timestamp from epoch
        __import__("datetime").datetime.utcfromtimestamp(float(exp))
        .replace(tzinfo=__import__("datetime").timezone.utc).isoformat()
    )

    # TTL fraction needs the issuance timestamp to compute. If `iat` is
    # default) since we can't know the actual issuance time.
    if isinstance(iat, (int, float)) and iat > 0:
        total_ttl = float(exp) - float(iat)
    else:
        total_ttl = 24 * 3600.0  # default
    if total_ttl > 0:
        out["ttl_pct_remaining"] = round(100.0 * max(0, remaining) / total_ttl, 1)

    # Classify the state
    if remaining <= 0:
        out["state"] = "expired"
        out["hint"] = (
            "Auth cookie EXPIRED. Discovery probe will fail; runs that "
            "depend on auth (Playwright, Newman, k6) will BLOCK. Paste "
            "a fresh cookie immediately."
        )
    elif out["ttl_pct_remaining"] is not None and out["ttl_pct_remaining"] <= 25.0:
        out["state"] = "stale_soon"
        out["hint"] = (
            f"Auth cookie expires in ~{out['ttl_remaining_hours']}h "
            f"({out['ttl_pct_remaining']}% TTL remaining). Refresh "
            "BEFORE expiry to keep the autonomous loop running."
        )
    else:
        out["state"] = "fresh"
        out["hint"] = None

    return _finalize(out)


@router.get("/projects/{project_id}/grounding-coverage")
async def grounding_coverage(project_id: str) -> dict:
    """R330 (SUT-Understanding P1) — how well-grounded is ARTA's understanding of
    this SUT's API surface? Aggregates the captured-endpoint store by provenance
    (source_grounded / human_corrected / requirement_declared / observed) so the
    operator sees what ARTA actually KNOWS vs guessed. Also reports whether SOURCE
    grounding is even available (a github integration configured) — a low
    source-grounded share on a source-blocked SUT is expected and points to R320
    human correction as the remedy. Killswitch ARTA_GROUNDING_COVERAGE_DISABLE."""
    if os.environ.get("ARTA_GROUNDING_COVERAGE_DISABLE") == "1":
        return {"project_id": project_id, "disabled": True}
    from ...agents import api_discovery as ad
    from .projects import _PROJECTS as _PROJECTS_REF
    try:
        cov = ad.grounding_coverage(project_id)
    except Exception as exc:
        log.warning("grounding_coverage failed: %s", exc)
        cov = {"total_endpoints": 0, "by_provenance": {}, "by_source": {},
               "grounded_endpoints": 0, "grounded_pct": 0.0}
    # Per-TEST grounding (from the traceability store the gate persists): how many
    # generated tests are source_grounded/human/observed vs GUESS, + the count
    # flagged potentially_incorrect. Surfaces the previously-invisible flag.
    tests_block = {}
    try:
        from ...agents.traceability_gate import read_traceability
        rt = read_traceability(project_id)
        tests_block = {
            "test_count": rt.get("test_count", 0),
            "grounded_by": rt.get("grounded_by", {}),
            "potentially_incorrect_count": rt.get("potentially_incorrect_count", 0),
            "traceability_pct": rt.get("traceability_pct"),
            # R330 P1d — gen-time fail-loud statuses ("unavailable:no_github_token"
            # …) + the distinct needs-attention count (guess ∪ flagged; summing the
            # two double-counted, since a guess test is usually also flagged).
            "source_grounding": rt.get("source_grounding", {}),
            "needs_attention_count": rt.get("needs_attention_count",
                                            rt.get("potentially_incorrect_count", 0)),
            # Code→API spine: tests whose endpoints resolve to a real SUT source file.
            "source_component_count": rt.get("source_component_count", 0),
        }
    except Exception as exc:
        log.debug("grounding_coverage tests block skipped: %s", exc)
    # R330 P1d — is SOURCE grounding possible for this SUT? The integrations
    # schema is FLAT (IntegrationsInput: github_repo/github_token/repositories) —
    # the old nested integrations["github"] read matched nothing, so the banner
    # was structurally always "unavailable". Align with what gen actually
    # enforces (_fetch_sut_source_context): the token is the hard gate.
    project = _PROJECTS_REF.get(project_id) or {}
    integrations = project.get("integrations") or {}
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
    if not isinstance(integrations, dict):
        integrations = {}
    # be stale/EMPTY after a restart while gen reads the durable project record
    # — the panel said token_available=false while gen truthfully stamped
    # "available". Fall back to the DB integrations so the instrument agrees
    # with what gen actually does.
    if not (integrations.get("github_token") or integrations.get("github_repo")
            or integrations.get("repositories")):
        try:
            from ..db_adapter import try_db
            import sqlalchemy as _sa
            from ...db import models as _models
            async with try_db() as _db:
                if _db is not None:
                    _row = (await _db.execute(
                        _sa.select(_models.Project.integrations)
                        .where(_models.Project.id == project_id))).scalar()
                    if isinstance(_row, dict) and _row:
                        integrations = _row
        except Exception as _exc:
            log.debug("grounding_coverage: DB integrations fallback skipped: %s", _exc)
    repo_configured = bool(integrations.get("github_repo") or integrations.get("repositories"))
    token_available = bool(integrations.get("github_token") or os.environ.get("GITHUB_TOKEN"))
    source_available = token_available
    if source_available and not repo_configured:
        note = ("Source grounding token present but no repository configured — "
                "add the SUT repo(s) to the GitHub integration for targeted fetches.")
    elif source_available:
        note = ("Source grounding available — endpoints not source_grounded can be "
                "improved by re-discovery.")
    else:
        note = ("No source (GitHub) grounding configured/reachable for this SUT — human "
                "correction (Refine with AI) is the authoritative grounding source.")
    return {
        "project_id": project_id,
        **cov,
        "tests": tests_block,
        "source_grounding_available": source_available,
        "source_grounding": {"token_available": token_available,
                             "repo_configured": repo_configured},
        "note": note,
    }


@router.get("/projects/{project_id}/authz-model")
async def authz_model(project_id: str) -> dict:
    """SUT-Understanding — the derived AUTHORIZATION model summary (route catalog
    + permission catalog + principals + mechanism). Read-only; composes the
    already-persisted config-layer artifacts. Fail-open: `{built:false}` when no
    route-catalog model exists yet; `principal_count:0` when principals aren't
    seeded. Killswitch ARTA_AUTHZ_MODEL_ENDPOINT_DISABLE."""
    if os.environ.get("ARTA_AUTHZ_MODEL_ENDPOINT_DISABLE") == "1":
        return {"project_id": project_id, "disabled": True}
    from ...agents.authz_discovery import load_authz_model, load_authz_profile
    from ...agents.authz_catalog import load_permission_catalog
    from ...agents.authz_principals import summarize_principals
    model = load_authz_model(project_id)
    if not model or not model.get("operations"):
        return {"project_id": project_id, "built": False}
    profile = load_authz_profile(project_id)
    catalog = load_permission_catalog(project_id) or {}
    princ = summarize_principals(project_id, catalog)
    return {
        "project_id": project_id,
        "built": True,
        "operation_count": model.get("operation_count", 0),
        "summary": model.get("summary", {}),
        "role_count": len((catalog or {}).get("role_permissions") or {}),
        "principal_count": princ.get("principal_count", 0),
        "principal_by_type": princ.get("by_type", {}),
        "mechanism": (profile or {}).get("authz_mechanism", "rbac_scoped_catalog"),
    }


@router.get("/projects/{project_id}/chains")
async def list_chains(project_id: str, limit: int = 50) -> dict:
    """Phase C/H1: list captured chains for the Chain View DAG.

    Returns chains sorted by occurrence_count desc, capped at `limit`.
    Each chain carries its node list with provides/consumes already
    populated — the frontend can render the DAG without further calls.
    """
    from ...agents import api_discovery as ad
    try:
        chains = ad.load_chains(project_id)
    except Exception as exc:
        log.warning("list_chains failed: %s", exc)
        chains = []
    chains = sorted(chains, key=lambda c: int(c.get("occurrence_count") or 1), reverse=True)
    return {"project_id": project_id, "count": len(chains), "chains": chains[:limit]}


@router.get("/projects/{project_id}/endpoints")
async def list_endpoints(project_id: str, limit: int = 200) -> dict:
    """Phase B4/H: list captured endpoints with shape catalog.

    Used by the test-explorer's "Discovered API" tab to show what ARTA
    has actually observed the SUT serving.
    """
    from ...agents import api_discovery as ad
    try:
        endpoints = ad._load_captured_endpoints(project_id)   # type: ignore
    except Exception as exc:
        log.warning("list_endpoints failed: %s", exc)
        endpoints = []
    return {
        "project_id": project_id,
        "count": len(endpoints),
        "endpoints": endpoints[:limit],
    }


@router.get("/projects/{project_id}/architecture")
async def architecture_summary(project_id: str) -> dict:
    """Phase AD — Architecture Discovery summary: per-graph node/edge counts,
    protocol_counts, and coverage gaps. Reads the on-disk
    .arta/architecture_discovery/<pid>/discovery_summary.json; empty when the
    phase hasn't run for this project yet."""
    from ...agents import architecture_discovery as ad_phase
    summary = ad_phase.load_summary(project_id)
    return summary or {"project_id": project_id, "graphs": {}}


@router.get("/projects/{project_id}/architecture/{graph}")
async def architecture_graph(project_id: str, graph: str) -> dict:
    """Phase AD — return one Architecture Discovery graph artifact:
    architecture_map | api_graph | dependency_graph | auth_graph |
    workflow_graph | data_flow_graph."""
    from ...agents import architecture_discovery as ad_phase
    if graph not in set(ad_phase._GRAPH_NAMES):
        raise HTTPException(status_code=400,
                            detail=f"unknown graph '{graph}'; allowed: {sorted(ad_phase._GRAPH_NAMES)}")
    g = ad_phase.load_graph(project_id, graph)
    if not g:
        raise HTTPException(status_code=404,
                            detail=f"no {graph} for project {project_id} (run discovery first)")
    return g


@router.get("/projects/{project_id}/traceability")
async def traceability_summary(project_id: str) -> dict:
    """Phase 3 — requirement→code traceability completeness: % of generated
    tests that trace to an endpoint implementing their requirement, plus the
    `potentially_incorrect` tests that don't (directive target: 100%)."""
    from ...agents.traceability_gate import read_traceability
    return read_traceability(project_id)


@router.get("/projects/{project_id}/root-causes")
async def root_causes_summary(project_id: str) -> dict:
    """Fail-Fast/Explain-Clearly — the structured RootCauseReports emitted when
    a generation stage (recipe / ATDD / risk / upstream-gate) failed fast
    instead of switching to a silent fallback. Each carries the 5-level
    deep-dive + recommended_fix + preventive_action + the retry-ladder trace."""
    from ...models.root_cause_report import read_root_causes
    return read_root_causes(project_id)


@router.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str) -> dict:
    """Phase E1+H2: per-step timeline for a run.

    Each step carries test_id, sequence index, method, path, status,
    duration_ms, cascade_skip flag + reason, and provider_contract_violation
    flag. The frontend renders this as a Gantt-style timeline with
    cascade-failure dashed arrows.
    """
    try:
        from .execution import get_steps, per_endpoint_p95
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"execution router unavailable: {exc}")
    steps = get_steps(run_id)
    if not steps:
        return {"run_id": run_id, "count": 0, "steps": [], "per_endpoint_p95": {}}
    return {
        "run_id": run_id,
        "count": len(steps),
        "steps": steps,
        "per_endpoint_p95": per_endpoint_p95(run_id),
    }


class _DiscoveryRefreshBody(BaseModel):
    project_id: str


async def _bg_run_discovery(project_id: str, project_dict: dict) -> None:
    """Phase J6 background task — invoke discovery_executor.execute().

    Builds a minimal context the executor can read (it only touches
    `requirements[0].project` + `workflow_id`). Best-effort: failures
    are logged, the next /summary poll will show stale `last_discovery_at`.
    """
    try:
        from ...agents.discovery_executor import execute as _discovery_execute
    except ImportError as exc:
        log.warning("J6 bg: discovery_executor import failed: %s", exc)
        return

    class _Ctx:
        def __init__(self):
            self.workflow_id = uuid4()
            self.requirements = [{"project": project_dict}]
            self.automation_scripts = {}
            self.gherkin_scenarios = []
            self._current_test_id = None

    try:
        await _discovery_execute(_Ctx(), project_dict)
        log.info("J6 bg: discovery executor completed for project %s", project_id)
    except Exception as exc:
        log.warning("J6 bg: discovery executor failed for %s: %s", project_id, exc)


@router.post("/refresh", status_code=202)
async def refresh_discovery(body: _DiscoveryRefreshBody, background: BackgroundTasks) -> dict:
    """Phase J6 — operator-triggered discovery rerun.

    Two-step:
      1. Stamp `discovery_pending=True` on the project (in case the
         background task can't run, the next pipeline invocation still
         fires Stage 2.5).
      2. Spawn `discovery_executor.execute()` as a BackgroundTask so the
         operator gets immediate 202 + a poll URL for `last_discovery_at`.

    Returns 202 Accepted with the polling URL — the frontend's
    DiscoveryPanel polls `/summary` every 15s after click to surface the
    updated `last_discovery_at` field.
    """
    try:
        from ..db_adapter import try_db
        from ...db import models
        from ...agents import discovery_settings as ds
        import sqlalchemy as sa
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc}")

    project_dict: dict = {}
    # try_db() is an async context manager (matches the pattern used by
    # post_run_chain_pipeline.py + exploratory.py). Pre-existing bug: this
    # file used async-for-as-generator which raises TypeError at runtime.
    try:
        async with try_db() as session:   # type: ignore
            if session is None:
                raise HTTPException(status_code=503, detail="db session unavailable")
            stmt = sa.select(models.Project).where(models.Project.id == body.project_id)
            result = await session.execute(stmt)
            project = result.scalar_one_or_none()
            if project is None:
                raise HTTPException(status_code=404, detail="project not found")
            current = dict(project.discovery_settings or {})
            current[ds.KEY_DISCOVERY_PENDING] = True
            project.discovery_settings = current
            await session.commit()

            # Build the minimal project dict the executor consumes.
            project_dict = {
                "id": str(project.id),
                "name": project.name,
                "is_api_only": bool(getattr(project, "is_api_only", False)),
                "discovery_settings": current,
                "integrations": dict(getattr(project, "integrations", None) or {}),
            }
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("refresh_discovery commit failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    # R219.C — the discovery executor needs `environments` (base_url,
    # api_base_url, auth block) to point the probe at the SUT. The DB
    # Project row has no `environments` column — that config lives only in
    # the file-backed project registry — so the DB-only dict above yields
    # env=None/base=None and the probe captures nothing. Merge the full
    # project's environments (+ llm/token config) from the canonical
    # resolver so any project onboarded via the API works, not just those
    # whose base_url happens to sit in integrations.
    try:
        from .projects import _resolve_project
        _full = await _resolve_project(body.project_id)
        if _full:
            for _k in ("environments", "llm_config", "token_exchange", "onboarding_config"):
                if _full.get(_k) and not project_dict.get(_k):
                    project_dict[_k] = _full[_k]
            # R221 — the DB `discovery_settings` (replay/HAR knobs) lacks the
            # file-registry probe knobs (route_cap, bfs_depth,
            # hardcoded_probes_disable). MERGE the file's discovery_settings ON
            # TOP so per-SUT probe config in projects.json actually reaches the
            # executor (file wins for overlapping keys; DB's discovery_pending
            # stamp is preserved since the file lacks it).
            _file_ds = _full.get("discovery_settings") or {}
            if isinstance(_file_ds, dict) and _file_ds:
                _merged = {**(project_dict.get("discovery_settings") or {}), **_file_ds}
                _merged[ds.KEY_DISCOVERY_PENDING] = True
                project_dict["discovery_settings"] = _merged
    except Exception as exc:
        log.warning("refresh_discovery: env-merge from registry failed: %s", exc)

    # Phase J6 — dispatch background discovery executor.
    if not project_dict.get("is_api_only"):
        background.add_task(_bg_run_discovery, body.project_id, project_dict)
        status = "dispatched"
    else:
        status = "skipped_api_only"

    return {
        "project_id": body.project_id,
        "discovery_pending": True,
        "status": status,
        "poll_url": f"/api/discovery/projects/{body.project_id}/summary",
        "note": (
            "Discovery executor running in background. Poll the summary "
            "endpoint to see updated last_discovery_at."
            if status == "dispatched"
            else "Project marked api_only — Stage 2.5 unconditionally skipped."
        ),
    }
