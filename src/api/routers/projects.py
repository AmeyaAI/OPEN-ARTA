"""
ARTA Projects Router
Multi-project support with per-project LLM provider configuration.

Endpoints:
  GET    /api/projects                  List all projects
  POST   /api/projects                  Create project (auth required)
  GET    /api/projects/providers        LLM provider + model presets (public)
  GET    /api/projects/{id}             Get project detail (masked API key)
  PUT    /api/projects/{id}             Update project config (auth required)
  DELETE /api/projects/{id}             Delete project (auth required)
  GET    /api/projects/{id}/summary     Quick coverage + defect summary
  POST   /api/projects/{id}/test-llm          Test LLM connectivity (auth required)
  POST   /api/projects/{id}/test-connectivity  Test target-app reachability (auth required)
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("arta.projects")

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ...models.llm_config import PROVIDER_PRESETS, LLMConfig, LLMProvider

from ..dependencies import RBACRoute  # RBAC: per-user project role enforcement
router = APIRouter(route_class=RBACRoute)
# ── Auth dependency ───────────────────────────────────────────────────────────


# F4-1: API-key check is centralised in src/api/dependencies.py.
from ..dependencies import require_api_key as _require_api_key  # noqa: E402


# ── Pydantic models ───────────────────────────────────────────────────────────


class ToolOverrideInput(BaseModel):
    """R127.A — per-tool LLM override schema.

    Empty / unset fields inherit from the project's base llm_config when
    `resolve_tool_config` runs (single source of truth in
    src/models/llm_config.py). Operators set this to route a SPECIFIC
    tool (e.g. `playwright`) to a different provider without touching
    the project's default for the other tools.

    R127.C also reuses this schema via conventional key suffix
    `<tool_name>_escalation` to configure the escalation client.
    """
    provider:    str         = ""
    model:       str         = ""
    api_key:     str         = ""
    base_url:    str         = ""
    temperature: float | None = None
    max_tokens:  int   | None = None


class LLMConfigInput(BaseModel):
    provider:    str   = "anthropic"
    model:       str   = "claude-sonnet-4-6"
    api_key:     str   = ""
    base_url:    str   = ""
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens:  int   = Field(default=4096, ge=256, le=32768)
    # R127.A — per-tool overrides (keys: playwright, newman, k6, axe, zap,
    # selenium, cypress, appium, plus `<tool>_escalation` for R127.C).
    # Empty dict = no overrides = current behavior.
    tool_overrides:       dict[str, ToolOverrideInput] = {}
    # R127.C — quality-gated escalation knobs.
    # Threshold: R126.Q aggregate score below this triggers escalation.
    # Cap (R127.C original / R127.E.3 per-batch ceiling): max escalations
    # across the WHOLE gen batch — operator's true cost knob.
    # Cap per req (R127.E.3): fresh budget per req. Prevents one dense
    # 16-sub req from exhausting the batch budget + starving siblings.
    escalation_threshold:    float = Field(default=0.5, ge=0.0, le=1.0)
    escalation_cap:          int   = Field(default=10,  ge=0, le=1000)
    escalation_cap_per_req:  int   = Field(default=10,  ge=0, le=100)
    # R128.B — LLM output cache (Redis-backed via observability/cache.py).
    # Opt-in (cache_enabled=False default preserves current behavior).
    # Bypassed when LLMConfig.temperature > cache_temperature_max
    # (variance protection — caching randomized outputs is pointless).
    cache_enabled:           bool  = False
    cache_ttl_seconds:       int   = Field(default=604800, ge=60, le=2592000)  # 1m to 30d
    cache_temperature_max:   float = Field(default=0.3, ge=0.0, le=1.0)


class EnvironmentAuth(BaseModel):
    # M4 — extra="allow" so the CANONICAL per-SUT auth profile survives model_dump().
    # Beyond method+credentials the profile carries schema-less fields discovered from
    # the SUT: chain, host_map, refresh, login, login_flows, mint, provenance
    # (_derived/_derived_from/_source_corrected), and `locked` (authoritative — never
    # re-derived). Without extra="allow" pydantic v2 DROPPED these on any PUT, wiping
    model_config = ConfigDict(extra="allow")
    method:      str  = "none"   # cookie | bearer | basic | oauth2 | none
    credentials: dict = {}       # method-specific: cookie_name/value, bearer_token, username/password


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="allow")   # M4 — preserve any future env-level keys
    base_url:     str                    = ""
    api_base_url: str | None             = None
    auth:         EnvironmentAuth        = EnvironmentAuth()
    variables:    dict                   = {}    # key-value env vars for test runner
    roles:        list[dict] | None      = None  # [{name, localStorage_overrides}] for multi-role testing


class RepositoryEntry(BaseModel):
    name:     str = ""
    repo:     str = ""         # "owner/repo" or repo name
    branch:   str = "main"
    provider: str = "github"   # github | gitlab | bitbucket


class IntegrationsInput(BaseModel):
    github_repo:   str = ""    # "owner/repo" — kept for backward compat
    github_token:  str = ""    # PAT shared across all repos
    jira_project:  str = ""    # project KEY, e.g. "OP"
    # Per-project Jira credentials (native import). Pre-fix ARTA had only a
    # single global-env JiraClient; these let each SUT read its own Jira.
    jira_url:      str = ""
    jira_email:    str = ""    # Atlassian account email
    jira_api_token: str = ""   # Atlassian API token (shared with Confluence)
    slack_channel: str = ""    # "#qa-alerts"
    base_url:      str = ""    # Target app URL for automation
    zap_url:       str = ""
    repositories:  list[RepositoryEntry] = []  # Multi-repo support


class QualityGatesInput(BaseModel):
    p0_coverage_pct:              float = 100.0
    p1_coverage_pct:              float = 90.0
    p2_coverage_pct:              float = 75.0
    overall_coverage_pct:         float = 80.0
    p0_pass_rate_pct:             float = 100.0
    overall_pass_rate_pct:        float = 90.0
    performance_p95_ms:           int   = 3000
    max_open_p0_defects:          int   = 0
    max_critical_security_findings: int = 0


class ProjectCreate(BaseModel):
    name:          str                              = Field(..., min_length=1, max_length=255)
    description:   str                              = ""
    color:         str                              = "#6366f1"
    icon:          str                              = "🧪"
    project_type:  str | None                       = None
    llm_config:    LLMConfigInput                   = LLMConfigInput()
    quality_gates: QualityGatesInput                = QualityGatesInput()
    integrations:  IntegrationsInput                = IntegrationsInput()
    environments:  dict[str, EnvironmentConfig]     = {}


class ProjectUpdate(BaseModel):
    name:          str | None                              = None
    description:   str | None                              = None
    color:         str | None                              = None
    icon:          str | None                              = None
    project_type:  str | None                              = None
    llm_config:    LLMConfigInput | None                   = None
    quality_gates: QualityGatesInput | None                = None
    integrations:  IntegrationsInput | None                = None
    environments:  dict[str, EnvironmentConfig] | None     = None


# ── In-memory project store (replace with DB in production) ──────────────────
# Seeded with 3 demo projects matching the schema.sql seed data.

_PROJECTS: dict[str, dict] = {
    "00000000-0000-0000-0000-000000000001": {
        "id":          "00000000-0000-0000-0000-000000000001",
        "name":        "E-Commerce Platform",
        "description": "Checkout, Cart, Refund, and Search — REQ-017 to REQ-020.",
        "color":       "#6366f1",
        "icon":        "🛒",
        "llm_config": {
            "provider":    "anthropic",
            "model":       "claude-sonnet-4-6",
            "api_key":     "",
            "base_url":    "",
            "temperature": 0.2,
            "max_tokens":  4096,
        },
        "quality_gates": {},
        "integrations": {
            "github_repo":   "",
            "jira_project":  "EP",
            "slack_channel": "#qa-alerts",
            "base_url":      "http://localhost:3000",
        },
        "environments": {},
        "requirement_count": 4,
        "test_count":         34,
        "coverage_pct":       78.0,
        "open_defects":       7,
        "last_run_status":    "BLOCK",
        "created_at":         "2026-01-15T10:00:00Z",
        "updated_at":         "2026-03-11T14:34:23Z",
    },
    "00000000-0000-0000-0000-000000000002": {
        "id":          "00000000-0000-0000-0000-000000000002",
        "name":        "Mobile Banking App",
        "description": "iOS/Android banking feature tests — login, transfers, statements.",
        "color":       "#8b5cf6",
        "icon":        "🏦",
        "llm_config": {
            "provider":    "google_gemini",
            "model":       "gemini-2.0-flash",
            "api_key":     "",
            "base_url":    "",
            "temperature": 0.2,
            "max_tokens":  4096,
        },
        "quality_gates": {},
        "integrations": {
            "github_repo":   "",
            "jira_project":  "MBA",
            "slack_channel": "#mobile-qa",
            "base_url":      "http://localhost:4000",
        },
        "environments": {},
        "requirement_count": 0,
        "test_count":         0,
        "coverage_pct":       0.0,
        "open_defects":       0,
        "last_run_status":    None,
        "created_at":         "2026-02-20T09:00:00Z",
        "updated_at":         "2026-02-20T09:00:00Z",
    },
    "00000000-0000-0000-0000-000000000003": {
        "id":          "00000000-0000-0000-0000-000000000003",
        "name":        "Internal Tooling",
        "description": "Developer portal, admin dashboard, and CI/CD toolchain tests.",
        "color":       "#06b6d4",
        "icon":        "⚙️",
        "llm_config": {
            "provider":    "ollama",
            "model":       "qwen2.5:32b",
            "api_key":     "",
            "base_url":    "http://host.docker.internal:11434",
            "temperature": 0.2,
            "max_tokens":  4096,
        },
        "quality_gates": {},
        "integrations": {
            "github_repo":   "",
            "jira_project":  "IT",
            "slack_channel": "#devops",
            "base_url":      "http://localhost:5000",
        },
        "environments": {},
        "requirement_count": 0,
        "test_count":         0,
        "coverage_pct":       0.0,
        "open_defects":       0,
        "last_run_status":    None,
        "created_at":         "2026-03-01T08:00:00Z",
        "updated_at":         "2026-03-01T08:00:00Z",
    },
    # R113.M.2 — seed real customer projects into in-memory dict so they
    # survive fresh deployments where `.arta/projects.json` is missing.
    # The on-disk file remains the override channel for operator edits
    # (loaded at startup via _load_projects + .update()), but the dict
    # Each entry declares `automation_dir` (R113.M.1) so the dispatch
    # helper `_r113_resolve_pw_scripts_dir` finds the correct spec dir
    # without hardcoded fallback strings in execution.py.
    "18347cc8-96e8-44f2-9c26-2be7c2953ca3": {
        "id":          "18347cc8-96e8-44f2-9c26-2be7c2953ca3",
        "name":        "BugTrackr",
        "description": "Next.js bug tracker — REQ-BT-001 to REQ-BT-008.",
        "color":       "#6366f1",
        "icon":        "🐞",
        "automation_dir": "bugtrackr",  # R113.M.1 → src/automation/bugtrackr/
        "llm_config": {
            "provider":    "ollama",
            "model":       "qwen2.5:32b",
            "api_key":     "",
            "base_url":    "http://host.docker.internal:11434",
            "temperature": 0.2,
            "max_tokens":  4096,
        },
        "quality_gates": {},
        "integrations": {
            "github_repo":   "",
            "jira_project":  "BT",
            "slack_channel": "#bugtrackr-qa",
            "base_url":      "http://localhost:3005",
        },
        "environments": {},
        "requirement_count": 8,
        "test_count":         0,
        "coverage_pct":       0.0,
        "open_defects":       0,
        "last_run_status":    None,
        "created_at":         "2026-03-22T00:00:00Z",
        "updated_at":         "2026-05-18T00:00:00Z",
    },
    "a1b2c3d4-5678-4ef0-abcd-1234567890ab": {
        "id":          "a1b2c3d4-5678-4ef0-abcd-1234567890ab",
        "name":        "Sample Analytics App",
        "description": "Second sample project — a multi-service analytics platform.",
        "color":       "#8b5cf6",
        "icon":        "📊",
        # directory. R113.A's helper falls back there automatically when no
        # project-scoped or automation_dir-declared directory contains specs.
        # No `automation_dir` declared here → resolver hits the legacy fallback,
        # which still works because the PW project filter scopes by spec prefix.
        "llm_config": {
            "provider":    "ollama",
            "model":       "qwen2.5:32b",
            "api_key":     "",
            "base_url":    "http://host.docker.internal:11434",
            "temperature": 0.2,
            "max_tokens":  4096,
        },
        "quality_gates": {},
        "integrations": {
            "github_repo":   "",
            "jira_project":  "SAMPLE",
            "slack_channel": "#qa",
            "base_url":      "http://localhost:3006",
        },
        "environments": {},
        "requirement_count": 22,
        "test_count":         0,
        "coverage_pct":       0.0,
        "open_defects":       0,
        "last_run_status":    None,
        "created_at":         "2026-03-22T00:00:00Z",
        "updated_at":         "2026-05-18T00:00:00Z",
    },
}


_SENSITIVE_CRED_KEYS = {"cookie_value", "bearer_token", "token", "password", "secret", "refresh_token"}
# The value GET/_mask substitutes for a sensitive credential. A client that echoes a
# masked GET back on save must NOT overwrite the real secret with this sentinel.
_MASK_SENTINEL = "***"


def _r84_widen_cookie_domain(
    cookie_domain: str | None,
    base_url: str | None,
    api_base_url: str | None,
) -> tuple[str | None, str | None]:
    """R84 + R144.B.2 — leading-dot cookie-domain widening helper.

    Returns ``(maybe_widened_domain, trigger)`` where ``trigger`` is one of:
      - ``None``   : no change applied
      - ``"explicit"``      : R84 cross-subdomain (base + api_base both set, different hosts under cookie_domain)
      - ``"implicit_widen"``: R144.B.2 (api_base unset, base is a ≥3-label subdomain matching cookie_domain)

    Idempotent: leading-dot domains pass through unchanged.
    Conservative: IP-literal hosts skipped; ≥3-label requirement avoids
    polluting root domains like ``example.com``.

    Live evidence (Iter 1-3 R143.F): a real SUT's login
    staging env had only ``base_url`` set → ``api_host=""`` → R84 never
    fired → cookie stayed host-only on the app host → SPA fetch to
    the api host sent no cookie → SUT 401 →
    redirect to ``/login`` → R143.G skipped the test cascade silently.
    """
    if not cookie_domain:
        return (cookie_domain, None)
    if cookie_domain.startswith("."):
        return (cookie_domain, None)   # already widened, idempotent
    if cookie_domain.replace(".", "").isdigit():
        return (cookie_domain, None)   # IP literal, no subdomain semantics

    from urllib.parse import urlparse

    api_host = urlparse(api_base_url).netloc if api_base_url else ""
    base_host = urlparse(base_url).netloc if base_url else ""

    needs_cross_subdomain = bool(
        api_host and base_host
        and api_host != base_host
        and api_host.endswith("." + cookie_domain.lstrip("."))
    )
    needs_implicit_widen = bool(
        not api_host
        and base_host
        and cookie_domain.lstrip(".") == base_host
        and len(base_host.split(".")) >= 3
    )
    if needs_cross_subdomain:
        return ("." + cookie_domain, "explicit")
    if needs_implicit_widen:
        return ("." + cookie_domain, "implicit_widen")
    return (cookie_domain, None)


def _mask(project: dict) -> dict:
    """Return project dict with api_key and environment credentials masked."""
    p = dict(project)

    # Mask LLM api_key
    llm = dict(p.get("llm_config", {}))
    if llm.get("api_key"):
        llm["api_key"] = "***"
    p["llm_config"] = llm

    # Mask sensitive values in environment credentials
    envs = p.get("environments")
    if envs:
        masked_envs = {}
        for env_name, env_cfg in envs.items():
            env_copy = dict(env_cfg) if isinstance(env_cfg, dict) else env_cfg
            auth = env_copy.get("auth")
            if auth and isinstance(auth, dict):
                auth = dict(auth)
                creds = auth.get("credentials")
                if creds and isinstance(creds, dict):
                    creds = dict(creds)
                    for key in _SENSITIVE_CRED_KEYS:
                        if creds.get(key):
                            creds[key] = "***"
                    auth["credentials"] = creds
                env_copy["auth"] = auth
            masked_envs[env_name] = env_copy
        p["environments"] = masked_envs

    # F6-3: Mask sensitive integration tokens. Expanded to cover the actual
    # field names found in projects.json (github_token, jira_api_token,
    # slack_webhook_url) — the previous list missed jira_api_token + the
    # _url suffix variant of slack_webhook.
    integ = p.get("integrations")
    if integ and isinstance(integ, dict):
        integ = dict(integ)
        for key in ("github_token", "jira_token", "jira_api_token",
                    "slack_webhook", "slack_webhook_url",
                    "confluence_api_token", "teams_webhook_url"):
            if integ.get(key):
                integ[key] = "***"
        p["integrations"] = integ

    return p


# ── Project resolution (in-memory + DB fallback) ────────────────────────────


async def _resolve_project(project_id: str) -> dict | None:
    """Look up project from in-memory cache, falling back to DB."""
    project = _PROJECTS.get(project_id)
    if project:
        return project
    from ..db_adapter import try_db
    async with try_db() as db:
        if db:
            from ...db.repository import ProjectRepo, _to_dict
            repo = ProjectRepo(db)
            row = await repo.get(project_id)
            if row:
                project = _to_dict(row)
                for key, default in [("environments", {}), ("project_type", None),
                                     ("requirement_count", 0), ("test_count", 0),
                                     ("coverage_pct", 0.0), ("open_defects", 0),
                                     ("last_run_status", None)]:
                    project.setdefault(key, default)
                _PROJECTS[project_id] = project
                return project
    return None


# ── Persistence helpers ───────────────────────────────────────────────────────

_PROJECTS_FILE = Path(os.environ.get("ARTA_PROJECTS_FILE", ".arta/projects.json"))


def _save_projects() -> None:
    """Persist user-created projects (non-demo) to JSON file.

    F6-3: Tighten file mode to 0600 after every write. The file holds plaintext
    integration tokens (GitHub PATs, Jira tokens, session cookies) and must not
    be readable by other users on the host. Default umask often produces 0664.
    """
    demo_ids = {"00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
                "00000000-0000-0000-0000-000000000003"}
    user_projects = {k: v for k, v in _PROJECTS.items() if k not in demo_ids}
    _PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROJECTS_FILE.write_text(json.dumps(user_projects, indent=2, default=str))
    try:
        os.chmod(_PROJECTS_FILE, 0o600)
    except OSError as e:
        log.warning("Could not chmod %s to 0600: %s — secrets may be world-readable",
                    _PROJECTS_FILE, e)


def _load_projects() -> None:
    """Load persisted user projects and merge with demo data."""
    if _PROJECTS_FILE.exists():
        try:
            saved = json.loads(_PROJECTS_FILE.read_text())
            _PROJECTS.update(saved)
        except (json.JSONDecodeError, OSError):
            pass


_load_projects()


_DEMO_IDS = {"00000000-0000-0000-0000-000000000001",
             "00000000-0000-0000-0000-000000000002",
             "00000000-0000-0000-0000-000000000003"}


async def sync_projects_to_db():
    """Sync user projects from .arta/projects.json into PostgreSQL if DB is available."""
    from ..db_adapter import try_db
    try:
        async with try_db() as db:
            if db:
                from sqlalchemy import text
                for pid, proj in _PROJECTS.items():
                    if pid in _DEMO_IDS:
                        continue
                    exists = (await db.execute(
                        text("SELECT 1 FROM projects WHERE id = :id"),
                        {"id": pid}
                    )).scalar()
                    if not exists:
                        # Store project_type inside integrations JSONB since
                        # the projects table may not have a project_type column
                        integ = dict(proj.get("integrations", {}))
                        integ["_project_type"] = proj.get("project_type", "web_app")
                        await db.execute(text("""
                            INSERT INTO projects (id, name, description, color, icon,
                                                  llm_config, quality_gates, integrations, created_at, updated_at)
                            VALUES (:id, :name, :desc, :color, :icon,
                                    CAST(:llm AS jsonb), CAST(:gates AS jsonb), CAST(:integ AS jsonb), NOW(), NOW())
                        """), {
                            "id": pid,
                            "name": proj.get("name", ""),
                            "desc": proj.get("description", ""),
                            "color": proj.get("color", "#6366f1"),
                            "icon": proj.get("icon", ""),
                            "llm": json.dumps(proj.get("llm_config", {})),
                            "gates": json.dumps(proj.get("quality_gates", {})),
                            "integ": json.dumps(integ),
                        })
                        log.info("Synced project '%s' (%s) to database", proj.get("name"), pid)
                    # M2 — persist/BACKFILL env config as a SEPARATE, individually-guarded
                    # statement (so a pending 013 migration can't break the base sync).
                    # Fills the DB column only when it is EMPTY — never overwrites a live
                    # present in the file but never in the DB) reaches Postgres on next
                    # startup WITHOUT re-minting. Killswitch ARTA_ENV_DB_PERSIST_DISABLE=1.
                    if os.environ.get("ARTA_ENV_DB_PERSIST_DISABLE") != "1" and proj.get("environments"):
                        try:
                            await db.execute(text("""
                                UPDATE projects SET environments = CAST(:envs AS jsonb), updated_at = NOW()
                                WHERE id = :id AND (environments IS NULL OR environments = '{}'::jsonb)
                            """), {"id": pid, "envs": json.dumps(proj.get("environments", {}))})
                        except Exception as _env_bf_exc:
                            log.debug("M2: environments backfill skipped for %s: %s", pid, _env_bf_exc)
    except Exception as exc:
        log.warning("Could not sync projects to DB: %s", exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/providers")
async def list_providers():
    """Return all LLM providers with their available model presets."""
    return {"providers": PROVIDER_PRESETS}


@router.get("", dependencies=[Depends(_require_api_key)])
async def list_projects(request: Request):
    """List projects with masked API keys. Merges DB + in-memory projects.

    RBAC read-scoping: a non-admin user sees only the projects they hold a role on.
    Machine/CI (shared API key) and platform admins see everything (`accessible=None`).
    """
    from ..db_adapter import try_db
    from ..dependencies import accessible_project_ids

    async with try_db() as db:
        if db:
            accessible = await accessible_project_ids(request, db)   # None = unrestricted
            try:
                from ...db.repository import ProjectRepo, _to_dict
                repo = ProjectRepo(db)
                rows = await repo.list()
                db_projects = {str(p.id): _mask(_to_dict(p)) for p in rows}
            except Exception:
                db_projects = {}
            # Merge in-memory projects (from .arta/projects.json) — in-memory has auth/env config
            for pid, proj in _PROJECTS.items():
                if pid in db_projects:
                    # Deep-merge: in-memory environment/auth config overrides empty DB fields
                    db_proj = db_projects[pid]
                    masked_mem = _mask(proj)
                    for field in ("environments", "integrations", "quality_gates", "llm_config"):
                        mem_val = masked_mem.get(field)
                        db_val = db_proj.get(field)
                        if mem_val and (not db_val or db_val == {} or db_val == []):
                            db_proj[field] = mem_val
                else:
                    db_projects[pid] = _mask(proj)
            if accessible is not None:
                db_projects = {pid: v for pid, v in db_projects.items() if pid in accessible}
            all_projects = list(db_projects.values())
            return {"projects": all_projects, "total": len(all_projects)}

    return {
        "projects": [_mask(p) for p in _PROJECTS.values()],
        "total": len(_PROJECTS),
    }


@router.post("", dependencies=[Depends(_require_api_key)])
async def create_project(body: ProjectCreate):
    """Create a new project with LLM configuration."""

    # Validate provider
    if body.llm_config.provider not in PROVIDER_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider '{body.llm_config.provider}'. "
                   f"Valid: {list(PROVIDER_PRESETS.keys())}",
        )

    # R127.A — validate each tool override's provider too (same rule as PUT).
    for tool_name, override in (body.llm_config.tool_overrides or {}).items():
        if override.provider and override.provider not in PROVIDER_PRESETS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown provider '{override.provider}' in "
                    f"tool_overrides['{tool_name}'] for project create."
                ),
            )

    from ..db_adapter import try_db

    async with try_db() as db:
        if db:
            from ...db.repository import ProjectRepo, _to_dict
            repo = ProjectRepo(db)
            p = await repo.create({
                "name": body.name,
                "description": body.description,
                "color": body.color,
                "icon": body.icon,
                "llm_config": body.llm_config.model_dump(),
                "quality_gates": body.quality_gates.model_dump(),
                "integrations": body.integrations.model_dump(),
                # M2 — persist env config durably (killswitch ARTA_ENV_DB_PERSIST_DISABLE=1).
                **({"environments": {k: v.model_dump() for k, v in body.environments.items()}}
                   if os.environ.get("ARTA_ENV_DB_PERSIST_DISABLE") != "1" else {}),
            })
            project_dict = _to_dict(p)
            project_dict["project_type"] = body.project_type
            project_dict["environments"] = {k: v.model_dump() for k, v in body.environments.items()}
            project_dict.setdefault("requirement_count", 0)
            project_dict.setdefault("test_count", 0)
            project_dict.setdefault("coverage_pct", 0.0)
            project_dict.setdefault("open_defects", 0)
            project_dict.setdefault("last_run_status", None)
            _PROJECTS[str(project_dict["id"])] = project_dict
            _save_projects()
            from ...telemetry import bucket as _tel_bucket, emit as _tel_emit
            _tel_emit("project.created", {"count_bucket": _tel_bucket(len(_PROJECTS))})
            return _mask(project_dict)

    # Mock fallback
    project_id = str(uuid.uuid4())
    now = "2026-03-11T00:00:00Z"
    project = {
        "id": project_id, "name": body.name, "description": body.description,
        "color": body.color, "icon": body.icon, "project_type": body.project_type,
        "llm_config": body.llm_config.model_dump(),
        "quality_gates": body.quality_gates.model_dump(),
        "integrations": body.integrations.model_dump(),
        "environments": {k: v.model_dump() for k, v in body.environments.items()},
        "requirement_count": 0, "test_count": 0, "coverage_pct": 0.0,
        "open_defects": 0, "last_run_status": None,
        "created_at": now, "updated_at": now,
    }
    _PROJECTS[project_id] = project
    _save_projects()
    from ...telemetry import bucket as _tel_bucket, emit as _tel_emit
    _tel_emit("project.created", {"count_bucket": _tel_bucket(len(_PROJECTS))})
    return _mask(project)


@router.get("/{project_id}", dependencies=[Depends(_require_api_key)])
async def get_project(project_id: str):
    """Get project detail. API key required."""
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return _mask(project)


@router.put("/{project_id}", dependencies=[Depends(_require_api_key)])
async def update_project(project_id: str, body: ProjectUpdate):
    """Update project name, description, color, icon, or LLM config."""
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    if body.name        is not None: project["name"]        = body.name
    if body.description is not None: project["description"] = body.description
    if body.color       is not None: project["color"]       = body.color
    if body.icon        is not None: project["icon"]        = body.icon

    if body.llm_config is not None:
        if body.llm_config.provider not in PROVIDER_PRESETS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider '{body.llm_config.provider}'.",
            )
        # R127.A — validate each tool override's provider against PROVIDER_PRESETS.
        # When provider is empty, the override is treated as a partial inherit
        # (only model/api_key etc. may differ from base) — that's allowed and
        # resolve_tool_config handles it. Non-empty providers MUST be valid.
        for tool_name, override in (body.llm_config.tool_overrides or {}).items():
            if override.provider and override.provider not in PROVIDER_PRESETS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unknown provider '{override.provider}' in "
                        f"tool_overrides['{tool_name}']."
                    ),
                )
        updated = body.llm_config.model_dump()
        # Preserve existing api_key if the update sends empty string (UI mask → no change)
        if not updated["api_key"] and project["llm_config"].get("api_key"):
            updated["api_key"] = project["llm_config"]["api_key"]
        # R127.A — preserve api_keys inside tool_overrides the same way
        # (UI masks override api_keys with "" if operator didn't touch them).
        existing_overrides = (project["llm_config"] or {}).get("tool_overrides") or {}
        for tool_name, override in (updated.get("tool_overrides") or {}).items():
            existing_override = existing_overrides.get(tool_name) or {}
            existing_key = existing_override.get("api_key") or ""
            if not override.get("api_key") and existing_key:
                override["api_key"] = existing_key
        project["llm_config"] = updated

    if body.quality_gates is not None:
        project["quality_gates"] = body.quality_gates.model_dump()

    if body.integrations is not None:
        updated_integ = body.integrations.model_dump()
        existing_integ = project.get("integrations", {})
        # Merge: keep existing keys not in update, overwrite only non-empty values
        merged = {**existing_integ}
        for k, v in updated_integ.items():
            if v or isinstance(v, list):
                merged[k] = v
        # Preserve tokens when update sends empty string (UI mask → no change)
        for token_key in ("github_token", "jira_token", "slack_webhook"):
            if not merged.get(token_key) and existing_integ.get(token_key):
                merged[token_key] = existing_integ[token_key]
        project["integrations"] = merged

    if body.environments is not None:
        # M1 — durable per-env MERGE (not whole-env replace) so a partial or UI save
        # can NEVER clobber a solved SUT auth. For each provided env it preserves:
        #       refresh_token=api_key live here) — deletion is done via the targeted
        #       PUT /environments/{env}/variables endpoint, not this whole-env PUT;
        #   (b) sensitive `credentials` when the body sends empty OR the mask sentinel
        #       "***" (GET returns masked secrets; a UI GET→edit→PUT echoes them);
        #   (c) schema-less nested auth.* (chain/host_map/refresh/login/login_flows)
        #       that pydantic model_dump() drops (EnvironmentAuth models only
        #       method+credentials).
        # Runs per provided env, so masked SIBLING envs the operator didn't touch are
        # protected too. Server-authoritative → immune to any partial client PUT.
        # Killswitch ARTA_ENV_MERGE_DISABLE=1 reverts to the pre-M1 falsy-only preserve.
        existing_envs = project.get("environments", {}) or {}
        _env_merge = os.environ.get("ARTA_ENV_MERGE_DISABLE") != "1"
        updated_envs = {}
        for env_name, env_cfg in body.environments.items():
            env_data = env_cfg.model_dump()
            old_env = existing_envs.get(env_name, {}) if isinstance(existing_envs, dict) else {}
            if not (_env_merge and isinstance(old_env, dict) and old_env):
                # Legacy behavior (killswitch on, or no prior env): falsy-only preserve.
                old_creds = old_env.get("auth", {}).get("credentials", {}) if isinstance(old_env, dict) else {}
                new_creds = env_data.get("auth", {}).get("credentials", {})
                for key in _SENSITIVE_CRED_KEYS:
                    if key in new_creds and not new_creds[key] and old_creds.get(key):
                        new_creds[key] = old_creds[key]
                updated_envs[env_name] = env_data
                continue
            old_auth = old_env.get("auth") if isinstance(old_env.get("auth"), dict) else {}
            new_auth = env_data.get("auth") if isinstance(env_data.get("auth"), dict) else {}
            # (c) preserve nested auth.* dropped by model_dump; incoming method/blocks win
            merged_auth = {**old_auth, **new_auth}
            # (b) credentials: preserve real secret when incoming is falsy OR "***"
            old_creds = old_auth.get("credentials") if isinstance(old_auth.get("credentials"), dict) else {}
            new_creds = new_auth.get("credentials") if isinstance(new_auth.get("credentials"), dict) else {}
            merged_creds = dict(old_creds)
            for k, v in new_creds.items():
                if k in _SENSITIVE_CRED_KEYS and (not v or v == _MASK_SENTINEL) and old_creds.get(k):
                    continue  # keep the existing real secret
                merged_creds[k] = v
            merged_auth["credentials"] = merged_creds
            env_data["auth"] = merged_auth
            # (a) variables: merge — never drop keys the body omits
            old_vars = old_env.get("variables") if isinstance(old_env.get("variables"), dict) else {}
            new_vars = env_data.get("variables") if isinstance(env_data.get("variables"), dict) else {}
            env_data["variables"] = {**old_vars, **new_vars}
            updated_envs[env_name] = env_data
        # Preserve envs the body did NOT include — a partial PUT of one env must not
        # drop the others (this is what lets the UI send only the edited env).
        project["environments"] = ({**existing_envs, **updated_envs}
                                   if _env_merge else updated_envs)

    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_projects()

    # Persist to DB
    try:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db:
                from ...db.repository import ProjectRepo
                repo = ProjectRepo(db)
                _upd = {
                    "name": project.get("name"),
                    "description": project.get("description"),
                    "color": project.get("color"),
                    "icon": project.get("icon"),
                    "llm_config": project.get("llm_config"),
                    "quality_gates": project.get("quality_gates"),
                    "integrations": project.get("integrations"),
                }
                # M2 — persist env config durably so a lost projects.json doesn't
                # erase a SUT's solved auth. Killswitch ARTA_ENV_DB_PERSIST_DISABLE=1.
                if os.environ.get("ARTA_ENV_DB_PERSIST_DISABLE") != "1":
                    _upd["environments"] = project.get("environments") or {}
                await repo.update(project_id, _upd)
    except Exception:
        pass  # DB write is best-effort; file is backup

    return _mask(project)


@router.delete("/{project_id}", dependencies=[Depends(_require_api_key)])
async def delete_project(project_id: str):
    """Delete a project. This also removes all associated data."""
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # F6-11: Cancel any in-flight Generate-All jobs for this project so we
    # don't leave orphaned background tasks writing to a deleted project.
    cancelled_jobs: list[str] = []
    try:
        from .tests import _GENERATE_ALL_JOBS  # type: ignore
        for jid, j in list(_GENERATE_ALL_JOBS.items()):
            if j.get("project_id") == project_id and j.get("status") in (
                "queued", "running",
            ):
                j["_abort_requested"] = True
                cancelled_jobs.append(jid)
        if cancelled_jobs:
            log.info("delete_project[%s]: signalled abort to %d in-flight job(s): %s",
                     project_id, len(cancelled_jobs), cancelled_jobs)
            # Best-effort short wait so the loop honours the abort before we drop
            # the project. The loop checks _abort_requested at requirement boundaries.
            import asyncio as _aio
            await _aio.sleep(0.5)
    except Exception as exc:
        log.warning("delete_project[%s]: job-cancel sweep failed: %s", project_id, exc)

    del _PROJECTS[project_id]
    _save_projects()
    return {
        "message": f"Project {project_id} deleted",
        "status": "ok",
        "cancelled_jobs": cancelled_jobs,
    }


@router.get("/{project_id}/summary", dependencies=[Depends(_require_api_key)])
async def project_summary(project_id: str):
    """Quick summary: coverage, defects, last run for dashboard tile."""
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    return {
        "project_id":      project_id,
        "name":            project["name"],
        "coverage_pct":    project["coverage_pct"],
        "open_defects":    project["open_defects"],
        "test_count":      project["test_count"],
        "last_run_status": project["last_run_status"],
    }


def _r130_j_compute_carry_rate(project_tests: list[dict]) -> dict:
    """R130.J — qwen-pro carry-rate score over the project's recent gen
    history (last 50 test rows). Surfaces a single number that tells the
    operator whether the configured Ollama model is carrying its weight.

    Pre-R130.J: operator could only inspect anecdotal evidence (the
    dashboard tiles for escalations + gen_quality). Post-R130.J: a clear
    `pivot_recommendation` enum drives 3 operator CTAs when qwen-pro
    under-performs:
      - "ok": carry-rate ≥70% — green badge
      - "investigate": pass-rate <70% but escalation <30% — amber
      - "consider_upgrade": pass-rate <50% AND escalation >30% — red,
        with operator pivot CTAs (per-tool override / larger Ollama /
        per-req escalation log review)

    NEVER auto-upgrades the model — operator decides per ARTA's
    mission contract ("make the configured model carry its weight").
    """
    if not project_tests:
        return {
            "carry_rate_pct": None, "pass_rate_pct": None,
            "escalation_rate_pct": None, "samples": 0,
            "ollama_model": "unknown", "pivot_recommendation": "no_data",
        }
    recent = project_tests[-50:]
    total = len(recent)
    escalated = sum(
        1 for t in recent
        if (t.get("_gen_metrics") or {}).get("r127_c_escalated")
        or (t.get("_gen_metrics") or {}).get("r127_c_subs_escalated", 0) > 0
    )
    passed = sum(
        1 for t in recent
        if t.get("generation_source") == "llm"
        and not t.get("generation_failure")
        and "playwright_grounding_violation" not in str(
            (t.get("_gen_metrics") or {})
        )
    )
    carry_rate = ((total - escalated) / total) * 100.0 if total else 0.0
    pass_rate = (passed / total) * 100.0 if total else 0.0
    escalation_rate = (escalated / total) * 100.0 if total else 0.0
    # Pivot recommendation logic — operator-action surface
    if pass_rate < 50.0 and escalation_rate > 30.0:
        rec = "consider_upgrade"
    elif pass_rate < 70.0:
        rec = "investigate"
    else:
        rec = "ok"
    # Extract Ollama model name from most recent test row's _gen_metrics
    _last_llm = (recent[-1].get("_gen_metrics") or {}).get("llm") or {}
    return {
        "carry_rate_pct":        round(carry_rate, 1),
        "pass_rate_pct":         round(pass_rate, 1),
        "escalation_rate_pct":   round(escalation_rate, 1),
        "samples":               total,
        "ollama_model":          str(_last_llm.get("model", "unknown")),
        "pivot_recommendation":  rec,
    }


async def _r130_j_compute_with_cache(
    project_tests: list[dict], project_id: str,
) -> dict:
    """R130.J safeguard #2 — TTL cache to prevent recomputation on every
    gen-health poll. Dashboard typically polls every 5s; without cache,
    walking 50 rows × 12 polls/min = wasteful. 30-second TTL keyed by
    (project_id, last_test_id) — cache invalidates automatically when a
    new test row lands.

    Falls through to direct computation when Redis unavailable + memory
    cache is also empty (graceful degradation).
    """
    if not project_tests:
        return _r130_j_compute_carry_rate(project_tests)
    try:
        from ...observability.cache import cache as _r130_j_cache
        last_test_id = project_tests[-1].get("id", "")
        cache_key = f"r130_j:carry_rate:{project_id}:{last_test_id}"
        cached = await _r130_j_cache.get(cache_key)
        if cached is not None:
            return cached
        fresh = _r130_j_compute_carry_rate(project_tests)
        await _r130_j_cache.set(cache_key, fresh, ttl_seconds=30)
        return fresh
    except Exception:
        # Defensive: cache failure must NEVER block the dashboard
        return _r130_j_compute_carry_rate(project_tests)


async def _r141_c_compute_defect_classification_health(project_id: str) -> dict:
    """R141.C — surface defect classifier health on the gen-health
    dashboard tile. Walks the project's last 5 runs and flags any where
    fail_count > 50 AND defect_count == 0 (a "stalled" run — R141.A.0
    diagnostic + R141.B fallback both failed to produce defects).

    Pre-R141.C: R135 evidence (Iter 0+1) showed 522+224 failures producing
    0 defects across 2 iters with ZERO operator-visible signal. R141.B's
    blanket-defect path now guarantees ≥1 defect per failures batch even
    on classifier outage, but the operator should also see the trend so
    they can investigate the upstream LLM cascade state.

    Returns:
      {
        "last_5_runs": [{run_id, fail_count, defect_count, stalled}, ...],
        "stalled_count": K,
        "alert": K >= 2,
        "recommendation": str | None,
      }

    Failure mode: when DB unavailable, returns a safe `degraded: True`
    payload so the dashboard renders cleanly + the operator knows the
    signal is missing.
    """
    safe_default = {
        "last_5_runs": [],
        "stalled_count": 0,
        "alert": False,
        "recommendation": None,
        "degraded": False,
    }
    try:
        from ..db_adapter import try_db
        async with try_db() as db:
            if db is None:
                return {**safe_default, "degraded": True,
                        "degraded_reason": "db_unavailable"}
            from sqlalchemy import text
            rows = (await db.execute(
                text(
                    "SELECT tr.run_id, tr.failed AS fail_count, "
                    "       (SELECT COUNT(*) FROM defects d "
                    "        WHERE d.run_id = tr.id) AS defect_count "
                    "FROM test_runs tr "
                    "WHERE tr.project_id = CAST(:pid AS uuid) "
                    "  AND tr.completed_at IS NOT NULL "
                    "ORDER BY tr.completed_at DESC LIMIT 5"
                ),
                {"pid": project_id},
            )).mappings().all()
    except Exception as exc:
        log.debug("R141.C defect-health query skipped: %s", exc)
        return {**safe_default, "degraded": True,
                "degraded_reason": f"{type(exc).__name__}"}

    last_5: list[dict] = []
    for r in rows:
        fail_count = int(r.get("fail_count") or 0)
        defect_count = int(r.get("defect_count") or 0)
        stalled = (fail_count > 50 and defect_count == 0)
        last_5.append({
            "run_id": str(r.get("run_id") or ""),
            "fail_count": fail_count,
            "defect_count": defect_count,
            "stalled": stalled,
        })
    stalled_count = sum(1 for r in last_5 if r["stalled"])
    alert = stalled_count >= 2
    recommendation = (
        "Defect classification LLM stalled across recent runs — check "
        "Claude CLI auth/rate + bump ARTA_CLAUDE_CLI_MAX_TURNS to 3+. "
        "R141.A.0 diagnostic logs (arta-api container) show the exact "
        "stall mode (turn-exhaust vs timeout vs rate-limit). R141.D "
        "backoff prevents thrash on transient 429s; raise "
        "ARTA_R141_D_MAX_ATTEMPTS if the burst window is wider."
    ) if alert else None
    return {
        "last_5_runs": last_5,
        "stalled_count": stalled_count,
        "alert": alert,
        "recommendation": recommendation,
    }


async def _r141_c_compute_with_cache(project_id: str) -> dict:
    """R141.C TTL-cached wrapper — mirrors R130.J Safeguard D pattern.
    30-second TTL keyed by (project_id, last_test_run_id). Prevents
    recomputation on dashboard poll storms during operator wait periods.

    Falls through to direct compute when cache unavailable.
    """
    try:
        from ...observability.cache import cache as _r141_c_cache
        cache_key = f"r141_c:defect_health:{project_id}"
        cached = await _r141_c_cache.get(cache_key)
        if cached is not None:
            return cached
        fresh = await _r141_c_compute_defect_classification_health(project_id)
        await _r141_c_cache.set(cache_key, fresh, ttl_seconds=30)
        return fresh
    except Exception:
        return await _r141_c_compute_defect_classification_health(project_id)


def _r145_f_load_bridge_trace_history(
    project_id: str,
    *,
    limit: int = 10,
    since_hours: int = 168,
) -> list[dict]:
    """R145.F — walk `.arta/runs/*/r145_c_bridge_trace.jsonl` and aggregate
    per-run delivery state for this project.

    Reads the SAME sidecar format R145.C wrote — single source of truth,
    no duplicate state. Best-effort: missing/malformed sidecars are
    skipped silently with log.debug. Drift detection fires when the most
    recent run's delivery_break_point differs from the prior consistent
    state across the last ≥3 runs.

    Returns:
      {entries: [{run_id, started_at, delivery_break_point, sut_host,
                  chromium_saw_launch_arg, subprocess_saw_env_var}],
       drift_detected: bool,
       drift_message: str | None}
    """
    import time as _t_r145f
    try:
        from .execution import _r145_c_load_bridge_trace, _r145_c_summarize_bridge_trace
    except Exception as exc:
        log.debug("R145.F: execution import skipped: %s", exc)
        return {"entries": [], "drift_detected": False, "drift_message": None}
    cutoff = _t_r145f.time() - (since_hours * 3600)
    runs_root = Path(".arta/runs")
    if not runs_root.is_dir():
        return {"entries": [], "drift_detected": False, "drift_message": None}
    candidates: list[tuple[float, str, list[dict]]] = []
    try:
        for run_dir in runs_root.iterdir():
            if not run_dir.is_dir():
                continue
            sidecar = run_dir / "r145_c_bridge_trace.jsonl"
            if not sidecar.exists():
                continue
            try:
                mtime = sidecar.stat().st_mtime
            except Exception:
                continue
            if mtime < cutoff:
                continue
            events = _r145_c_load_bridge_trace(run_dir.name)
            if not events:
                continue
            # Filter to this project_id when known. The first event with
            # project_id keys is project_id_stamped (site 1).
            owner = None
            for ev in events:
                if isinstance(ev, dict) and ev.get("project_id"):
                    owner = ev["project_id"]
                    break
            if owner and owner != project_id:
                continue
            candidates.append((mtime, run_dir.name, events))
    except Exception as exc:
        log.debug("R145.F: history walk skipped: %s", exc)
    candidates.sort(key=lambda t: t[0], reverse=True)
    entries: list[dict] = []
    for mtime, run_id, events in candidates[:limit]:
        summary = _r145_c_summarize_bridge_trace(events)
        sut_host = None
        for ev in events:
            if ev.get("event") == "r143_d_state_stamped":
                sut_host = ev.get("sut_host")
                break
        entries.append({
            "run_id": run_id,
            "started_at": (events[0] or {}).get("ts") if events else None,
            "mtime": mtime,
            "delivery_break_point": summary.get("delivery_break_point"),
            "sut_host": sut_host,
            "chromium_saw_launch_arg": summary.get("chromium_saw_launch_arg"),
            "subprocess_saw_env_var":  summary.get("subprocess_saw_env_var"),
        })
    drift_detected = False
    drift_message = None
    if len(entries) >= 4:
        latest = entries[0].get("delivery_break_point")
        prior_window = [e.get("delivery_break_point") for e in entries[1:4]]
        prior_consistent = (
            len(set(prior_window)) == 1 and prior_window[0] is not None
        )
        if prior_consistent and latest != prior_window[0]:
            drift_detected = True
            drift_message = (
                f"R145.F: bridge delivery regressed — latest run="
                f"{latest!r}, prior 3 consecutive runs="
                f"{prior_window[0]!r}. Inspect dispatcher env "
                "propagation; R145.C sidecar at "
                f".arta/runs/{entries[0]['run_id']}/r145_c_bridge_trace.jsonl"
            )
    return {
        "entries":         entries,
        "drift_detected":  drift_detected,
        "drift_message":   drift_message,
    }


def _r146_a_latest_run_id_for_project(project_id: str) -> str | None:
    """R146.A — find most recent run_id for this project by walking
    `.arta/runs/*/r145_c_bridge_trace.jsonl` sidecars in mtime order.
    Best-effort: returns None when no per-run sidecars exist."""
    try:
        runs_root = Path(".arta/runs")
        if not runs_root.is_dir():
            return None
        candidates: list[tuple[float, str]] = []
        for d in runs_root.iterdir():
            if not d.is_dir():
                continue
            sidecar = d / "r145_c_bridge_trace.jsonl"
            if not sidecar.exists():
                continue
            try:
                first_line = (
                    sidecar.read_text(encoding="utf-8").split("\n", 1)[0] or ""
                )
                rec = json.loads(first_line) if first_line.strip() else {}
                if rec.get("project_id") and rec["project_id"] != project_id:
                    continue
                candidates.append((sidecar.stat().st_mtime, d.name))
            except Exception:
                continue
        candidates.sort(reverse=True)
        return candidates[0][1] if candidates else None
    except Exception as exc:
        log.debug("R146.A: latest_run_id lookup failed: %s", exc)
        return None


def _r146_a_summary_a_aggregate(project_id: str, limit: int = 10) -> dict:
    """R146.A — aggregate R145.A audit data across the project's last
    `limit` runs. Reads `.arta/audit/r145_a3_autopurge.jsonl` for the
    auto-purge sweep counts; aggregates `total_items_scanned`,
    `total_items_substituted`, `total_items_blocked` for entries matching
    this project_id."""
    audit_path = Path(".arta/audit/r145_a3_autopurge.jsonl")
    if not audit_path.exists():
        return {
            "total_items_scanned":     0,
            "total_items_substituted": 0,
            "total_items_blocked":     0,
            "audit_entries":           0,
            "triggers_seen":           [],
        }
    entries = []
    try:
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("project_id") != project_id:
                continue
            entries.append(rec)
    except Exception as exc:
        log.debug("R146.A: audit aggregate failed: %s", exc)
        return {
            "total_items_scanned":     0,
            "total_items_substituted": 0,
            "total_items_blocked":     0,
            "audit_entries":           0,
            "triggers_seen":           [],
        }
    entries = entries[-limit:]
    return {
        "total_items_scanned":     sum(int(e.get("newman_files_scanned") or 0) for e in entries),
        "total_items_substituted": sum(int(e.get("items_substituted") or 0) for e in entries),
        "total_items_blocked":     sum(int(e.get("items_blocked") or 0) for e in entries),
        "audit_entries":           len(entries),
        "triggers_seen":           sorted({str(e.get("trigger") or "") for e in entries if e.get("trigger")}),
    }


def _r146_a_summary_b_aggregate(project_id: str) -> dict:
    """R146.A — count auth_bypass-bypassed AC + scenarios for this project.
    Reads GENERATED_TESTS stamps `_r145_b_bypassed_count` (per-row).
    Falls back to 0 when no rows carry the stamp."""
    try:
        from .tests import GENERATED_TESTS
        bypassed_total = 0
        rows_seen = 0
        for t in GENERATED_TESTS:
            if t.get("project_id") != project_id:
                continue
            meta = t.get("metadata") or {}
            gen_metrics = (meta.get("_gen_metrics") or {})
            count = int(gen_metrics.get("_r145_b_bypassed_count") or 0)
            if count > 0:
                bypassed_total += count
                rows_seen += 1
        return {
            "bypassed_total":  bypassed_total,
            "rows_with_stamp": rows_seen,
        }
    except Exception as exc:
        log.debug("R146.A: summary_b aggregate failed: %s", exc)
        return {"bypassed_total": 0, "rows_with_stamp": 0}


def _r146_a_summary_c_latest_run(project_id: str) -> dict:
    """R146.A — load + summarize the per-current-run R145.C bridge trace
    sidecar. Returns empty-shaped defaults when no sidecar found."""
    try:
        from .execution import (
            _r145_c_load_bridge_trace, _r145_c_summarize_bridge_trace,
        )
        latest = _r146_a_latest_run_id_for_project(project_id)
        if not latest:
            return _r145_c_summarize_bridge_trace([])  # empty-shaped defaults
        events = _r145_c_load_bridge_trace(latest)
        summary = _r145_c_summarize_bridge_trace(events)
        summary["run_id"] = latest
        return summary
    except Exception as exc:
        log.debug("R146.A: summary_c latest_run failed: %s", exc)
        return {}


def _r146_a_summary_d_cascade(project_id: str) -> dict:
    """R146.A — cascade-skip aggregator for the project's most-recent run.
    Reads execution_results from DB matching the latest run; returns
    cascade_skips / cascade_ratio / top_cascade_specs."""
    try:
        from .execution import (
            _r144_d_compute_skip_cascade, _REAL_RESULTS,
        )
        latest = _r146_a_latest_run_id_for_project(project_id)
        if not latest:
            return {}
        all_results = _REAL_RESULTS.get(latest) or []
        pw_total = sum(
            1 for r in all_results
            if r.get("automation_tool") == "playwright"
        )
        summary = _r144_d_compute_skip_cascade(all_results, pw_total)
        # Project only the cascade-relevant keys per R146.A scope; auth-stale
        # + by_cause stays in R144.D dashboard tile.
        return {
            "run_id":             latest,
            "cascade_skips":      summary.get("cascade_skips", 0),
            "cascade_ratio":      summary.get("cascade_ratio", 0.0),
            "top_cascade_specs":  summary.get("top_cascade_specs", []),
        }
    except Exception as exc:
        log.debug("R146.A: summary_d cascade failed: %s", exc)
        return {}


def _r156_d_compute_source_grounding_summary(project_id: str) -> dict:
    """R156.D — operator-visible summary of R156 source-code grounding
    coverage. Surfaces:

      - token_chain_depth: number of hops in TOKEN_CHAINS (example
        default is 4: session_token → agent_token → aws_temp_creds /
        s3_presigned)
      - refresh_flow_present: bool — whether R156.I.2 attached a
        refresh_flow to agent_token (closes >60min smoke TTL gap)
      - protocol_coverage: counts of REST / gRPC / SSE / WebSocket /
        GraphQL endpoints detected by R156.C across the project's
        cached source-extraction state. When extraction hasn't run
        yet, returns 'rest: 0' across the board (truthful cold-start).
      - operator_actions: list of operator-actionable next steps
        derived from the coverage gaps (e.g., "set
        agent_token.refresh_flow when SUT supports it" when the
        chain hasn't been populated).

    Reads from `_R156_B_1_TOKEN_CHAINS` class constant + on-disk
    extraction cache at `.arta/discovery/<pid>/r156_protocol_audit.json`
    when present (future R156 work persists the audit here).

    Killswitch: `ARTA_R156_D_DASHBOARD_DISABLE=1` → returns
    `{"disabled": True}`. Default-off so dashboard always renders.
    """
    if os.environ.get("ARTA_R156_D_DASHBOARD_DISABLE") == "1":
        return {"disabled": True}
    try:
        from src.agents.automation_engineer import AutomationEngineerAgent
        chains = getattr(
            AutomationEngineerAgent, "_R156_B_1_TOKEN_CHAINS", {}
        ) or {}
        # Token chain depth — count hops from `from` chain. Walks
        # backward from each leaf to count the longest chain.
        def _depth(kind: str, visited: set | None = None) -> int:
            visited = visited or set()
            if kind in visited:
                return 0
            visited.add(kind)
            entry = chains.get(kind)
            if not isinstance(entry, dict):
                return 1
            parent = entry.get("from")
            if not parent:
                return 1
            return 1 + _depth(parent, visited)
        chain_depth = max((_depth(k) for k in chains.keys()), default=0)
        # Refresh-flow presence — check BOTH the class-default chain
        # AND the per-project env_block override (set by operator via
        # `env_block.variables.arta_refresh_endpoint` per R156.J.3
        # propagation contract). Reports True when EITHER source has
        # a refresh endpoint configured, so operators who customize
        # per-project see the truthful coverage state.
        agent_entry = chains.get("agent_token") or {}
        refresh_flow = agent_entry.get("refresh_flow") if isinstance(agent_entry, dict) else None
        chain_has_refresh_flow = bool(
            refresh_flow
            and isinstance(refresh_flow, dict)
            and (refresh_flow.get("endpoint") or refresh_flow.get("endpoint_path"))
        )
        # Per-project override: scan _PROJECTS for an env_block with
        # `arta_refresh_endpoint`. Mirror R156.J.3 dispatcher's lookup
        # (priority: per-project override > class default).
        project_has_refresh_override = False
        refresh_flow_source = "none"
        try:
            project = _PROJECTS.get(project_id) or {}
            envs = (project.get("environments") or {})
            for _env_name, _env_cfg in envs.items():
                _vars = (_env_cfg.get("variables") or {}) if isinstance(_env_cfg, dict) else {}
                if _vars.get("arta_refresh_endpoint") or _vars.get("ARTA_REFRESH_ENDPOINT"):
                    project_has_refresh_override = True
                    break
        except Exception:
            pass  # cold-start project; override stays False
        refresh_flow_present = chain_has_refresh_flow or project_has_refresh_override
        if project_has_refresh_override:
            refresh_flow_source = "project_override"
        elif chain_has_refresh_flow:
            refresh_flow_source = "class_default"
        # Protocol coverage from cached audit (when present)
        protocol_coverage = {"rest": 0, "grpc": 0, "sse": 0, "websocket": 0, "graphql": 0}
        audit_path = Path(".arta/discovery") / project_id / "r156_protocol_audit.json"
        try:
            if audit_path.is_file():
                audit_data = json.loads(audit_path.read_text(encoding="utf-8"))
                counts = (audit_data or {}).get("counts") or {}
                for kind, n in counts.items():
                    if kind in protocol_coverage and isinstance(n, int):
                        protocol_coverage[kind] = n
        except Exception:
            pass  # cold-start; counts stay 0
        # Operator-actionable next-steps. Each step is a short string;
        # the dashboard renders them as a checklist below the tile.
        operator_actions: list[str] = []
        if not refresh_flow_present:
            operator_actions.append(
                "R156.I.2: agent_token has no refresh_flow attached. "
                "When SUT exposes a refresh endpoint, set the project's "
                "env_block.variables.arta_refresh_endpoint OR run "
                "discovery → extraction to auto-populate from source. "
                "Closes the >60min smoke TTL gap (R156.J auto-refresh)."
            )
        if protocol_coverage["sse"] + protocol_coverage["websocket"] + protocol_coverage["grpc"] > 0:
            operator_actions.append(
                "R156.C: non-REST endpoints detected. R156.E/G/H gen "
                "pipelines must be enabled to generate functional tests "
                "for these (default OFF until operator opts in)."
            )
        return {
            "token_chain_depth": chain_depth,
            "refresh_flow_present": refresh_flow_present,
            # R156.D.2 — surface refresh_flow_source so operator can
            # see WHETHER the class default is sufficient OR they need
            # a per-project override (env_block.variables.arta_refresh_endpoint)
            "refresh_flow_source": refresh_flow_source,
            "protocol_coverage": protocol_coverage,
            "operator_actions": operator_actions,
            "source_extraction_audit_cached": audit_path.is_file(),
        }
    except Exception as exc:
        log.debug("R156.D: source grounding summary failed: %s", exc)
        return {"error": str(exc)[:200]}


@router.get("/{project_id}/gen-health", dependencies=[Depends(_require_api_key)])
async def project_gen_health(project_id: str):
    """R125.I — gen-quality dashboard tile per project.

    Surfaces:
      - Total test rows generated for this project
      - gen_source breakdown (llm vs fallback)
      - R125.E recipe-stage failure count + per-req drill-down
      - R125.K per-provider quality (provider/model/strategy distribution)
      - R125.M strategy_divergence stamps (operator-actionable CTAs)
      - List of reqs with gen_source=failed (for R125.J auto-rebuild)

    Per the user directive "performance/accuracy same across providers",
    this endpoint is THE comparison surface — operators can verify
    same-SUT-on-Claude vs on-Ollama side-by-side before/after
    R125.H provider switch.
    """
    from .tests import GENERATED_TESTS
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Filter GENERATED_TESTS to this project
    project_tests = [
        t for t in GENERATED_TESTS
        if t.get("project_id") == project_id
    ]

    # Aggregate
    total = len(project_tests)
    by_gen_source: dict[str, int] = {}
    by_provider: dict[str, dict] = {}  # provider → {count, models[set], strategies[set]}
    failed_reqs: dict[str, dict] = {}  # req_id → {reason, failed_at}
    strategy_divergence_count = 0

    # R127.C — aggregate escalation outcomes for the dashboard tile.
    # Per-script `_gen_metrics.r127_c_*` fields populated by
    # `_generate_playwright` at the quality-gated escalation site.
    r127_c_summary: dict = {
        "escalations_total":  0,
        "by_provider":        {},   # provider → count
        # R127.D.4 + R127.E.3 — outcomes keys include both the legacy
        # `capped` (pre-E.3 single-cap world) and `capped_batch` /
        # `capped_req` (R127.E.3 differentiated caps). Each is summed
        # independently so old + new metadata both surface.
        "outcomes":           {
            "passed": 0, "failed_quality_regression": 0,
            "failed_violations": 0, "failed_runtime": 0,
            "capped": 0, "capped_batch": 0, "capped_req": 0,
        },
        "avg_baseline_score": None,
        "_baseline_score_sum": 0.0,  # internal accumulator
    }
    r127_b_summary: dict = {
        "decomposed_count": 0,
        "by_tool":          {},   # tool → count of decomposed reqs
        "part_counts":      [],   # part_total for each decomposed req
    }
    # R128.A — shadow divergence aggregator. Compares deterministic contract
    # Newman vs LLM-mode Newman per req; surfaces drift as severity-bucketed
    # counts + top hallucinated-endpoint hotspots so operators see when
    # LLM gen materially deviates from the OpenAPI baseline.
    r128_a_summary: dict = {
        "samples_scored":      0,
        "severity_counts":     {"aligned": 0, "minor": 0, "material": 0, "severe": 0},
        "avg_overlap_pct":     None,
        "_overlap_sum":        0.0,    # internal accumulator
        "top_llm_only_endpoints":      [],  # hallucination hotspots across reqs
        "top_contract_only_endpoints": [],  # coverage gaps in LLM gen
        "_llm_only_freq":      {},     # internal accumulator
        "_contract_only_freq": {},     # internal accumulator
    }

    for t in project_tests:
        # gen_source breakdown
        gs = t.get("generation_source") or "unknown"
        by_gen_source[gs] = by_gen_source.get(gs, 0) + 1

        # R125.K per-provider tag
        gm = t.get("_gen_metrics") or {}
        llm = gm.get("llm") or {}
        provider = llm.get("provider", "unknown")
        model = llm.get("model", "unknown")
        strategy = llm.get("strategy", "unknown")
        bucket = by_provider.setdefault(provider, {
            "count": 0, "models": set(), "strategies": set(),
        })
        bucket["count"] += 1
        bucket["models"].add(model)
        bucket["strategies"].add(strategy)

        # R127.B decomposition aggregation
        if gm.get("r127_b_decomposed"):
            r127_b_summary["decomposed_count"] += 1
            tool_name = t.get("tool") or "unknown"
            r127_b_summary["by_tool"][tool_name] = (
                r127_b_summary["by_tool"].get(tool_name, 0) + 1
            )
            pt = gm.get("r127_b_part_total")
            if isinstance(pt, int):
                r127_b_summary["part_counts"].append(pt)

        # R127.C escalation aggregation — single-row monolithic pattern.
        # Each test row stamped with `r127_c_escalated=True` adds ONE
        # escalation event to the summary.
        if gm.get("r127_c_escalated"):
            r127_c_summary["escalations_total"] += 1
            esc_provider = str(gm.get("r127_c_escalation_provider", "unknown"))
            r127_c_summary["by_provider"][esc_provider] = (
                r127_c_summary["by_provider"].get(esc_provider, 0) + 1
            )
            outcome = gm.get("r127_c_escalation_outcome", "unknown")
            if outcome in r127_c_summary["outcomes"]:
                r127_c_summary["outcomes"][outcome] += 1
            baseline = gm.get("r127_c_baseline_score") or {}
            if isinstance(baseline, dict) and isinstance(baseline.get("aggregate"), (int, float)):
                r127_c_summary["_baseline_score_sum"] += float(baseline["aggregate"])

        # R127.D.4 — rollup pattern from R127.B-decomposed parents.
        # Each parent test row carries `r127_c_subs_escalated=N` +
        # `r127_c_subs_outcomes` + `r127_c_subs_providers` aggregating
        # per-sub R127.C events from the decomposition. Pre-R127.D.4 the
        # parent only had `r127_b_*` fields → dashboard escalation count
        # stayed at 0 even when N subs successfully escalated. The reader
        # adds N to escalations_total (one per sub) and apportions the
        # outcome distribution.
        if gm.get("r127_c_subs_escalated"):
            subs_n = int(gm.get("r127_c_subs_escalated") or 0)
            r127_c_summary["escalations_total"] += subs_n
            for outcome_key, count in (gm.get("r127_c_subs_outcomes") or {}).items():
                if outcome_key in r127_c_summary["outcomes"]:
                    try:
                        r127_c_summary["outcomes"][outcome_key] += int(count or 0)
                    except (TypeError, ValueError):
                        pass
            for prov in (gm.get("r127_c_subs_providers") or []):
                r127_c_summary["by_provider"][prov] = (
                    r127_c_summary["by_provider"].get(prov, 0) + subs_n
                )
            avg = gm.get("r127_c_subs_avg_baseline")
            if isinstance(avg, (int, float)):
                r127_c_summary["_baseline_score_sum"] += float(avg) * subs_n

        # R128.A — per-test shadow divergence rollup. Only Newman test
        # rows carry `r128_a_divergence` (stamped at tests.py contract-
        # merge site when BOTH deterministic + LLM Newman were generated
        # for the same req).
        _r128 = gm.get("r128_a_divergence")
        if isinstance(_r128, dict):
            r128_a_summary["samples_scored"] += 1
            sev = _r128.get("divergence_severity") or "aligned"
            if sev in r128_a_summary["severity_counts"]:
                r128_a_summary["severity_counts"][sev] += 1
            _ovl = _r128.get("overlap_pct")
            if isinstance(_ovl, (int, float)):
                r128_a_summary["_overlap_sum"] += float(_ovl)
            for ep in (_r128.get("llm_only_endpoints") or []):
                r128_a_summary["_llm_only_freq"][ep] = (
                    r128_a_summary["_llm_only_freq"].get(ep, 0) + 1
                )
            for ep in (_r128.get("contract_only_endpoints") or []):
                r128_a_summary["_contract_only_freq"][ep] = (
                    r128_a_summary["_contract_only_freq"].get(ep, 0) + 1
                )

        # Failed reqs (gen_source=failed OR generation_failure stamped)
        if gs == "failed" or t.get("generation_failure"):
            req_id = t.get("requirement_id", "?")
            if req_id not in failed_reqs:
                failed_reqs[req_id] = {
                    "reason": str(t.get("generation_failure") or "gen_source=failed")[:200],
                    "tool": t.get("tool", "?"),
                }

        # R125.M strategy_divergence — count tests stamped with the divergence marker
        if isinstance(t.get("generation_failure"), str) and "r125_m_strategy_divergence" in t["generation_failure"]:
            strategy_divergence_count += 1

    # Finalize R127.C avg baseline + clean up internal accumulator
    if r127_c_summary["escalations_total"] > 0:
        r127_c_summary["avg_baseline_score"] = round(
            r127_c_summary["_baseline_score_sum"] / r127_c_summary["escalations_total"], 3,
        )
    r127_c_summary.pop("_baseline_score_sum", None)

    # Finalize R128.A summary: average overlap, top-10 frequency lists
    if r128_a_summary["samples_scored"] > 0:
        r128_a_summary["avg_overlap_pct"] = round(
            r128_a_summary["_overlap_sum"] / r128_a_summary["samples_scored"], 1,
        )
    r128_a_summary["top_llm_only_endpoints"] = sorted(
        (
            {"endpoint": ep, "freq": freq}
            for ep, freq in r128_a_summary["_llm_only_freq"].items()
        ),
        key=lambda x: x["freq"],
        reverse=True,
    )[:10]
    r128_a_summary["top_contract_only_endpoints"] = sorted(
        (
            {"endpoint": ep, "freq": freq}
            for ep, freq in r128_a_summary["_contract_only_freq"].items()
        ),
        key=lambda x: x["freq"],
        reverse=True,
    )[:10]
    r128_a_summary.pop("_overlap_sum", None)
    r128_a_summary.pop("_llm_only_freq", None)
    r128_a_summary.pop("_contract_only_freq", None)

    # Materialize sets to sorted lists for JSON serialization
    by_provider_out: dict[str, dict] = {}
    for prov, b in by_provider.items():
        by_provider_out[prov] = {
            "count": b["count"],
            "models": sorted(b["models"]),
            "strategies": sorted(b["strategies"]),
        }

    # R126.Q — gen-quality score breakdown per provider. Samples up to N
    # PW specs from disk per provider and scores them via
    # `score_pw_spec_quality`. Surfaces the provider-parity contract:
    gen_quality: dict = {}
    try:
        from pathlib import Path
        from src.agents.grounding_validator import score_pw_spec_quality
        from src.agents.api_discovery import load_dom_catalog, _load_captured_endpoints
        # Score up to 5 randomly-sampled PW main specs on disk
        pw_dir = Path("src/automation/playwright")
        pw_specs = sorted(
            p for p in pw_dir.glob("req_am_*.spec.ts")
            if not p.name.endswith("_a11y.spec.ts")
        )[:10]  # sample cap for cost
        sampled_specs: list[dict] = []
        dom_catalog_q = load_dom_catalog(project_id) or {}
        captured_q = _load_captured_endpoints(project_id) or []
        for spec_path in pw_specs:
            try:
                content = spec_path.read_text(errors="replace")
            except Exception:
                continue
            score = score_pw_spec_quality(
                content,
                ac_count=0,  # use intrinsic measurement; AC count unknown here
                dom_catalog=dom_catalog_q,
                captured_endpoints=captured_q,
            )
            sampled_specs.append({
                "spec": spec_path.name,
                **score,
            })
        if sampled_specs:
            avg_aggregate = sum(s["aggregate"] for s in sampled_specs) / len(sampled_specs)
            gen_quality = {
                "samples_scored": len(sampled_specs),
                "avg_aggregate": round(avg_aggregate, 3),
                "by_component": {
                    "ac_coverage": round(sum(s["ac_coverage"] for s in sampled_specs) / len(sampled_specs), 3),
                    "grounding_density": round(sum(s["grounding_density"] for s in sampled_specs) / len(sampled_specs), 3),
                    "assertion_substance": round(sum(s["assertion_substance"] for s in sampled_specs) / len(sampled_specs), 3),
                    "structural_validity": round(sum(s["structural_validity"] for s in sampled_specs) / len(sampled_specs), 3),
                    "gherkin_translation": round(sum(s["gherkin_translation"] for s in sampled_specs) / len(sampled_specs), 3),
                    # R127.E.1 — three new structural dimensions
                    "markdown_fence_clean": round(
                        sum(s.get("markdown_fence_clean", 0.0) for s in sampled_specs) / len(sampled_specs), 3,
                    ),
                    "single_import_per_module": round(
                        sum(s.get("single_import_per_module", 0.0) for s in sampled_specs) / len(sampled_specs), 3,
                    ),
                    "balanced_parens": round(
                        sum(s.get("balanced_parens", 0.0) for s in sampled_specs) / len(sampled_specs), 3,
                    ),
                    # R127.E.2 — semantic intent alignment (binary 0/1 per spec)
                    "gherkin_intent_alignment": round(
                        sum(s.get("gherkin_intent_alignment", 0.0) for s in sampled_specs) / len(sampled_specs), 3,
                    ),
                },
                "samples": sampled_specs,
            }
    except Exception as exc:
        gen_quality = {"error": f"score sampling skipped: {type(exc).__name__}: {str(exc)[:200]}"}

    return {
        "project_id": project_id,
        "name": project.get("name"),
        "total_tests": total,
        "by_gen_source": by_gen_source,
        "by_provider": by_provider_out,
        "failed_req_count": len(failed_reqs),
        "failed_reqs": [
            {"requirement_id": req_id, **info}
            for req_id, info in failed_reqs.items()
        ],
        "strategy_divergence_count": strategy_divergence_count,
        "gen_quality": gen_quality,
        # R127.B — decomposition rate per project + per tool
        "r127_b_decomposition_summary": r127_b_summary,
        # R127.C — escalation outcome distribution + provider breakdown
        "r127_c_escalation_summary":    r127_c_summary,
        # R128.A — shadow divergence (deterministic contract vs LLM Newman)
        "shadow_divergence_summary":    r128_a_summary,
        # R130.J — qwen-pro carry-rate + operator pivot recommendation
        "r130_j_carry_rate_summary":    await _r130_j_compute_with_cache(
            project_tests, project_id,
        ),
        # R141.C — defect classifier health (last 5 runs; alerts when
        # ≥2 of last 5 have fail_count > 50 + defect_count = 0). Operator
        # CTA: investigate Claude CLI state via R141.A.0 diagnostic logs;
        # R141.B blanket defect guarantees ≥1 defect per failures batch.
        "r141_c_defect_classification_health": await _r141_c_compute_with_cache(
            project_id,
        ),
        # R145.C/F — chromium bridge trace summary for the latest run AND
        # historical aggregation across the last 10 runs (with drift
        # detection). R145.C tells the operator WHERE the bridge broke
        # this run; R145.F tells them whether THIS run regressed from
        # prior consistent behavior.
        # R146.A — 4 per-current-run + historical r145_*_summary fields.
        # Operators see ALL R145 surfaces from one endpoint. Killswitch:
        # ARTA_R146_A_SUMMARIES_DISABLE=1 reverts each to None.
        "r145_a_summary": (
            None
            if os.environ.get("ARTA_R146_A_SUMMARIES_DISABLE") == "1"
            else _r146_a_summary_a_aggregate(project_id)
        ),
        "r145_b_summary": (
            None
            if os.environ.get("ARTA_R146_A_SUMMARIES_DISABLE") == "1"
            else _r146_a_summary_b_aggregate(project_id)
        ),
        "r145_c_bridge_trace_summary": (
            None
            if os.environ.get("ARTA_R146_A_SUMMARIES_DISABLE") == "1"
            else _r146_a_summary_c_latest_run(project_id)
        ),
        "r145_d_summary": (
            None
            if os.environ.get("ARTA_R146_A_SUMMARIES_DISABLE") == "1"
            else _r146_a_summary_d_cascade(project_id)
        ),
        "r145_f_bridge_trace_history": _r145_f_load_bridge_trace_history(
            project_id, limit=10,
        ),
        # R156.D — source-code grounding summary. Shows token chain
        # depth, whether refresh_flow is configured (closes >60min
        # smoke TTL gap via R156.J), and protocol coverage counts
        # from R156.C audit when extraction has run. Operator-actionable
        # next-steps surface as a checklist. Killswitch:
        # ARTA_R156_D_DASHBOARD_DISABLE=1 → returns {"disabled": True}.
        "r156_source_grounding_summary": _r156_d_compute_source_grounding_summary(
            project_id,
        ),
    }


@router.get("/{project_id}/strategies", dependencies=[Depends(_require_api_key)])
async def list_project_strategies(project_id: str, limit: int = 20):
    """F5-2: List persisted strategy artifacts for a project (newest first).

    Each item summarises the run-wide risk decision: distribution, top risks,
    model + prompt version. Use the `path` to fetch the full artifact.
    """
    from pathlib import Path
    import json

    out_dir = Path(os.environ.get("ARTA_STRATEGIES_DIR", ".arta/strategies"))
    if not out_dir.exists():
        return {"project_id": project_id, "strategies": [], "total": 0}

    pid = (project_id or "global").replace("/", "_")
    matches = sorted(out_dir.glob(f"{pid}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items: list[dict] = []
    for p in matches[:limit]:
        try:
            data = json.loads(p.read_text())
            items.append({
                "path": str(p),
                "filename": p.name,
                "generated_at": data.get("generated_at"),
                "trace_id": data.get("trace_id"),
                "model": data.get("model"),
                "prompt_version": data.get("prompt_version"),
                "requirements_scored": data.get("requirements_scored"),
                "risk_distribution": data.get("risk_distribution"),
                "top_risks": data.get("top_risks_score_ge_6", [])[:5],
            })
        except Exception as e:
            log.warning("Could not read strategy artifact %s: %s", p, e)
    return {"project_id": project_id, "strategies": items, "total": len(items)}


@router.get("/{project_id}/strategies/{filename}", dependencies=[Depends(_require_api_key)])
async def get_strategy_artifact(project_id: str, filename: str):
    """F5-2: Fetch a specific strategy artifact by filename (path-traversal protected)."""
    from pathlib import Path
    import json

    if not filename or "/" in filename or ".." in filename or not filename.endswith(".json"):
        raise HTTPException(400, "Invalid filename")
    out_dir = Path(os.environ.get("ARTA_STRATEGIES_DIR", ".arta/strategies")).resolve()
    target = (out_dir / filename).resolve()
    if out_dir != target.parent and out_dir not in target.parents:
        raise HTTPException(403, "Path traversal denied")
    if not target.exists():
        raise HTTPException(404, "Strategy artifact not found")
    pid = (project_id or "global").replace("/", "_")
    if not target.name.startswith(f"{pid}_"):
        raise HTTPException(404, "Strategy artifact does not belong to this project")
    try:
        return json.loads(target.read_text())
    except Exception as e:
        raise HTTPException(500, f"Could not read artifact: {e}")


@router.get("/{project_id}/environments", dependencies=[Depends(_require_api_key)])
async def list_environments(project_id: str) -> dict:
    """Part 5C — list environments available for this project.

    Reads `.arta/environments/*.json` files and surfaces their `id`,
    `name`, and `base_url`. The frontend dropdown populates from this so
    users can pick "Staging" / "Bugtrackr Local" etc. instead of the
    hardcoded `local`/`staging`/`production` triple. Project-prefixed
    matches sort first; unprefixed envs (like the bare `staging.json`)
    appear after as fallbacks.
    """
    from pathlib import Path
    import json as _json
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    env_dir = Path(os.environ.get("ARTA_ENVIRONMENTS_DIR", ".arta/environments"))
    if not env_dir.is_dir():
        return {"project_id": project_id, "environments": []}
    project_name = (project.get("name") or "").lower().split()[0] if project.get("name") else ""
    out: list[dict] = []
    for f in sorted(env_dir.glob("*.json")):
        try:
            data = _json.loads(f.read_text())
        except Exception:
            continue
        env_id = data.get("id") or f.stem
        # Two on-disk shapes: (a) Postman-style with `values: [{key,value},…]`,
        # (b) Playwright storage-state with `cookies` + `origins`.
        base_url = ""
        is_storage_state = False
        if isinstance(data.get("values"), list):
            for v in data["values"]:
                if isinstance(v, dict) and v.get("key") == "base_url":
                    base_url = v.get("value", "")
                    break
        elif isinstance(data.get("cookies"), list) and isinstance(data.get("origins"), list):
            is_storage_state = True
            origins = data.get("origins") or []
            if origins and isinstance(origins[0], dict):
                base_url = origins[0].get("origin", "")
        out.append({
            "id": env_id,
            "name": data.get("name", env_id),
            "base_url": base_url,
            "filename": f.name,
            "has_storage_state": is_storage_state or env_id.endswith("-storage"),
            "project_match": project_name and env_id.lower().startswith(project_name),
        })
    # project-prefixed envs first
    out.sort(key=lambda e: (not e["project_match"], e["id"]))
    return {"project_id": project_id, "environments": out}


@router.get(
    "/{project_id}/environments/{env_name}/variables",
    dependencies=[Depends(_require_api_key)],
)
async def list_env_variables(project_id: str, env_name: str) -> dict:
    """R29.4 — return all declared env-vars for an environment with the
    operator-action state surfaced (which still hold placeholder values).

    Pre-R29.4 operators had to hand-edit `.arta/projects.json` to fill
    the 22+ unfilled vars (REPLACE_ME / ***) — error-prone, no validation,
    requires repo access. This endpoint is the read side of the
    Settings → Environments → Variables UI.

    Returns:
        {
          env_name: <resolved env block name (e.g., "staging")>,
          variables: [
              {name, value, is_placeholder, is_sensitive},
              ...
          ],
          filled_count: int,
          total_count: int,
          needs_attention: [name, ...],   # names still set to a placeholder
        }

    The same R-EnvNameMatch resolver (auth_refresher._select_env_block)
    is reused so "acme-staging" → "staging" suffix matching works.
    """
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    try:
        from ...agents.auth_refresher import _select_env_block as _sel
    except Exception as exc:
        log.warning("R29.4: auth_refresher import failed (%s); using direct lookup", exc)
        _sel = None  # type: ignore[assignment]
    if _sel:
        resolved_env, env_block = _sel(project, env_name)
    else:
        envs = project.get("environments") or {}
        resolved_env = env_name if env_name in envs else None
        env_block = envs.get(env_name) if isinstance(envs.get(env_name), dict) else {}
    if hasattr(env_block, "model_dump"):
        env_block = env_block.model_dump()
    variables = (env_block or {}).get("variables") or {}
    placeholders = {"REPLACE_ME", "REPLACE-ME", "REPLACEME", "***", "REDACTED", "TODO", ""}
    sensitive_tokens = ("token", "secret", "password", "key", "cookie", "auth")
    items: list[dict] = []
    for name, value in variables.items():
        s = str(value) if value is not None else ""
        s_clean = s.strip()
        is_placeholder = (
            s_clean in placeholders or s_clean.startswith("__ARTA_UNSET")
        )
        is_sensitive = any(t in name.lower() for t in sensitive_tokens)
        items.append({
            "name": name,
            "value": "" if is_placeholder else s,
            "is_placeholder": is_placeholder,
            "is_sensitive": is_sensitive,
        })
    # Sort: needs-attention first, then alphabetical.
    items.sort(key=lambda x: (not x["is_placeholder"], x["name"]))
    return {
        "project_id": project_id,
        "env_name": resolved_env or env_name,
        "variables": items,
        "filled_count": sum(1 for i in items if not i["is_placeholder"]),
        "total_count": len(items),
        "needs_attention": [i["name"] for i in items if i["is_placeholder"]],
    }


class UpdateVariablesBody(BaseModel):
    updates: dict[str, str] = {}


@router.put(
    "/{project_id}/environments/{env_name}/variables",
    dependencies=[Depends(_require_api_key)],
)
async def update_env_variables(
    project_id: str, env_name: str, body: UpdateVariablesBody,
) -> dict:
    """R29.4 — bulk update env-vars (atomic per-call). Validates each
    new value isn't itself a placeholder before persisting; returns the
    refreshed counts so the editor can update its UI without re-fetching.

    Rejects updates whose value matches a known placeholder (REPLACE_ME,
    ***, etc.) — operators occasionally paste these by mistake.
    """
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    try:
        from ...agents.auth_refresher import _select_env_block as _sel
        resolved_env, env_block = _sel(project, env_name)
    except Exception:
        envs = project.get("environments") or {}
        resolved_env = env_name if env_name in envs else None
        env_block = envs.get(env_name) if isinstance(envs.get(env_name), dict) else None
    if not env_block:
        raise HTTPException(
            404,
            f"Environment {env_name!r} not found. Existing: "
            f"{list((project.get('environments') or {}).keys())}",
        )
    if hasattr(env_block, "model_dump"):
        env_block = env_block.model_dump()
        # Reattach to project so later persistence uses the dict shape.
        project["environments"][resolved_env or env_name] = env_block
    placeholders = {"REPLACE_ME", "REPLACE-ME", "REPLACEME", "***", "REDACTED", "TODO"}
    variables = env_block.setdefault("variables", {})
    rejected: dict[str, str] = {}
    saved: list[str] = []
    for name, raw in (body.updates or {}).items():
        key = (name or "").strip()
        val = (raw or "").strip()
        if not key:
            continue
        if val in placeholders or val.startswith("__ARTA_UNSET"):
            rejected[key] = "value is a placeholder — fill with a real value"
            continue
        variables[key] = val
        saved.append(key)
    if rejected:
        raise HTTPException(400, {
            "error": "rejected",
            "rejected": rejected,
            "saved": saved,
        })
    if saved:
        project["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _save_projects()
        except Exception as exc:
            log.warning("R29.4: persist failed for %s: %s", project_id, exc)
    placeholders_for_count = placeholders | {""}
    filled_count = sum(
        1 for v in variables.values()
        if str(v).strip() not in placeholders_for_count
        and not str(v).startswith("__ARTA_UNSET")
    )
    return {
        "project_id": project_id,
        "env_name": resolved_env or env_name,
        "saved": saved,
        "filled_count": filled_count,
        "total_count": len(variables),
    }


class BulkAddVariablesBody(BaseModel):
    names: list[str] = []
    default_value: str = ""
    env_name: str = "staging"
    # Fix HHH — operator-accepted suggestions arrive here as key-value pairs.
    # When provided, each pair is persisted (overwriting REPLACE_ME / unset
    # placeholders); existing real values are preserved.
    values: dict[str, str] | None = None


@router.post("/{project_id}/environments/{env_name}/variables/bulk",
             dependencies=[Depends(_require_api_key)])
async def bulk_add_environment_variables(
    project_id: str, env_name: str, body: BulkAddVariablesBody
) -> dict:
    """Fix E: bulk-add blank placeholder variables to a project environment.
    Idempotent — keys that already exist are kept (their values aren't
    overwritten). Used by the Architecture page when the user wants to
    add 25 unresolved Newman path-params at once instead of typing each.
    """
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    envs = project.setdefault("environments", {})
    env_cfg = envs.get(env_name)
    if env_cfg is None:
        raise HTTPException(
            400,
            f"Environment {env_name!r} not found. Existing: {list(envs.keys())}",
        )
    if hasattr(env_cfg, "model_dump"):
        env_cfg = env_cfg.model_dump()
        envs[env_name] = env_cfg
    variables = env_cfg.setdefault("variables", {})
    added: list[str] = []
    skipped: list[str] = []
    for raw_name in body.names or []:
        name = (raw_name or "").strip()
        if not name:
            continue
        if name in variables:
            skipped.append(name)
            continue
        variables[name] = body.default_value
        added.append(name)
    # Fix HHH — apply operator-accepted key-value pairs. Overwrites placeholders
    # ("REPLACE_ME", empty, or __ARTA_UNSET__) but preserves real existing values.
    placeholder_values = {"", "REPLACE_ME"}
    for k, v in (body.values or {}).items():
        key = (k or "").strip()
        val = v or ""
        if not key:
            continue
        cur = variables.get(key, "")
        if cur not in placeholder_values and not str(cur).startswith("__ARTA_UNSET"):
            skipped.append(key)
            continue
        variables[key] = val
        if key not in added:
            added.append(key)
    # Persist via the project store so both in-memory cache and disk are updated.
    if added:
        project["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _save_projects()
        except Exception as exc:
            log.warning("bulk-add: persist failed for %s: %s", project_id, exc)
    return {
        "project_id": project_id,
        "env_name": env_name,
        "added": added,
        "skipped": skipped,
        "total_variables": len(variables),
    }


@router.get("/{project_id}/ci-config", dependencies=[Depends(_require_api_key)])
async def get_ci_config(project_id: str, ci_provider: str | None = None) -> dict:
    """BMAD Layer 1+4 deliverable: per-project CI pipeline + tool chain.

    Reads the project's most-recent strategy artifact for the canonical tool
    chain (Layer 1 deliverable), then renders the matching CI template with
    that tool chain documented at the top. ci_provider param overrides the
    project's stored default (e.g. ?ci_provider=gitlab_ci to preview).
    """
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    # Pull tool chain from the latest strategy artifact (already groups
    # recommended_tools across all profiles into a deduplicated list).
    tools: list[str] = []
    try:
        from pathlib import Path
        import json as _json
        strat_dir = Path(os.environ.get("ARTA_STRATEGIES_DIR", ".arta/strategies"))
        pid = (project_id or "global").replace("/", "_")
        candidates = sorted(strat_dir.glob(f"{pid}_*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            data = _json.loads(candidates[0].read_text())
            tools = list(data.get("tool_chain") or [])
    except Exception as exc:
        log.debug("ci-config: tool chain lookup skipped: %s", exc)
    from ...agents.framework_setup_agent import FrameworkSetupAgent
    agent = FrameworkSetupAgent()
    try:
        result = agent.render_ci_config(
            project=project,
            tools=tools,
            ci_provider=ci_provider,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "project_id": project_id,
        "ci_provider": ci_provider or project.get("ci_provider", "github_actions"),
        "tool_chain": tools,
        "path": result["path"],
        "content": result["content"],
    }


@router.post("/{project_id}/test-llm", dependencies=[Depends(_require_api_key)])
async def test_llm_connection(project_id: str):
    """
    Test LLM connectivity for the project's configured provider.
    Sends a minimal prompt and measures latency.
    """
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    llm_data = project["llm_config"]
    config = LLMConfig.from_dict(llm_data)

    # Merge api_key from env if not stored in project
    if not config.api_key:
        import os
        env_map = {
            "anthropic":     "ANTHROPIC_API_KEY",
            "claude_code":   "CLAUDE_CODE_API_KEY",
            "google_gemini": "GOOGLE_API_KEY",
            "openai":        "OPENAI_API_KEY",
            "azure_openai":  "AZURE_OPENAI_API_KEY",
        }
        env_key = env_map.get(config.provider.value, "")
        config.api_key = os.environ.get(env_key, "")

    from ..agents.llm_client import create_llm_client  # type: ignore

    client = create_llm_client(config)
    t0 = time.monotonic()

    try:
        response = await client.messages.create(
            model=config.model,
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with one word: OK"}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "status":         "ok",
            "provider":       config.provider.value,
            "model":          config.model,
            "latency_ms":     latency_ms,
            "response_text":  response.content[0].text.strip(),
        }
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "status":     "error",
            "provider":   config.provider.value,
            "model":      config.model,
            "latency_ms": latency_ms,
            "error":      str(exc),
        }


# ── Test target-app connectivity ───────────────────────────────────────────


@router.post("/{project_id}/test-connectivity", dependencies=[Depends(_require_api_key)])
async def test_connectivity(project_id: str, body: dict):
    """Test if a project's target app is reachable and auth is valid."""
    environment = body.get("environment", "local")

    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    env_config = project.get("environments", {}).get(environment)
    if not env_config:
        raise HTTPException(status_code=404, detail=f"Environment '{environment}' not configured")

    # Support both dict (from JSON store) and Pydantic model
    if not isinstance(env_config, dict):
        env_config = env_config.model_dump() if hasattr(env_config, "model_dump") else dict(env_config)

    import httpx

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers: dict[str, str] = {}
            cookies: dict[str, str] = {}

            auth = env_config.get("auth", {})
            method = auth.get("method", "none")
            creds = auth.get("credentials", {})

            if method == "cookie":
                cookie_name = creds.get("cookie_name", "")
                cookie_value = creds.get("cookie_value", "")
                if cookie_name and cookie_value:
                    cookies[cookie_name] = cookie_value
            elif method == "bearer":
                token = creds.get("token", creds.get("bearer_token", ""))
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            elif method == "basic":
                import base64
                username = creds.get("username", "")
                password = creds.get("password", "")
                b64 = base64.b64encode(f"{username}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {b64}"

            target_url = env_config.get("api_base_url") or env_config.get("base_url", "")
            response = await client.get(
                target_url + "/health",
                headers=headers,
                cookies=cookies,
            )
            latency = int((time.time() - start) * 1000)

            return {
                "reachable": True,
                "status_code": response.status_code,
                "latency_ms": latency,
                "auth_valid": response.status_code != 401,
                "environment": environment,
            }
    except Exception as exc:
        return {
            "reachable": False,
            "status_code": 0,
            "latency_ms": 0,
            "auth_valid": False,
            "error": str(exc),
            "environment": environment,
        }


# ── Feature 5: Integration test-connection endpoint ───────────────────────

class ScaffoldRequest(BaseModel):
    stack_type:       str  = Field(default="fullstack", pattern="^(frontend|backend|fullstack)$")
    is_greenfield:    bool = True
    engagement_model: str  = Field(default="tea_solo", pattern="^(tea_solo|tea_lite|integrated)$")
    ci_provider:      str  = Field(default="github_actions", pattern="^(github_actions|gitlab_ci|jenkins|azure_devops|circleci)$")


@router.post("/{project_id}/sut/onboard", dependencies=[Depends(_require_api_key)])
async def onboard_sut(project_id: str, body: dict | None = None) -> dict:
    """Fix III (Phase G): run the SUT Onboarding Agent (Fix GGG) and
    return discovered config + needs_operator list. Persists to
    `project.integrations.onboarding_config`. Idempotent — re-running
    refreshes the config.

    Body (optional):
        {
            "session_token": "<override>",   # default: read from auth-state
            "har_path": ".../trace.har",  # optional Playwright HAR
            "endpoints": [...]            # optional pre-discovered endpoints
        }
    """
    body = body or {}
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    # R113.M.3 — parameterize storage-state path discovery. Pre-R113.M.3
    # → BugTrackr + future projects would silently pull the WRONG token.
    # Post-R113.M.3: derive candidates from the project's environment
    # configs + cookie names; fall back to legacy paths last.
    session_token = body.get("session_token")
    if not session_token:
        # Build candidate list dynamically from project environments
        _candidates: list[str] = []
        envs = project.get("environments") or {}
        for env_name in envs.keys():
            _candidates.append(f".arta/environments/{env_name}-storage.json")
            _candidates.append(f".arta/environments/{env_name}.json")
        # may still have these files even after R98.1 sync rebuild).
        for _extra in os.environ.get("ARTA_STORAGE_STATE_CANDIDATES", "").split(","):
            if _extra.strip():
                _candidates.append(_extra.strip())

        # Discover canonical cookie name(s) from project auth configs.
        # uses whatever the project declares.
        _cookie_names: list[str] = []
        for env_cfg in envs.values():
            if isinstance(env_cfg, dict):
                _ck = (env_cfg.get("auth") or {}).get("credentials", {}).get("cookie_name")
                if isinstance(_ck, str) and _ck.strip():
                    _cookie_names.append(_ck.strip())

        for cand in _candidates:
            p = Path(cand)
            if p.is_file():
                try:
                    data = json.loads(p.read_text())
                    for c in (data.get("cookies") or []):
                        if isinstance(c, dict) and c.get("name") in _cookie_names:
                            session_token = c.get("value")
                            break
                    if session_token:
                        break
                except Exception:
                    pass
    har_path = body.get("har_path")
    endpoints = body.get("endpoints")
    if not endpoints:
        try:
            from ...agents.api_discovery import _load_captured_endpoints
            endpoints = _load_captured_endpoints(project_id)
        except Exception:
            endpoints = []

    from ...agents.sut_onboarding import discover
    config = await discover(project, session_token=session_token, har_path=har_path, endpoints=endpoints)

    # Persist to project.integrations.onboarding_config
    integrations = project.setdefault("integrations", {})
    if hasattr(integrations, "model_dump"):
        integrations = integrations.model_dump()
        project["integrations"] = integrations
    integrations["onboarding_config"] = config
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _save_projects()
    except Exception as exc:
        log.warning("onboard_sut: persist skipped: %s", exc)

    return {
        "project_id": project_id,
        "onboarding_config": config,
        "needs_operator": config.get("needs_operator", []),
        "auto_resolved_count": len(config.get("harvested_claims", {})) + sum(
            1 for v in config.get("list_endpoints", {}).values() if v.get("probed_first_id")
        ),
    }


@router.post("/{project_id}/scaffold", dependencies=[Depends(_require_api_key)])
async def scaffold_project(project_id: str, body: ScaffoldRequest):
    """
    Generate test-framework scaffolding for a project.
    Returns directory structure, config files, CI pipeline, and recommendations.
    """
    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    from ...agents.framework_setup_agent import FrameworkSetupAgent

    agent = FrameworkSetupAgent()
    result = agent.scaffold({
        "name": project["name"],
        "stack_type": body.stack_type,
        "is_greenfield": body.is_greenfield,
        "engagement_model": body.engagement_model,
        "ci_provider": body.ci_provider,
    })

    # Persist the framework fields on the project (in-memory store)
    project["engagement_model"] = body.engagement_model
    project["stack_type"] = body.stack_type
    project["is_greenfield"] = body.is_greenfield

    return {
        "project_id": project_id,
        "project_name": project["name"],
        "stack_type": body.stack_type,
        "is_greenfield": body.is_greenfield,
        "engagement_model": body.engagement_model,
        "ci_provider": body.ci_provider,
        "scaffold": result,
    }


class IntegrationTestRequest(BaseModel):
    integration: str   # "github" | "jira" | "slack" | "teams"


@router.post("/{project_id}/integrations/test", dependencies=[Depends(_require_api_key)])
async def test_integration(project_id: str, body: IntegrationTestRequest):
    """Simulate testing an integration connection for a project."""
    import time as _time
    t0 = _time.monotonic()

    # In production: actually probe each service endpoint
    MOCK_RESULTS = {
        "github": {"ok": True,  "message": "Repository found and accessible"},
        "jira":   {"ok": True,  "message": "Jira project key verified — 3 open sprints found"},
        "slack":  {"ok": True,  "message": "Test message delivered to #qa-alerts"},
        "teams":  {"ok": False, "message": "Teams webhook URL is required"},
    }
    result = MOCK_RESULTS.get(body.integration, {"ok": False, "message": "Unknown integration"})
    latency_ms = int((_time.monotonic() - t0) * 1000) + 312  # Simulate network
    return {
        "ok": result["ok"],
        "integration": body.integration,
        "project_id": project_id,
        "message": result["message"],
        "latency_ms": latency_ms,
    }


# ── R15: SUT auth-state pre-flight + ingest ─────────────────────────────────
# Powers the dashboard's pre-run modal: detect when the project's stored
# SUT cookie is expired BEFORE the operator triggers tests, then accept a
# fresh cookie via paste so the run isn't wasted on cascade-skips.
# Reuses auth_refresher.py helpers — no agent-code changes.


def _to_iso(ts: float | int | None) -> str | None:
    """Unix timestamp → ISO-8601 string (UTC). None passes through."""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _r98_1_sync_env_block_from_jwt(
    env_block_vars: dict, jwt_payload: dict,
) -> dict[str, str]:
    """R98.1 — sync env_block.variables from JWT claims using R96.1's
    canonical claim map.

    For each (env_var_name, jwt_claim_path) in _R96_1_DEFAULT_CLAIM_MAP,
    resolve the JWT value (supports dotted + indexed paths like
    'organizations[0]') and overwrite env_block_vars[env_var_name] if
    the resolved JWT value differs from the current env_block value.

    Mutates env_block_vars in place. Returns a dict of {var_name:
    new_value} for the keys that were actually changed (for logging
    + auto_filled_envvars reporting).

    Preserves operator-edited values that DON'T match a JWT claim
    (e.g. custom service_id, workspace_id) — only the 6 mapped fields
    flow through. Idempotent: re-sync writes nothing if values match.

    Mission: closes the run-8bfc2c 1968-× HTTP 500 cluster where
    env_block.account_id was hand-set to organization_id (424e744f...)
    instead of root_account_id (0aee6bd7...). Newman correctly
    substitutes {{account_id}} but the value was wrong; with R98.1
    the paste auto-corrects from the JWT.
    """
    import re as _re_r98_1
    # R96.1's claim map is the single source of truth.
    from ...agents.api_discovery import _R96_1_DEFAULT_CLAIM_MAP

    def _walk_jwt(path: str):
        """Resolve dotted/indexed paths like 'organizations[0]'."""
        cur = jwt_payload
        for part in _re_r98_1.split(r"\.|\[|\]", path):
            if not part:
                continue
            if isinstance(cur, list) and part.isdigit():
                try:
                    cur = cur[int(part)]
                except IndexError:
                    return None
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
            if cur is None:
                return None
        return cur

    synced: dict[str, str] = {}
    for env_var_name, jwt_claim_path in _R96_1_DEFAULT_CLAIM_MAP.items():
        jwt_value = _walk_jwt(jwt_claim_path)
        if jwt_value is None:
            continue
        # Coerce to string for env_block (Newman --env-var format).
        jwt_value_str = str(jwt_value)
        current = env_block_vars.get(env_var_name)
        if current == jwt_value_str:
            continue   # already matches — no-op (idempotent)
        env_block_vars[env_var_name] = jwt_value_str
        synced[env_var_name] = jwt_value_str
    return synced


class AuthStateUpdate(BaseModel):
    environment: str = Field(..., min_length=1)
    cookie_value: str = Field(..., min_length=20)  # JWTs are ≥30 chars
    refresh_token: str | None = Field(None, min_length=10)
    # R15-V1: bookmarklet supplies these so backend can guard against
    # by mistake → silent auth failure post-write).
    cookie_name: str | None = None
    source_host: str | None = None
    # R45.2: live SUT probe at paste time. Default-on so the operator
    # gets immediate feedback when the cookie is server-side-rejected.
    # Override to skip when pasting an opaque session cookie that
    # won't 401 cleanly (rare).
    skip_live_probe: bool = False
    # R82.2 — operator-supplied additional localStorage entries. SPAs
    # populate them here. Each key-value pair lands in storage-state
    # JSON's `origins[].localStorage[]` after R82.1's auto-promote runs.
    # Backend serialises values as-is; operator chooses raw-string vs
    # JSON.stringify per their SPA's convention.
    localStorage_entries: dict[str, str] | None = None
    # R87.2 — sessionStorage entries (separate from localStorage; some
    # SPAs use this for session-only state like CSRF tokens or temporary
    # auth refresh state). Playwright's storageState natively applies
    # `origins[].sessionStorage[]` on context creation. Operators capture
    # via DevTools → Application → Session Storage on the SUT tab.
    sessionStorage_entries: dict[str, str] | None = None
    # R87.2 — additional cookies beyond the main session cookie. SPAs
    # Application → Cookies. Each entry: {name, value, domain?, path?,
    # httpOnly?, secure?, sameSite?, expires?}.
    additional_cookies: list[dict] | None = None


@router.get(
    "/{project_id}/auth-state",
    dependencies=[Depends(_require_api_key)],
)
async def get_auth_state(
    project_id: str,
    environment: str | None = Query(None),
) -> dict:
    """R15a — pre-flight check for the run-trigger UI.

    Reports SUT-auth-cookie expiry status WITHOUT exposing the cookie
    value. Frontend calls this on every Run Suite click and gates the
    run when `needs_refresh` is true.
    """
    from ...agents import auth_refresher as ar

    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    env_name, env_block = ar._select_env_block(project, environment)
    auth = (env_block or {}).get("auth") or {}
    cookie_name = ((auth.get("credentials") or {}).get("cookie_name")) or None
    base_url = (env_block or {}).get("base_url")

    # R28.0c — invert precedence: storage state takes priority over
    # projects.json placeholder. R15 paste writes ONLY to storage
    # state (.arta/environments/<env>-storage.json); projects.json
    # `cookie_value: "***"` is a deliberate redaction marker and stays
    # `***` forever. Pre-R28.0c, R21b returned `redacted_placeholder`
    # whenever projects.json had `***`, triggering the modal to re-pop
    # on every Run Suite click — even when the operator had pasted a
    # fresh cookie minutes ago. Now we consult storage state FIRST and
    # short-circuit to `valid` when a non-expired cookie is present.
    try:
        sc = ar.get_active_cookie(env_name, cookie_name)
    except Exception as _exc:
        log.debug("R28.0c: get_active_cookie failed: %s", _exc)
        sc = None
    if sc and sc.get("value"):
        payload = ar._decode_jwt_payload(sc["value"])
        return {
            "status": "valid",
            "needs_refresh": False,
            "cookie_name": cookie_name or sc.get("name"),
            "base_url": base_url,
            "refresh_url": base_url,
            "env_name": env_name,
            "expires_at": (
                _to_iso(payload["exp"])
                if payload and isinstance(payload.get("exp"), (int, float))
                else None
            ),
            "source": "storage_state",
        }

    # Storage state empty/expired/missing → fall through to placeholder
    # detection. R21b's original behavior preserved for the case where
    # the operator hasn't yet pasted via R15 modal.
    _PLACEHOLDER_CREDS = {"REPLACE_ME", "REPLACE-ME", "REPLACEME",
                           "***", "REDACTED", "TODO"}
    direct_cookie = (auth.get("credentials") or {}).get("cookie_value")
    direct_token = (auth.get("credentials") or {}).get("token")
    direct_password = (auth.get("credentials") or {}).get("password")
    redacted_field = None
    for fname, fval in (
        ("cookie_value", direct_cookie),
        ("token", direct_token),
        ("password", direct_password),
    ):
        if isinstance(fval, str) and fval.strip() in _PLACEHOLDER_CREDS:
            redacted_field = fname
            break
    if redacted_field:
        _placeholder_repr = repr(direct_cookie) if redacted_field == "cookie_value" else "<placeholder>"
        return {
            "status": "redacted_placeholder",
            "needs_refresh": True,
            "cookie_name": cookie_name,
            "base_url": base_url,
            "refresh_url": base_url,
            "env_name": env_name,
            "redacted_field": redacted_field,
            "message": (
                f"auth.credentials.{redacted_field} is a redacted placeholder "
                f"({_placeholder_repr}) AND no fresh cookie in storage state. "
                f"Operator must paste a fresh credential via the Refresh "
                f"Auth modal before this project's tests can authenticate."
            ),
        }

    storage_path = ar._find_storage_state_path(env_name)
    if not storage_path:
        return {
            "status": "missing",
            "needs_refresh": True,
            "cookie_name": cookie_name,
            "base_url": base_url,
            "refresh_url": base_url,
            "env_name": env_name,
            "message": "No storage-state file. Operator must complete first-run login.",
        }

    storage = ar._read_storage_state(storage_path)
    if not storage:
        return {
            "status": "unreadable",
            "needs_refresh": True,
            "cookie_name": cookie_name,
            "base_url": base_url,
            "refresh_url": base_url,
            "env_name": env_name,
            "message": f"Could not parse {storage_path.name}. Operator should re-login.",
        }

    session_value = None
    for c in storage.get("cookies") or []:
        if cookie_name and c.get("name") == cookie_name:
            session_value = c.get("value")
            break
        if not cookie_name and "token" in (c.get("name") or "").lower():
            session_value = c.get("value")
            cookie_name = c.get("name")
            break

    if not session_value:
        return {
            "status": "missing_cookie",
            "needs_refresh": True,
            "cookie_name": cookie_name,
            "base_url": base_url,
            "refresh_url": base_url,
            "env_name": env_name,
            "message": "No session cookie in storage state. Operator should login.",
        }

    payload = ar._decode_jwt_payload(session_value)
    if not payload or not isinstance(payload.get("exp"), (int, float)):
        # Opaque token — can't tell expiry. Trust it; operator returns
        # if tests start cascade-skipping.
        return {
            "status": "opaque",
            "needs_refresh": False,
            "cookie_name": cookie_name,
            "base_url": base_url,
            "refresh_url": base_url,
            "env_name": env_name,
            "message": "Cookie is not a JWT — can't auto-detect expiry.",
        }

    # long before the wrapper (~days); the wrapper-only check reported a stale session
    # as "valid" (→ analytics 400). `_is_session_expired` = min(wrapper, inner).
    expired = ar._is_session_expired(session_value)
    return {
        "status": "expired" if expired else "valid",
        "needs_refresh": expired,
        "cookie_name": cookie_name,
        "base_url": base_url,
        "refresh_url": base_url,
        "env_name": env_name,
        "expires_at": _to_iso(payload["exp"]),
        "expired_at": _to_iso(payload["exp"]) if expired else None,
    }


@router.post(
    "/{project_id}/auth-state",
    dependencies=[Depends(_require_api_key)],
)
async def update_auth_state(project_id: str, body: AuthStateUpdate) -> dict:
    """R15b — accept fresh cookie + refresh-token from the dashboard
    modal. Validates JWT shape, host match (V1), and writes storage
    state atomically via auth_refresher's existing helper.

    Why no JWT-required-here check: some SUTs use opaque session tokens.
    But we DO reject pasting a token that's already past its `exp`
    (operator most likely pasted the wrong one).
    """
    from urllib.parse import urlparse
    from ...agents import auth_refresher as ar

    project = await _resolve_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    env_name, env_block = ar._select_env_block(project, body.environment)
    auth = (env_block or {}).get("auth") or {}
    cookie_name = ((auth.get("credentials") or {}).get("cookie_name")) or None
    base_url = (env_block or {}).get("base_url")

    if not cookie_name:
        raise HTTPException(
            status_code=400,
            detail=f"Project has no cookie_name configured for env {env_name!r}",
        )

    # V1 — guard against paste-from-wrong-tab. Bookmarklet supplies host
    # + cookie_name; cross-check both.
    if body.cookie_name and body.cookie_name != cookie_name:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cookie-name mismatch: project expects '{cookie_name}', "
                f"clipboard contained '{body.cookie_name}'. Make sure you "
                f"copied from the right SUT tab."
            ),
        )
    if body.source_host and base_url:
        expected_host = urlparse(base_url).netloc
        if expected_host and not (
            body.source_host == expected_host
            or body.source_host.endswith("." + expected_host)
            or expected_host.endswith("." + body.source_host)
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cookie was copied from '{body.source_host}' but this "
                    f"project expects auth from '{expected_host}'. Wrong tab?"
                ),
            )

    # inner id_token already expired — either way it will fail SUT auth).
    # Opaque (non-JWT) tokens can't be checked — accept those. Inner-token-aware.
    payload = ar._decode_jwt_payload(body.cookie_value)
    if payload and (isinstance(payload.get("exp"), (int, float))
                    or payload.get("third_party_token") or payload.get("id_token")):
        if ar._is_session_expired(body.cookie_value):
            raise HTTPException(
                status_code=400,
                detail=(
                    "The cookie you pasted is already expired. Re-login to "
                    f"{base_url!r} first, then re-copy. (JWT exp was "
                    f"{_to_iso(payload['exp'])})"
                ),
            )

    # R45.2 — live SUT probe before persisting. Pre-fix the operator
    # could paste a syntactically-valid-but-functionally-rejected cookie
    # (wrong tenant, server-side invalidated, scope mismatch); the
    # modal would happily report success and the next Run Suite click
    # would 409 with the same 22-vars toast. This probe gives immediate
    # feedback at the source so the operator never lands on a stale
    # storage state thinking auth is fixed.
    api_base_for_probe = (env_block or {}).get("api_base_url") or base_url
    if api_base_for_probe and not body.skip_live_probe:
        import httpx as _httpx_45_2
        probe_url = api_base_for_probe.rstrip("/") + "/api/v1/users/me"
        probe_headers = {"Cookie": f"{cookie_name}={body.cookie_value}"}
        try:
            async with _httpx_45_2.AsyncClient(
                timeout=5.0, follow_redirects=True, verify=False,
            ) as _probe_client:
                _probe_resp = await _probe_client.get(
                    probe_url, headers=probe_headers,
                )
            _probe_ct = (_probe_resp.headers.get("content-type") or "").lower()
            _looks_like_login = "html" in _probe_ct and _probe_resp.status_code == 200
            if _probe_resp.status_code == 401 or _looks_like_login:
                _suffix = " + HTML login page" if _looks_like_login else ""
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "cookie_not_authenticated",
                        "message": (
                            "The cookie you pasted didn't authenticate against "
                            f"{api_base_for_probe} (returned {_probe_resp.status_code}{_suffix}). "
                            "Re-login on the SUT, copy a fresh cookie, and try again."
                        ),
                        "probe_status": _probe_resp.status_code,
                        "probe_url": probe_url,
                    },
                )
        except HTTPException:
            raise
        except _httpx_45_2.HTTPError as _probe_exc:
            # Network failure — can't validate. Persist anyway with a
            # warning; operator's next Run Suite will surface any real
            # auth issue via R39.1's auth_failed.flag detection.
            log.warning(
                "R45.2: cookie probe failed for project=%s (%s) — "
                "proceeding without validation; auth_failed.flag will "
                "fire post-discovery if the cookie is broken.",
                project_id, _probe_exc,
            )

    # R219.B — the auth-state WRITE must target this env's own file strictly.
    # `_find_storage_state_path` falls back to the newest `*-storage.json` when
    # `<env>-storage.json` doesn't exist yet (correct for READING an existing
    # session, wrong for WRITING) — for a NEW project's first paste that
    # fallback merges the session into ANOTHER project's storage file (SUT
    # isolation break). Always write to the exact per-env path; only reuse an
    # existing file when it is THIS env's canonical file (to preserve prior
    # cookies/origins for the same env across re-pastes).
    _envs_dir = Path(os.environ.get("ARTA_ENVIRONMENTS_DIR", ".arta/environments"))
    storage_path = _envs_dir / f"{env_name}-storage.json"
    storage = ar._read_storage_state(storage_path) or {"cookies": [], "origins": []}

    # Add/replace localStorage refresh-token under the SUT origin.
    if body.refresh_token and base_url:
        sut_origin = base_url.rstrip("/")
        # Strip trailing slash + any path component — Playwright origins
        # are scheme://host[:port], no path.
        parsed = urlparse(sut_origin)
        sut_origin = f"{parsed.scheme}://{parsed.netloc}"
        origins = storage.setdefault("origins", [])
        origin_block = next(
            (o for o in origins if o.get("origin") == sut_origin), None
        )
        if not origin_block:
            origin_block = {"origin": sut_origin, "localStorage": []}
            origins.append(origin_block)
        ls = origin_block.setdefault("localStorage", [])
        # Some SPAs JSON.stringify the token; preserve that wrapper.
        new_ls_value = body.refresh_token
        if not new_ls_value.startswith('"'):
            new_ls_value = f'"{new_ls_value}"'
        ls_entry = next((x for x in ls if x.get("name") == "refresh-token"), None)
        if ls_entry:
            ls_entry["value"] = new_ls_value
        else:
            ls.append({"name": "refresh-token", "value": new_ls_value})

    # R112.A.3 — KEYSTONE — write the cookie to storage.cookies[] so
    # Playwright runtime authenticates via the cookie HTTP header on
    # page.goto. Pre-R112.A.3 R45.2 only wrote refresh-token + cookie
    # alias into origins[].localStorage; the actual HTTP cookie was NOT
    # in storage.cookies[]. Result: PW page.goto loaded SUT routes
    # without the auth cookie → SUT 401 → redirect to login → 10s
    # waitForResponse timeout (run-25aabd evidence: L3 auth pre-flight
    # also detected storage_state_has_creds=False because cookies[] was
    # empty). Mission gap: PW PASS = 0 forever without this.
    if body.cookie_value and cookie_name and base_url:
        parsed_cv = urlparse(base_url.rstrip("/"))
        # Cookie domain MUST allow the SUT host. Use the bare host (no leading
        # dot) for exact-match; Playwright also accepts a leading-dot variant
        # for wildcard. Exact-match is safer + matches operator's DevTools
        # screenshot patterns.
        cookie_domain = parsed_cv.netloc.split(":")[0]
        # R146.G KEYSTONE — apply R84 widening BEFORE the cookie write.
        # Pre-R146.G this path wrote the host-only cookie, then a separate
        # R84-widen path at line ~2801 wrote a SECOND cookie with leading-
        # Chromium picked the host-only entry → SPA-side API calls to
        # SUT 401 → /login redirect → skipIfAuthStale fired → Iter 5
        # 329 PW SKIPs misclassified as `framework_limit_or_implicit`.
        # Post-R146.G: widen at the source; downstream R84 call becomes
        # idempotent + duplicate eliminated.
        _r146_g_api_base_url = (env_block or {}).get("api_base_url") or ""
        cookie_domain, _r146_g_trigger = _r84_widen_cookie_domain(
            cookie_domain, base_url, _r146_g_api_base_url,
        )
        if _r146_g_trigger:
            log.info(
                "R146.G: hoisted R84 widen BEFORE R112.A.3 cookie write "
                "(api_base=%s, base=%s, trigger=%s) → cookie_domain=%s",
                urlparse(_r146_g_api_base_url).netloc if _r146_g_api_base_url else "<unset>",
                urlparse(base_url).netloc,
                _r146_g_trigger, cookie_domain,
            )
        cookies_list = storage.setdefault("cookies", [])
        # R146.G — deduplicate any pre-existing host-only entry written by
        # prior pastes BEFORE this fix shipped. When the operator pastes
        # AFTER R146.G ships, host-only entries from prior pastes get
        # removed; subsequent re-pastes only see one widened entry.
        _r146_g_pre_widen_host = parsed_cv.netloc.split(":")[0]
        if cookie_domain != _r146_g_pre_widen_host:
            _r146_g_stale_removed = 0
            cookies_list_dedup = []
            for c in cookies_list:
                if (
                    c.get("name") == cookie_name
                    and c.get("domain") == _r146_g_pre_widen_host
                ):
                    _r146_g_stale_removed += 1
                    continue
                cookies_list_dedup.append(c)
            if _r146_g_stale_removed:
                cookies_list[:] = cookies_list_dedup
                log.info(
                    "R146.G: removed %d stale host-only %s cookie entry/entries "
                    "(domain=%s) from storage state; only widened entry "
                    "(domain=%s) will remain after this paste",
                    _r146_g_stale_removed, cookie_name,
                    _r146_g_pre_widen_host, cookie_domain,
                )
        # Update-or-append: if a cookie with the same (name, domain) exists,
        # mutate its value; otherwise append a new entry.
        existing = next(
            (
                c for c in cookies_list
                if c.get("name") == cookie_name and c.get("domain") == cookie_domain
            ),
            None,
        )
        # Long-lived: 7 days. Playwright treats `-1` as session-only; explicit
        # future expiry survives newContext() invocations within the smoke.
        cookie_entry = {
            "name": cookie_name,
            "value": body.cookie_value,
            "domain": cookie_domain,
            "path": "/",
            "expires": int(time.time()) + 7 * 24 * 3600,
            "httpOnly": False,
            "secure": parsed_cv.scheme == "https",
            "sameSite": "Lax",
        }
        if existing:
            existing.update(cookie_entry)
            _r112_a3_action = "updated"
        else:
            cookies_list.append(cookie_entry)
            _r112_a3_action = "appended"
        log.info(
            "R112.A.3: %s cookie %s=<%d chars> for domain=%s in storage.cookies[] "
            "(now %d cookies total)",
            _r112_a3_action, cookie_name, len(body.cookie_value),
            cookie_domain, len(cookies_list),
        )

    # R82.1 KEYSTONE — promote cookie_value to a localStorage entry under
    # the HTTP cookie (for server-side validation) AND localStorage (for
    # the client-side router's "am I authenticated?" check). Operator's
    # token') === null and redirected every route to /login → discovery
    # probe got HTML on every navigation → 0 env vars harvested → R67.C
    # blocked Playwright forever. Auto-promote into the same JSON file
    # so a single paste populates BOTH locations. Safe by design: the
    # value IS the cookie, so duplicating to localStorage doesn't expose
    # new info. Legacy SPAs that ignore the entry are unaffected.
    if body.cookie_value and cookie_name and base_url:
        sut_origin_ck = base_url.rstrip("/")
        parsed_ck = urlparse(sut_origin_ck)
        sut_origin_ck = f"{parsed_ck.scheme}://{parsed_ck.netloc}"
        origins_ck = storage.setdefault("origins", [])
        origin_block_ck = next(
            (o for o in origins_ck if o.get("origin") == sut_origin_ck), None,
        )
        if not origin_block_ck:
            origin_block_ck = {"origin": sut_origin_ck, "localStorage": []}
            origins_ck.append(origin_block_ck)
        ls_ck = origin_block_ck.setdefault("localStorage", [])
        # R88.1 — wrap cookie_value in JSON.stringify quotes BEFORE writing
        # to localStorage. Mirrors the refresh-token pattern at line
        # a raw JWT fails the parse → SPA treats user as logged out →
        # bootstrap redirects to /login → discovery probe walks login
        # pages → 0 backend XHRs harvested → R67.C blocks Playwright.
        #
        # route captured identical 4-element login-page DOM snapshots;
        # post-R88.1 routes capture authenticated dashboard content
        # menu). Idempotent: skip wrap if value already starts with a
        # quote (operator may have wrapped manually via R82.2 path).
        new_cookie_ls_value = body.cookie_value
        if not new_cookie_ls_value.startswith('"'):
            new_cookie_ls_value = f'"{new_cookie_ls_value}"'
        ls_entry_ck = next(
            (x for x in ls_ck if x.get("name") == cookie_name), None,
        )
        if ls_entry_ck:
            ls_entry_ck["value"] = new_cookie_ls_value
        else:
            ls_ck.append({"name": cookie_name, "value": new_cookie_ls_value})
        log.info(
            "R88.1: promoted cookie_value to localStorage entry %s @ %s "
            "(JSON-stringified per SPA-uses-JSON.parse contract)",
            cookie_name, sut_origin_ck,
        )

    # R82.2 — operator-supplied additional localStorage entries. SPAs
    # localStorage per the DevTools screenshot) can populate via this
    # dict. Backend serialises values as-is; operator chooses raw-string
    # vs JSON.stringify per their SPA's convention.
    if body.localStorage_entries and base_url:
        sut_origin_ls = base_url.rstrip("/")
        parsed_ls = urlparse(sut_origin_ls)
        sut_origin_ls = f"{parsed_ls.scheme}://{parsed_ls.netloc}"
        origins_ls = storage.setdefault("origins", [])
        origin_block_ls = next(
            (o for o in origins_ls if o.get("origin") == sut_origin_ls), None,
        )
        if not origin_block_ls:
            origin_block_ls = {"origin": sut_origin_ls, "localStorage": []}
            origins_ls.append(origin_block_ls)
        ls_ls = origin_block_ls.setdefault("localStorage", [])
        _r82_2_written = 0
        for k, v in body.localStorage_entries.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            entry = next((x for x in ls_ls if x.get("name") == k), None)
            if entry:
                entry["value"] = v
            else:
                ls_ls.append({"name": k, "value": v})
            _r82_2_written += 1
        if _r82_2_written:
            log.info(
                "R82.2: wrote %d operator-supplied localStorage entrie(s) @ %s",
                _r82_2_written, sut_origin_ls,
            )

    # R87.2 — operator-supplied sessionStorage entries. SPAs that store
    # CSRF tokens, refresh-temporary state, or per-tab session keys in
    # sessionStorage (rather than localStorage) need this channel —
    # localStorage_entries alone won't reach them. Playwright's
    # storageState natively applies `origins[].sessionStorage[]` on
    # context creation.
    if body.sessionStorage_entries and base_url:
        sut_origin_ss = base_url.rstrip("/")
        parsed_ss = urlparse(sut_origin_ss)
        sut_origin_ss = f"{parsed_ss.scheme}://{parsed_ss.netloc}"
        origins_ss = storage.setdefault("origins", [])
        origin_block_ss = next(
            (o for o in origins_ss if o.get("origin") == sut_origin_ss), None,
        )
        if not origin_block_ss:
            origin_block_ss = {
                "origin": sut_origin_ss,
                "localStorage": [],
                "sessionStorage": [],
            }
            origins_ss.append(origin_block_ss)
        ss_ss = origin_block_ss.setdefault("sessionStorage", [])
        _r87_2_ss_written = 0
        for k, v in body.sessionStorage_entries.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            entry = next((x for x in ss_ss if x.get("name") == k), None)
            if entry:
                entry["value"] = v
            else:
                ss_ss.append({"name": k, "value": v})
            _r87_2_ss_written += 1
        if _r87_2_ss_written:
            log.info(
                "R87.2: wrote %d sessionStorage entrie(s) @ %s",
                _r87_2_ss_written, sut_origin_ss,
            )

    # R87.2 — additional cookies beyond the main session cookie. SPAs
    # that the operator captures via DevTools → Application → Cookies.
    # Each dict: {name, value, domain?, path?, httpOnly?, secure?,
    # sameSite?, expires?}. Idempotent: update-in-place on name match.
    if body.additional_cookies and isinstance(body.additional_cookies, list):
        cookies_list_r87_2 = storage.setdefault("cookies", [])
        _r87_2_ck_written = 0
        for c in body.additional_cookies:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            name_r87_2 = str(c["name"])
            existing_r87_2 = next(
                (x for x in cookies_list_r87_2 if x.get("name") == name_r87_2), None,
            )
            # Default domain: derive from base_url if operator didn't
            # supply one. Leading dot stays optional — operator decides.
            default_domain_r87_2 = (
                c.get("domain")
                or (urlparse(base_url).netloc if base_url else "")
            )
            new_cookie_r87_2 = {
                "name": name_r87_2,
                "value": str(c.get("value", "")),
                "domain": str(default_domain_r87_2),
                "path": str(c.get("path") or "/"),
                "expires": float(c.get("expires") or (time.time() + 24 * 3600)),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", True)),
                "sameSite": str(c.get("sameSite") or "Lax"),
            }
            if existing_r87_2:
                existing_r87_2.update(new_cookie_r87_2)
            else:
                cookies_list_r87_2.append(new_cookie_r87_2)
            _r87_2_ck_written += 1
        if _r87_2_ck_written:
            log.info(
                "R87.2: wrote %d additional cookie(s) to storage state",
                _r87_2_ck_written,
            )

    # Resolve cookie_domain: prefer existing entry's domain, else derive
    # from base_url's host.
    cookie_domain = None
    for c in storage.get("cookies") or []:
        if c.get("name") == cookie_name:
            cookie_domain = c.get("domain")
            break
    if not cookie_domain and base_url:
        cookie_domain = urlparse(base_url).netloc
    # R84 KEYSTONE — prefix with `.` to enable subdomain inclusion when
    # base_url + api_base_url live on different subdomains. Playwright's
    # storage state interprets domain WITHOUT leading dot as host-only,
    # Prefixing with `.` makes the cookie cross-subdomain by RFC 6265
    # subdomain-match semantics. Idempotent: skip if already prefixed,
    # skip for IP-literal hosts (no subdomains anyway), and skip when
    # the project's base_url + api_base_url share the same exact host
    # (single-subdomain SUT). For the latter check we use api_base_url
    # too — read from the env_block similarly to base_url.
    api_base_url = (env_block or {}).get("api_base_url") or ""
    cookie_domain, _r84_trigger = _r84_widen_cookie_domain(
        cookie_domain, base_url, api_base_url,
    )
    if _r84_trigger:
        log.info(
            "R84/R144.B.2: prefixed cookie_domain (api=%s, base=%s, "
            "trigger=%s) → domain=%s",
            urlparse(api_base_url).netloc if api_base_url else "<unset>",
            urlparse(base_url).netloc if base_url else "",
            _r84_trigger, cookie_domain,
        )

    # Atomic write via existing helper — V5 pattern derives expiry from
    # JWT exp claim, falls back to 24h for opaque tokens.
    new_path = ar._update_storage_state(
        storage_path,
        storage,
        cookie_name=cookie_name,
        new_cookie_value=body.cookie_value,
        set_cookie_headers=[],
        cookie_domain=cookie_domain,
    )

    # R112.A.5 KEYSTONE — write paste-trust meta sidecar so auth-setup.ts's
    # globalSetup honors the operator's fresh paste regardless of whether
    # the discovery probe subprocess env carries TARGET_AUTH_COOKIE_VALUE
    # in the exact form needed for hash matching. Pre-R112.A.5: R112.A.4
    # required subprocess env COOKIE_VALUE == file's cookie value; the
    # R45.3 discovery probe subprocess often runs with a stale or empty
    # env relative to the just-pasted file → R112.A.4 didn't fire →
    # chromium launched → about:blank fallback → file wiped.
    # R112.A.5: declare "operator just pasted, trust this file" via the
    # meta sidecar. auth-setup.ts reads source==r45_2_paste + sees cookies
    # present → returns early without launching chromium.
    if new_path:
        try:
            meta_path = Path(str(new_path) + ".r112a.meta.json")
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps({
                "source": "r45_2_paste",
                "built_at": datetime.utcnow().isoformat() + "Z",
                "cookie_name": cookie_name,
                "cookie_len": len(body.cookie_value or ""),
            }, indent=2))
            log.info(
                "R112.A.5: wrote paste-trust meta sidecar at %s (cookie_name=%s, "
                "cookie_len=%d). auth-setup.ts will preserve the file on next run.",
                meta_path, cookie_name, len(body.cookie_value or ""),
            )
        except Exception as _r112_a5_exc:
            # R113.H — propagate failure to caller. Pre-R113.H: log.warning
            # swallowed the failure and returned 200 OK to the operator, but
            # auth-setup.ts then ran without the paste-trust marker → next
            # R45.3 probe wiped storage state → smoke later failed with
            # auth_failure (confusing operator who saw "paste succeeded").
            # R113.H raises HTTPException(500) so operator sees the truth.
            log.error("R113.H: R112.A.5 meta sidecar write FAILED: %s", _r112_a5_exc)
            raise HTTPException(
                status_code=500,
                detail=(
                    f"R45.2 paste partially succeeded (storage state written) "
                    f"but R112.A.5 paste-trust meta sidecar write FAILED at "
                    f"{meta_path}: {_r112_a5_exc}. Storage state may be wiped "
                    f"on next R45.3 discovery probe. Investigate filesystem "
                    f"permissions on `.arta/environments/` and retry paste."
                ),
            ) from _r112_a5_exc

    # R55.4 — create an alias symlink at the operator-typed env name
    # whenever it differs from the resolved alias. Pre-R55.4 the writer
    # used `env_name` (resolved) but the discovery reader / some legacy
    # paths still looked up by `body.environment` (typed) → mismatch →
    # discovery probe ran un-authenticated → 29 stale `auth_failed.flag`
    # files accumulated. With the alias symlink, both lookups find the
    # file.
    if env_name and body.environment and env_name != body.environment:
        try:
            envs_dir = Path(os.environ.get("ARTA_ENVIRONMENTS_DIR", ".arta/environments"))
            alias_path = envs_dir / f"{body.environment}-storage.json"
            target_name = Path(new_path).name if new_path else f"{env_name}-storage.json"
            # Remove any pre-existing symlink/file at the alias path
            # (be conservative: only unlink if it's a symlink, not a
            # regular file written by an operator).
            if alias_path.is_symlink():
                alias_path.unlink()
            elif alias_path.exists():
                # Pre-existing regular file — leave it alone (operator
                # may have hand-written it).
                log.debug(
                    "R55.4: skipping symlink — %s already exists as a regular file",
                    alias_path,
                )
                alias_path = None
            if alias_path is not None:
                alias_path.symlink_to(target_name)  # relative symlink
                log.info(
                    "R55.4: created storage-state alias symlink %s → %s",
                    alias_path.name, target_name,
                )
        except OSError as _r55_4_exc:
            log.debug(
                "R55.4: symlink creation skipped (best-effort): %s",
                _r55_4_exc,
            )

    # R45.3 — synchronous discovery after a SUT-validated paste (R45.2).
    # Pre-fix the storage state was written + the modal closed BUT the
    # operator's next Run Suite click hit the gate against still-stale
    # env vars (R44.3 fired discovery in background but the gate
    # evaluated immediately). With the cookie now confirmed valid,
    # block for up to 90s while discovery harvests the project's env
    # vars from the authenticated SPA. Operator sees the harvest
    # count in the modal AND lands on a fresh env_block for the next
    # Run Suite click.
    discovery_completed = False
    envvars_harvested = 0
    discovery_diagnosis = None
    if not body.skip_live_probe:
        try:
            from ...agents import discovery_executor as _disc_exec_mod
            import asyncio as _aio_45_3
            _project_for_disc = {
                "id": project_id,
                "environments": project.get("environments") or {},
                "integrations": project.get("integrations") or {},
                "discovery_settings": project.get("discovery_settings") or {},
            }
            # R90.2 — synthetic ctx with workflow_id=project_id so the
            # probe's HAR + DOM sidecars land at
            # `.arta/discovery/<project_id>/` (not the shared `no_id`
            # fallback dir). Pre-R90.2 passing ctx=None caused
            # `getattr(ctx, "workflow_id", "no_id")` in
            # discovery_executor.execute to default to "no_id" → HAR
            # collisions across projects → ingest_dom_snapshots read
            # stale `no_id/` sidecars → project's dom_catalog.json kept
            # stale May-8 zero counts → R67.C correctly defensively
            # blocked Playwright despite R88.1 having authenticated the
            # SPA. Discovery_executor reads only `workflow_id` via
            # getattr-with-default, so this minimal context suffices.
            class _R90_2_Ctx:
                """Minimal R45.3 context — discovery_executor reads only
                `workflow_id` via getattr-with-default."""
                def __init__(self, wf_id: str) -> None:
                    self.workflow_id = wf_id
            # R91.D — timeout 90s → 240s (UNCONDITIONAL). The subprocess
            # cap inside _spawn_playwright_discovery is 540s, which is
            # evidence the team expected probes to commonly take >90s.
            # walk + networkidle waits empirically exceeds 90s. Pre-R91.D
            # operators saw `envvars_harvested: 0` on the paste response
            # despite the background probe successfully completing
            # within the 540s subprocess cap. 240s gives the synchronous
            # path room to finish on healthy SPAs without sacrificing
            # operator UX (R91.4 surfaces the timeout case with a
            # structured "still running" message).
            _harvest = await _aio_45_3.wait_for(
                _disc_exec_mod.execute(
                    _R90_2_Ctx(project_id), _project_for_disc, env_name,
                ),
                timeout=240.0,
            )
            envvars_harvested = len((_harvest or {}).get("envvar_values") or {})
            discovery_completed = True
            discovery_diagnosis = (_harvest or {}).get("_auth_diagnosis") or (_harvest or {}).get("_diagnosis")
            log.info(
                "R45.3: synchronous discovery for project=%s env=%s harvested %d env vars",
                project_id, env_name, envvars_harvested,
            )
        except _aio_45_3.TimeoutError:
            log.warning(
                "R45.3: synchronous discovery timed out for project=%s env=%s; "
                "harvest will continue in background",
                project_id, env_name,
            )
        except Exception as _disc_exc:
            log.warning(
                "R45.3: synchronous discovery failed for project=%s env=%s: %s",
                project_id, env_name, _disc_exc,
            )

    # R48.1 — auto-fill cookie-aliased env vars from the just-pasted
    # cookie. Pre-R48.1 the operator pasted a cookie → storage state
    # wrote → but vars named `cookie_value`, `cookie0_value`,
    # `auth_token` (when project is cookie-only) stayed REPLACE_ME.
    # The 7-var R36.2 gate kept firing because those 3 cookie-aliased
    # vars stayed placeholder despite the paste. Two-tier rule:
    #   Tier 1 — `*cookie*` (case-insensitive): always fill. By
    #     convention these are aliases for the cookie value.
    #   Tier 2 — `*token*` / `*bearer*` / `*jwt*`: fill ONLY when the
    #     project's auth.credentials.bearer_token is NOT configured
    #     (cookie-only project). When Bearer is separately configured,
    #     auth_token may be a different value than the cookie.
    import re as _re_48_1
    _COOKIE_ALIAS_RE = _re_48_1.compile(r"cookie", _re_48_1.IGNORECASE)
    _TOKEN_ALIAS_RE = _re_48_1.compile(r"(token|bearer|jwt)", _re_48_1.IGNORECASE)
    _PLACEHOLDERS_48_1 = {"REPLACE_ME", "REPLACE-ME", "***", "REDACTED", ""}

    # R48.1 fix — `creds` was a local in R45.2's probe scope; re-derive
    # from env_block here so this block doesn't depend on prior locals.
    _r48_1_creds = ((env_block or {}).get("auth") or {}).get("credentials") or {}
    _existing_bearer = _r48_1_creds.get("bearer_token")
    _bearer_token_set = (
        isinstance(_existing_bearer, str)
        and _existing_bearer.strip() not in _PLACEHOLDERS_48_1
    )
    _cookie_only_project = not _bearer_token_set

    declared_vars_48_1 = (env_block or {}).get("variables") or {}
    auto_filled_envvars: list[dict] = []
    for var_name_48, var_val_48 in list(declared_vars_48_1.items()):
        if not isinstance(var_val_48, str):
            continue
        var_lower_48 = var_name_48.lower()
        # R98.1.1 — cookie aliases MUST refresh on every paste (the operator
        # design since cookies expire ~hourly). R48.1's "never overwrite"
        # rule was correct for opaque ID vars (account_id, etc.) but wrong
        # for ephemeral cookies. Live evidence (post-R98.1): cookie_value
        #
        # Scope: cookie aliases ONLY. Token aliases (agent_token,
        # auth_token, bearer_token) MUST NOT be overwritten by the raw
        # which is a different (and longer-lived) JWT. R98.1.1's first
        # iteration accidentally clobbered the 831-char agent_token with
        # aliases only.
        is_cookie_alias_98_1_1 = bool(_COOKIE_ALIAS_RE.search(var_lower_48))
        is_token_alias_cookie_only = (
            _cookie_only_project and _TOKEN_ALIAS_RE.search(var_lower_48)
        )
        # Skip if filled AND not a cookie alias that demands refresh.
        # Token aliases still use the original R48.1 rule (fill-once)
        # because their canonical source is R96.1 / Fix EEE, not paste.
        if var_val_48.strip() not in _PLACEHOLDERS_48_1:
            if not is_cookie_alias_98_1_1:
                continue
            # Idempotent: skip if already exactly equal to the new value
            if var_val_48 == body.cookie_value:
                continue
        if is_cookie_alias_98_1_1:
            declared_vars_48_1[var_name_48] = body.cookie_value
            auto_filled_envvars.append({"name": var_name_48, "tier": "cookie_alias"})
        elif is_token_alias_cookie_only:
            declared_vars_48_1[var_name_48] = body.cookie_value
            auto_filled_envvars.append({"name": var_name_48, "tier": "token_alias_cookie_only"})

    if auto_filled_envvars:
        try:
            # Mutate _PROJECTS in place (the project dict came from there)
            # then call existing _save_projects to persist projects.json.
            project["environments"][env_name]["variables"] = declared_vars_48_1
            _save_projects()
            log.info(
                "R48.1: auto-filled %d env var(s) from cookie paste "
                "(project=%s env=%s): %s",
                len(auto_filled_envvars), project_id, env_name,
                [v["name"] for v in auto_filled_envvars],
            )
        except Exception as _save_exc:
            log.warning(
                "R48.1: persist failed for project=%s; auto-fill held in "
                "memory only: %s",
                project_id, _save_exc,
            )

    # R98.1 KEYSTONE — JWT-claim → env_block.variables auto-sync.
    #
    # Pre-R98.1 env_block.variables.account_id was hand-edited to
    # need `root_account_id` (0aee6bd7-...). R96.1's claim-map fires
    # ONLY for token-exchange (Fix EEE), NOT for env_block population.
    # Result (run-8bfc2c): 1968 × HTTP 500 because Newman correctly
    # substitutes {{account_id}} but the value injected is the wrong
    #
    # Fix: reuse R96.1's claim map. Walk it; for each (env_var,
    # jwt_path), resolve the JWT path → if value differs from current
    # env_block value, overwrite + stamp `_jwt_synced_at`. Preserves
    # operator-edited values that DON'T match a JWT claim (e.g.
    # custom service_id) — only overwrites the 6 mapped fields.
    if payload and isinstance(payload, dict):
        try:
            r98_1_synced = _r98_1_sync_env_block_from_jwt(
                env_block_vars=declared_vars_48_1,
                jwt_payload=payload,
            )
            if r98_1_synced:
                project["environments"][env_name]["variables"] = declared_vars_48_1
                _save_projects()
                log.info(
                    "R98.1: JWT-synced %d env var(s) (project=%s env=%s): %s",
                    len(r98_1_synced), project_id, env_name,
                    [f"{k}={v[:8] if isinstance(v, str) else v}..." for k, v in r98_1_synced.items()],
                )
                # Track in auto_filled_envvars for the response so the
                # modal can show "X auto-synced from JWT".
                for k in r98_1_synced.keys():
                    auto_filled_envvars.append({"name": k, "tier": "jwt_claim"})
        except Exception as _r98_1_exc:
            log.warning(
                "R98.1: JWT env_block sync failed for project=%s: %s",
                project_id, _r98_1_exc,
            )

    # R75.2 — stamp last_paste_at so opaque (non-JWT) cookies can have
    # synthetic TTL computed downstream by /auth-staleness. Pre-R75.2
    # opaque cookies always returned state=unknown because there was no
    # exp claim to compute TTL from + no paste timestamp to fall back
    # to. Now the staleness endpoint can do `(now - last_paste_at) /
    # ttl_hours_configured` arithmetic and return fresh / stale_soon /
    # expired like JWT cookies do.
    try:
        from datetime import datetime as _dt_75_2, timezone as _tz_75_2
        _env_creds_75_2 = (env_block or {}).get("auth", {}).get("credentials") or {}
        _env_creds_75_2["last_paste_at"] = _dt_75_2.now(_tz_75_2.utc).isoformat()
        # Ensure auth.credentials path exists when we mutate
        env_dict = project["environments"][env_name]
        env_dict.setdefault("auth", {}).setdefault("credentials", {})
        env_dict["auth"]["credentials"]["last_paste_at"] = _env_creds_75_2["last_paste_at"]
        _save_projects()
        log.info(
            "R75.2: stamped last_paste_at=%s on project=%s env=%s",
            _env_creds_75_2["last_paste_at"], project_id, env_name,
        )
    except Exception as _r75_2_exc:
        log.debug("R75.2: last_paste_at stamp failed: %s", _r75_2_exc)

    # R145.A.3 — post-paste auto-purge trigger. Operator just supplied
    # fresh env_block values via this paste flow → R145.A.2's
    # substitution success rate climbs sharply. Best-effort; failures
    # log warn but DO NOT break the paste response.
    _r145_a_3_post_paste_audit = None
    try:
        from ..main import _r145_a_3_autopurge
        _r145_a_3_post_paste_audit = _r145_a_3_autopurge("post_paste", project_id)
    except Exception as _r145_a3_exc:
        log.debug("R145.A.3: post-paste auto-purge skipped: %s", _r145_a3_exc)

    return {
        "status": "ok",
        "storage_path": str(new_path),
        "expires_at": _to_iso(payload.get("exp")) if payload else None,
        "cookie_name": cookie_name,
        "env_name": env_name,
        "refresh_token_updated": bool(body.refresh_token),
        # R45.3 — surface synchronous discovery result so the modal
        # can show "Discovery harvested N env vars" before closing.
        "discovery_completed": discovery_completed,
        "envvars_harvested": envvars_harvested,
        "discovery_diagnosis": discovery_diagnosis,
        # R48.1 — list the auto-filled cookie-aliased env vars so the
        # modal can show "✓ Auto-filled cookie_value, cookie0_value..."
        # and operator catches mis-aliases.
        "auto_filled_envvars": auto_filled_envvars,
        "auto_fill_caveat": (
            "auth_token/bearer/jwt vars were filled with the cookie "
            "value because no separate bearer_token is configured. "
            "Override in Settings if your Bearer differs from the cookie."
            if _cookie_only_project and any(
                v["tier"] == "token_alias_cookie_only" for v in auto_filled_envvars
            ) else None
        ),
        # R145.A.3 — surface what the post-paste auto-purge healed
        "auto_purge_swept": (
            {
                "items_substituted": _r145_a_3_post_paste_audit.get("items_substituted", 0),
                "items_blocked": _r145_a_3_post_paste_audit.get("items_blocked", 0),
            } if _r145_a_3_post_paste_audit else None
        ),
    }

