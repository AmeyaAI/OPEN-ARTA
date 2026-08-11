"""
ARTA Platform — Reusable FastAPI Dependencies
Authentication, authorization, and database session injection.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Header, Query, Request, status
from fastapi.security import OAuth2PasswordBearer

log = logging.getLogger("arta.auth")

from ..db.session import get_db

try:
    from jose import JWTError, jwt
except ImportError:
    jwt = None  # type: ignore[assignment]
    JWTError = Exception  # type: ignore[misc,assignment]

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.repository import UserRepo, ProjectRoleRepo

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

_JWT_SECRET = os.environ.get("JWT_SECRET", "arta-dev-secret-change-in-production")
_JWT_ALGORITHM = "HS256"

# Role hierarchy: higher index = more privilege
_ROLE_ORDER = {"viewer": 0, "tester": 1, "qa_lead": 2, "admin": 3}


# ── Database Session ─────────────────────────────────────────────────────────

DBSession = Annotated[AsyncSession, Depends(get_db)]


# ── Current User ─────────────────────────────────────────────────────────────

async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    x_api_key: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Extract and validate the current user from JWT or API key.

    Supports both:
    - Bearer JWT token (from login)
    - X-API-Key header (for CI/CD and programmatic access)
    """
    repo = UserRepo(db)

    # Try JWT first
    if token and jwt:
        try:
            payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
            email: str | None = payload.get("sub")
            if not email:
                raise HTTPException(status_code=401, detail="Invalid token")
            user = await repo.get_by_email(email)
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            if not user.is_active:
                raise HTTPException(status_code=403, detail="Account deactivated")
            return user
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Fallback: API key (admin access for CI/CD)
    expected_key = os.environ.get("ARTA_API_KEY", "")
    if x_api_key and expected_key and x_api_key == expected_key:
        # Return the admin user for API-key auth
        admin = await repo.get_by_email("admin@arta.dev")
        if admin:
            return admin

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_optional(
    token: str | None = Depends(oauth2_scheme),
    x_api_key: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Like get_current_user but returns None instead of raising 401."""
    try:
        return await get_current_user(token, x_api_key, db)
    except HTTPException:
        return None


# ── API Key Check (CI / programmatic access) ────────────────────────────────

async def require_api_key(
    x_api_key: str | None = Header(None),
    authorization: str | None = Header(None),
    api_key: str | None = Query(None),  # F12-1: SSE clients can't send headers
    token: str | None = Query(None),    # F12-1: JWT-as-query for SSE
) -> None:
    """F4-1 + F6-1 + F12-1: Centralised API-key check used by 13 routers.

    Accepts in this order:
      1. `X-API-Key` header equal to the configured ARTA_API_KEY.
      2. `Authorization: Bearer <token>` where <token> is either:
         a) the configured ARTA_API_KEY value (Bearer-as-API-key), OR
         b) a JWT signed with JWT_SECRET (Bearer-as-JWT).
      3. `?api_key=<key>` query param equal to ARTA_API_KEY (F12-1).
      4. `?token=<jwt>` query param validated against JWT_SECRET (F12-1).

    No-ops in dev when ARTA_API_KEY is empty.

    F6-1: Previous version returned success on ANY header starting with "Bearer ",
    which was a complete auth bypass. The plan archive has the proof. Now the
    Bearer token MUST be either the API key or a valid JWT.

    F12-1: Browser EventSource API can't attach custom headers, so SSE
    consumers (`/api/tests/generate-all/stream/{job_id}`) authenticate via
    `?api_key=` or `?token=` query params. The trade-off is that query params
    end up in server access logs, but for short-lived job streams (~minutes)
    this is the standard pattern. Job IDs are not guessable so log exposure
    is bounded.
    """
    expected = os.environ.get("ARTA_API_KEY", "")
    if not expected:
        return
    # R41.2 — track WHICH credential check failed so the structured 401
    # detail can carry an actionable reason. Pre-fix the generic
    # "Invalid or missing API key or Bearer token" message left the
    # operator stuck (couldn't tell jwt-expired from missing-header
    # from secret-rotation). Specific reasons let the frontend branch:
    # jwt_expired → redirect to /login; no_credential_provided →
    # surface as a config bug; jwt_invalid → log + redirect.
    reason = "no_credential_provided"
    if x_api_key:
        if x_api_key == expected:
            return
        reason = "x_api_key_mismatch"
    if api_key:
        if api_key == expected:
            return
        reason = "query_api_key_mismatch"
    if authorization and authorization.startswith("Bearer "):
        bearer_token = authorization[len("Bearer "):].strip()
        if bearer_token and bearer_token == expected:
            return
        if bearer_token and jwt is not None:
            try:
                jwt.decode(bearer_token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
                return
            except JWTError as jwt_exc:
                # python-jose raises JWTError for both expired and
                # invalid signatures. Distinguish by inspecting the
                # exception class name when available.
                cls_name = type(jwt_exc).__name__
                if "Expired" in cls_name:
                    reason = "jwt_expired"
                else:
                    reason = f"jwt_invalid:{cls_name}"
        elif bearer_token:
            reason = "jwt_lib_unavailable"
    elif authorization:
        reason = "auth_header_not_bearer"
    if token and jwt is not None:
        try:
            jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
            return
        except JWTError as jwt_exc:
            cls_name = type(jwt_exc).__name__
            if "Expired" in cls_name:
                reason = "query_jwt_expired"
            else:
                reason = f"query_jwt_invalid:{cls_name}"
    log.warning(
        "auth: 401 reason=%s x_api_key=%s authorization=%s",
        reason,
        "set" if x_api_key else "missing",
        ("bearer:set" if (authorization and authorization.startswith("Bearer "))
         else "missing"),
    )
    user_message = (
        "Session expired — please log in again."
        if reason in ("jwt_expired", "query_jwt_expired")
        else "Invalid or missing API key or Bearer token."
    )
    raise HTTPException(
        status_code=401,
        detail={
            "error": "auth_failed",
            "reason": reason,
            "message": user_message,
        },
    )


# ── Role Checks ──────────────────────────────────────────────────────────────

def require_admin(user=Depends(get_current_user)):
    """Dependency: requires platform-level admin."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_role(min_role: str):
    """Factory: creates a dependency requiring at least `min_role` on a given project."""
    min_level = _ROLE_ORDER.get(min_role, 0)

    async def _check(
        project_id: str,
        user=Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        # Platform admins bypass project-level checks
        if user.is_admin:
            return user
        repo = ProjectRoleRepo(db)
        role = await repo.get_role(str(user.id), project_id)
        if not role:
            raise HTTPException(status_code=403, detail="No access to this project")
        user_level = _ROLE_ORDER.get(role.role, 0)
        if user_level < min_level:
            raise HTTPException(
                status_code=403,
                detail=f"Requires at least '{min_role}' role on this project",
            )
        return user

    return _check


# ── Per-user project RBAC enforcement (the real authorization gate) ───────────
#
# The `_require_api_key` gate authorizes by a SHARED key (the frontend ships
# `NEXT_PUBLIC_ARTA_API_KEY` to every browser) or ANY JWT — it never checks WHO
# the user is or their project role. So a logged-in `viewer` could do everything.
# `require_project_access` is the fix: it resolves the authenticated USER from the
# Authorization Bearer JWT and enforces their per-project role. It is applied
# ALONGSIDE `_require_api_key` on every project-scoped endpoint.

async def _resolve_principal(request, db: AsyncSession):
    """Resolve who is calling, SAFELY. Returns (user | None, is_machine).

    - Bearer = a valid USER JWT  → (user, False): enforce that user's project role.
    - Bearer = the shared ARTA_API_KEY → (None, True): trusted machine/CI → bypass RBAC.
    - No Authorization header (X-API-Key / ?api_key only) → (None, True): machine/CI.
    - Bearer present but an INVALID/EXPIRED user JWT → raise 401. NEVER silently bypass
      just because the (public) shared key is also present — that would let an expired
      session keep full access.
    """
    authz = request.headers.get("authorization") or ""
    expected_key = os.environ.get("ARTA_API_KEY", "")

    async def _user_from_jwt(tok: str):
        payload = jwt.decode(tok, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        email = payload.get("sub")
        user = await UserRepo(db).get_by_email(email) if email else None
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not getattr(user, "is_active", True):
            raise HTTPException(status_code=403, detail="Account deactivated")
        return user

    # 1. Authorization: Bearer <user-JWT | backend-key>
    if authz.lower().startswith("bearer "):
        tok = authz[7:].strip()
        if expected_key and tok == expected_key:
            return None, True  # Bearer-as-API-key → machine/CI
        if not jwt:
            raise HTTPException(status_code=401, detail="Invalid token")
        try:
            return (await _user_from_jwt(tok)), False
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid or expired token")

    # 2. SSE / query-token path (F12-1): ?token=<user-JWT>
    qtoken = request.query_params.get("token")
    if qtoken and jwt:
        try:
            return (await _user_from_jwt(qtoken)), False
        except HTTPException:
            raise
        except Exception:
            pass

    # 3. Machine/CI — ONLY the SECRET backend key (X-API-Key or ?api_key). The frontend's
    #    PUBLIC NEXT_PUBLIC_ARTA_API_KEY is a different value, so it can't reach this path.
    if expected_key:
        if (request.headers.get("x-api-key") == expected_key
                or request.query_params.get("api_key") == expected_key):
            return None, True
        raise HTTPException(status_code=401, detail="Missing or invalid authentication")

    # 4. No API key configured (local dev) → open, matching require_api_key's dev no-op.
    return None, True


def require_project_access(min_role: str | None = None):
    """Enforce the caller's per-project role on a project-scoped endpoint.

    `min_role=None` (default) infers from the HTTP method: GET/HEAD/OPTIONS → `viewer`
    (read), any mutating method → `tester` (write). Pass an explicit role for endpoints
    that need more (e.g. `qa_lead`/`admin`). Machine/CI (shared API key) and platform
    admins bypass. Requires the route to expose a `project_id` path parameter.
    """
    async def _check(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
        user, is_machine = await _resolve_principal(request, db)
        if is_machine:
            return None                       # trusted CI/machine
        if getattr(user, "is_admin", False):
            return user                       # platform admin bypass
        mr = min_role or (
            "viewer" if request.method in ("GET", "HEAD", "OPTIONS") else "tester")
        role = await ProjectRoleRepo(db).get_role(str(user.id), project_id)
        if not role:
            raise HTTPException(status_code=403, detail="No access to this project")
        if _ROLE_ORDER.get(role.role, 0) < _ROLE_ORDER.get(mr, 0):
            raise HTTPException(
                status_code=403,
                detail=f"Requires at least '{mr}' role on this project")
        return user

    return _check


async def accessible_project_ids(request, db: AsyncSession) -> set[str] | None:
    """The set of project_ids the caller may see, for READ-scoping list endpoints.
    Returns None when the caller is a machine/CI or a platform admin (see everything).
    """
    user, is_machine = await _resolve_principal(request, db)
    if is_machine or getattr(user, "is_admin", False):
        return None  # unrestricted
    roles = await ProjectRoleRepo(db).list_for_user(str(user.id))
    return {str(r.project_id) for r in (roles or [])}


# ── Router-wide RBAC enforcement (one route_class covers every endpoint) ───────

from fastapi.routing import APIRoute  # noqa: E402

# Endpoints whose write should require a higher-than-tester role. Path is matched by
# suffix so it survives the router prefix. (Deleting a whole project / editing its
# settings / CI wiring is a lead/owner action, not a tester one.)
_ELEVATED_WRITE_SUFFIXES = ("/settings", "/ci-config", "/integrations")

# Resource-id path params → the repo that maps that id to its owning project_id. Lets
# RBACRoute scope endpoints keyed by a RESOURCE id (e.g. GET /requirements/{req_id},
# GET /execution/runs/{run_id}) that carry no project_id — otherwise any user could read
# or mutate another project's resource by id. (repo_class, get_method, id_normaliser)
_RESOURCE_PROJECT_RESOLVERS: dict[str, tuple[str, str]] = {
    "req_id": ("RequirementRepo", "get"),
    "test_id": ("TestCaseRepo", "get"),
    "run_id": ("TestRunRepo", "get"),
    "defect_id": ("DefectRepo", "get"),
    "proposal_id": ("HealingProposalRepo", "get"),
    "session_id": ("ExploratorySessionRepo", "get"),
    "finding_id": ("ExploratoryFindingRepo", "get"),
}


async def _project_for_resource(path_params: dict, db: AsyncSession) -> str | None:
    """Resolve the owning project_id for a resource-id path param, best-effort. Returns
    None when unresolvable (non-existent id → the handler 404s; unmapped resource type →
    left to any other guard)."""
    from ..db import repository as _repo  # lazy — avoid import cycles
    for param, (repo_name, method) in _RESOURCE_PROJECT_RESOLVERS.items():
        rid = path_params.get(param)
        if not rid:
            continue
        if param == "run_id":  # live runs live in-memory before/without a DB row
            try:
                from .routers.execution import _REAL_RUNS
                pid = (_REAL_RUNS.get(rid) or {}).get("project_id")
                if pid:
                    return str(pid)
            except Exception:
                pass
        try:
            repo = getattr(_repo, repo_name)(db)
            obj = await getattr(repo, method)(rid.upper() if param == "req_id" else rid)
            pid = getattr(obj, "project_id", None) if obj else None
            if pid:
                return str(pid)
        except Exception:
            pass
    return None


class RBACRoute(APIRoute):
    """APIRoute that enforces per-user project RBAC on any route exposing a
    `project_id` (path OR query). Applied via `router.route_class = RBACRoute`, so a
    single line protects every endpoint in the router — reads need `viewer`, writes need
    `tester` (elevated for settings/ci/integrations). Routes without a project_id (global
    lists, health, etc.) are untouched here and scoped separately. Machine/CI (shared API
    key) and platform admins bypass; a Bearer USER token is always enforced (never bypassed
    via the public shared key)."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def rbac_handler(request: Request):
            project_id = (request.path_params.get("project_id")
                          or request.query_params.get("project_id"))
            # Mutating endpoints often carry project_id in the JSON body (POST /run,
            # /generate, …). Read it here — Starlette caches request._body so the real
            # handler still parses the body normally.
            if not project_id and request.method not in ("GET", "HEAD", "OPTIONS"):
                if "application/json" in (request.headers.get("content-type") or ""):
                    try:
                        raw = await request.body()
                        if raw:
                            import json as _json
                            data = _json.loads(raw)
                            if isinstance(data, dict):
                                cand = data.get("project_id") or data.get("project")
                                if isinstance(cand, str) and cand:
                                    project_id = cand
                    except Exception:
                        project_id = None
            # Resource-id endpoints (e.g. /requirements/{req_id}, /runs/{run_id}) carry no
            # project_id — resolve the owning project from the resource so they're scoped too.
            needs_resource_lookup = (not project_id and any(
                p in request.path_params for p in _RESOURCE_PROJECT_RESOLVERS))
            if project_id or needs_resource_lookup:
                from ..db.session import async_session_factory
                session = async_session_factory()
                try:
                    if not project_id:
                        project_id = await _project_for_resource(dict(request.path_params), session)
                    # Only enforce when we have an owning project; unresolvable ids fall
                    # through to the handler (which 404s a non-existent resource).
                    if project_id:
                        user, is_machine = await _resolve_principal(request, session)
                        if not is_machine and not getattr(user, "is_admin", False):
                            if request.method in ("GET", "HEAD", "OPTIONS"):
                                min_role = "viewer"
                            elif any(request.url.path.endswith(s) for s in _ELEVATED_WRITE_SUFFIXES):
                                min_role = "qa_lead"
                            else:
                                min_role = "tester"
                            role = await ProjectRoleRepo(session).get_role(str(user.id), project_id)
                            if not role:
                                raise HTTPException(status_code=403, detail="No access to this project")
                            if _ROLE_ORDER.get(role.role, 0) < _ROLE_ORDER.get(min_role, 0):
                                raise HTTPException(
                                    status_code=403,
                                    detail=f"Requires at least '{min_role}' role on this project")
                finally:
                    try:
                        await session.close()
                    except Exception:
                        pass
            return await original(request)

        return rbac_handler
