"""Phase 6 — authorization-model ingestion from the SUT's OpenAPI contract.

WHY. Manual RBAC test plans (e.g. the the SUT Authorization Coverage
Matrix) are COMPUTED from the SUT's authorization source of truth — a route
catalog (which operation needs which permission at which scope) + a permission
catalog (role -> permission bindings). ARTA historically generated RBAC
assertions by LLM-guessing per acceptance-criterion, which produced
expectations that CONTRADICT the real model (the special-org carve-out: a
platform-admin is 403 on its OWN operator org, not 200). A generated RBAC test
that asserts the wrong authorization is worse than none — it is a false green.

This module is the first concrete step toward a DERIVED (not guessed) authz
oracle: it ingests what the OpenAPI contract deterministically carries — the
route-catalog half of the manual plan's two inputs.

From each operation the OpenAPI reliably yields four of the five matrix axes:
  - operation   : operationId, method, path
  - scope       : PLATFORM | ORG | PROJECT | GLOBAL   (from path structure)
  - visibility  : public | internal | common          (x-visibility)
  - success     : the declared 2xx code (200/201/202/204)
  - auth_gated  : has a 403 response (vs auth-only 401 -> exempt/self-service)

The FIFTH axis — the exact permission string (iam.groups.read) and the
role->permission bindings — is NOT in OpenAPI; it lives in the SUT code catalog
(roleperms.go). This module derives a BEST-EFFORT permission (domain.resource.
verb) tagged permission_source="heuristic" and leaves a clean seam for a later
catalog ingestion to override it (permission_source="catalog"). The RBAC ORACLE
(expected_status per principal) and the MATRIX GENERATOR are separate steps.

DETERMINISTIC (no LLM). SUT-agnostic: keys (x-visibility, responses, path
params) are OpenAPI-standard shapes, not the SUT literals. Fail-open. Killswitch
ARTA_AUTHZ_INGEST_DISABLE=1.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("arta.authz_discovery")

_AUTHZ_DIR = Path(".arta/authz")

# HTTP method -> canonical CRUD verb (mirrors the manual matrix's permission
# suffixes: read/create/update/delete). PUT and PATCH both mean "update".
_VERB_BY_METHOD = {
    "get": "read", "post": "create", "put": "update",
    "patch": "update", "delete": "delete",
}

# A path segment is a variable placeholder when wrapped in braces.
_PARAM_RE = re.compile(r"^\{.+\}$")

# ── SUT authorization PROFILE — the ONLY place SUT-specific vocabulary lives ──
#
# The engine below is fully generic: it reads structure the OpenAPI standard
# guarantees (path-param nesting, response codes, an x-* visibility key). A
# profile only RENAMES/refines that generic output into a SUT's own taxonomy —
# its scope-tier names, permission-prefix conventions, visibility folding.
#
# NEVER hard-code a specific SUT's vocabulary here. A profile is DATA, loaded
# from the config layer (`.arta/authz_profile/<pid>.json`). The default below is
# NEUTRAL: every SUT gets meaningful output with NO profile (scope = the owning
# container's name, permission = <domain>.<resource>.<verb> from its own path
# segments). A profile is an optional refinement, never a requirement.
_PROFILE_DIR = Path(".arta/authz_profile")

GENERIC_DEFAULT_PROFILE: dict = {
    # container-segment name -> scope-tier label; "" (no owning container) uses
    # `root_tier`. Empty => the generic fallback (tier = container name upper).
    "scope_tiers": {},
    "root_tier": "GLOBAL",
    # any of these path segments forces the top tier (overrides container).
    "scope_platform_markers": [],
    "platform_tier": "PLATFORM",
    # permission-prefix normalisation, e.g. {"organizations": "org"}.
    "domain_aliases": {},
    # which OpenAPI extension key carries portal/surface visibility.
    "visibility_extension": "x-visibility",
    # visibility values that, when BOTH present, fold to a single label.
    "visibility_common_pair": ["public", "internal"],
    "visibility_common_label": "common",
    "visibility_default": "common",
}


def load_authz_profile(project_id: str | None) -> dict:
    """The SUT profile from the config/data layer, merged over the neutral
    default. Absent profile => pure-generic behaviour. Never raises."""
    profile = dict(GENERIC_DEFAULT_PROFILE)
    if not project_id:
        return profile
    p = _PROFILE_DIR / f"{project_id}.json"
    if p.is_file():
        try:
            profile.update(json.loads(p.read_text()) or {})
        except Exception as exc:
            log.warning("authz_discovery: profile load failed for %s: %s "
                        "— using generic default", project_id, exc)
    return profile


def _segments(path: str) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def _scope_container(path: str) -> str:
    """The owning resource-container: the DEEPEST named segment immediately
    followed by an id param. Fully generic — `organizations/{}`, `tenants/{}`,
    `accounts/{}`, `projects/{}` all resolve to that container's name. Empty for
    a root collection (no owning id in the path)."""
    segs = _segments(path)
    container = ""
    for i, s in enumerate(segs):
        if (i + 1 < len(segs) and _PARAM_RE.match(segs[i + 1])
                and not _PARAM_RE.match(s)):
            container = s.lower()   # keep the deepest (last wins)
    return container


def derive_scope(path: str, profile: dict | None = None) -> str:
    """Scope tier from path structure, mapped through the SUT profile.

    Generic path: a `scope_platform_marker` segment -> platform tier; else the
    owning container's tier from `scope_tiers` (falling back to the container
    name uppercased); no owning container -> `root_tier`. With NO profile every
    SUT still gets a sensible tier derived from its own path vocabulary."""
    profile = profile or GENERIC_DEFAULT_PROFILE
    low = [s.lower() for s in _segments(path)]
    for marker in profile.get("scope_platform_markers", []):
        if marker in low:
            return profile.get("platform_tier", "PLATFORM")
    container = _scope_container(path)
    tiers = profile.get("scope_tiers", {})
    if container in tiers:
        return tiers[container]
    if not container:
        return profile.get("root_tier", "GLOBAL")
    return container.upper()


def derive_permission(path: str, method: str, profile: dict | None = None) -> dict:
    """Best-effort <domain>.<resource>.<verb> from path structure. domain/
    resource are read from the SUT's own path segments (no hard-coded domain
    list); `domain_aliases` normalises a container prefix (e.g. organizations ->
    org). Tagged heuristic — the authoritative permission catalog (roleperms.go
    or equivalent) overrides this at the next step."""
    profile = profile or GENERIC_DEFAULT_PROFILE
    aliases = profile.get("domain_aliases", {})
    segs = _segments(path)
    low = [s.lower() for s in segs]
    verb = _VERB_BY_METHOD.get((method or "").lower())
    container = _scope_container(path)
    # resource = last non-param segment (the thing acted on).
    ri = next((i for i in range(len(segs) - 1, -1, -1)
               if not _PARAM_RE.match(segs[i])), None)
    resource = low[ri].replace("-", "") if ri is not None else None
    domain = None
    if ri is not None and resource == container:
        # operation ON the container itself (e.g. get/update the org) — the
        # resource IS the scope; the true resource (settings/…) needs the
        # catalog. domain = the container (aliased); resource unknown.
        domain = aliases.get(container, container)
        resource = None
    elif ri is not None:
        # domain = nearest preceding NON-param segment; if that is the container
        # (resource directly owned by the scope), alias the container prefix.
        for j in range(ri - 1, -1, -1):
            if not _PARAM_RE.match(segs[j]):
                domain = low[j]
                break
        if domain is None or domain == container:
            domain = aliases.get(container, container) if container else domain
        else:
            domain = aliases.get(domain, domain)
    guess = f"{domain}.{resource}.{verb}" if (domain and resource and verb) else None
    return {
        "domain": domain,
        "resource": resource,
        "verb": verb,
        "permission_guess": guess,
        "permission_source": "heuristic",
    }


def _map_visibility(x_visibility, profile: dict | None = None) -> str:
    """Fold the SUT's visibility-extension value to a label. Generic: when BOTH
    of `visibility_common_pair` are present -> `visibility_common_label`; a
    single value keeps its name; empty -> `visibility_default`."""
    profile = profile or GENERIC_DEFAULT_PROFILE
    default = profile.get("visibility_default", "common")
    if x_visibility is None:
        return default
    vals = x_visibility if isinstance(x_visibility, list) else [x_visibility]
    vals = [str(v).strip().lower() for v in vals if str(v).strip()]
    if not vals:
        return default
    pair = [str(v).lower() for v in profile.get("visibility_common_pair", [])]
    if pair and all(v in vals for v in pair):
        return profile.get("visibility_common_label", "common")
    if len(vals) == 1:
        return vals[0]
    return profile.get("visibility_common_label", "common")


def _success_status(responses: dict) -> int | None:
    for code in sorted(responses or {}):
        if str(code).startswith("2"):
            try:
                return int(code)
            except (TypeError, ValueError):
                continue
    return None


def extract_authz_model(openapi_doc: dict, profile: dict | None = None) -> dict:
    """Parse an OpenAPI dict into the per-operation authz catalog. Works on a
    bundled OR $ref'd spec: only response KEYS ('401'/'403'), the visibility
    extension, operationId and the path string are read — none of which need ref
    resolution. `profile` (SUT config-layer data) refines the generic output;
    the neutral default is used when absent."""
    profile = profile or GENERIC_DEFAULT_PROFILE
    vis_key = profile.get("visibility_extension", "x-visibility")
    ops: list[dict] = []
    paths = (openapi_doc or {}).get("paths") or {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _VERB_BY_METHOD or not isinstance(op, dict):
                continue
            responses = op.get("responses") or {}
            resp_keys = {str(k) for k in responses}
            perm = derive_permission(path, method, profile)
            ops.append({
                "operationId": op.get("operationId") or f"{method.upper()} {path}",
                "method": method.upper(),
                "path": path,
                "scope": derive_scope(path, profile),
                "visibility": _map_visibility(op.get(vis_key), profile),
                "auth_required": "401" in resp_keys,
                # exempt (self-service / public) = auth-required but NOT authz-gated
                "auth_gated": "403" in resp_keys,
                "success_status": _success_status(responses),
                "x_status": op.get("x-status"),
                **perm,
            })

    by_scope: dict[str, int] = {}
    by_vis: dict[str, int] = {}
    gated = exempt = 0
    for o in ops:
        by_scope[o["scope"]] = by_scope.get(o["scope"], 0) + 1
        by_vis[o["visibility"]] = by_vis.get(o["visibility"], 0) + 1
        if o["auth_gated"]:
            gated += 1
        elif o["auth_required"]:
            exempt += 1
    profiled = profile is not GENERIC_DEFAULT_PROFILE and profile != GENERIC_DEFAULT_PROFILE
    return {
        "source": "openapi",
        "operation_count": len(ops),
        "operations": ops,
        "summary": {
            "by_scope": by_scope,
            "by_visibility": by_vis,
            "authz_gated": gated,
            "exempt_auth_only": exempt,
            "domains": sorted({o["domain"] for o in ops if o["domain"]}),
            "permission_source": "heuristic (authoritative permission catalog "
                                 "not yet ingested)",
            # provenance: was a SUT profile applied, or pure-generic derivation?
            "profile_applied": bool(profiled),
        },
    }


def _authz_path(project_id: str) -> Path:
    return _AUTHZ_DIR / f"{project_id}.json"


def persist_authz_model(project_id: str, model: dict) -> Path:
    _AUTHZ_DIR.mkdir(parents=True, exist_ok=True)
    p = _authz_path(project_id)
    p.write_text(json.dumps(model, indent=2))
    return p


def load_authz_model(project_id: str) -> dict | None:
    p = _authz_path(project_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.warning("authz_discovery: load failed for %s: %s", project_id, exc)
        return None


def build_authz_model(project_id: str, openapi_doc: dict | None = None) -> dict | None:
    """Entry point: extract + persist the authz catalog for a project.

    Reads the cached OpenAPI (`.arta/openapi/<pid>.json`) when no doc is
    supplied. Returns None (fail-open) when disabled, no spec, or zero ops.
    Killswitch ARTA_AUTHZ_INGEST_DISABLE=1."""
    if os.environ.get("ARTA_AUTHZ_INGEST_DISABLE") == "1":
        return None
    doc = openapi_doc
    if doc is None:
        try:
            from .openapi_cache import _read_cache
            doc = _read_cache(project_id)
        except Exception as exc:
            log.debug("authz_discovery: openapi cache read failed: %s", exc)
            doc = None
    if not isinstance(doc, dict) or not doc.get("paths"):
        log.info("authz_discovery: no OpenAPI spec for %s — authz model skipped "
                 "(RBAC gen stays LLM-grounded until a spec is available)", project_id)
        return None
    profile = load_authz_profile(project_id)   # SUT config-layer data, or generic
    model = extract_authz_model(doc, profile)
    if not model["operations"]:
        return None
    model["project_id"] = project_id
    # Step 2 — permission catalog (role->permission bindings): if the SUT's
    # catalog is available in the config layer, promote heuristic op-permissions
    # to catalog-confirmed + attach role bindings for the oracle. Fail-open: no
    # catalog => the model stays route-catalog-only (heuristic permissions).
    try:
        from .authz_catalog import (load_permission_catalog,
                                    apply_permission_catalog,
                                    extract_permissions_from_openapi)
        catalog = load_permission_catalog(project_id)
        if catalog:
            op_perms = extract_permissions_from_openapi(
                doc, profile.get("permission_extension", "x-permission"))
            apply_permission_catalog(model, catalog, op_perms)
    except Exception as _cat_exc:
        log.debug("authz_catalog: apply skipped for %s: %s", project_id, _cat_exc)
    persist_authz_model(project_id, model)
    s = model["summary"]
    log.info("authz_discovery: %s -> %d ops (%d authz-gated, %d exempt) "
             "scopes=%s domains=%s profile=%s", project_id, model["operation_count"],
             s["authz_gated"], s["exempt_auth_only"], s["by_scope"], s["domains"],
             "sut" if s["profile_applied"] else "generic-default")
    return model


def summarize_authz_for_prompt(project_id: str, *, max_chars: int = 2000,
                               relevant_paths: "set | list | None" = None) -> str:
    """Compact authz catalog for RBAC gen grounding. Empty when no model.
    Lists authz-gated operations with scope + derived permission so the
    generator asserts against the REAL scope/permission rather than inventing
    'operator sees all'. The exempt set is called out so gen never treats an
    exempt 200 as an RBAC privilege.

    `relevant_paths` (path strings this requirement exercises): when provided,
    RANKS the gated ops whose path is relevant FIRST so `max_chars` truncation
    can never drop the op the caller cares about (a correctness concern on large
    SUTs, not just bloat). Unmatched ops still follow, space permitting."""
    model = load_authz_model(project_id)
    if not model:
        return ""
    ops = model["operations"]
    gated = [o for o in ops if o["auth_gated"]]
    exempt = [o for o in ops if o["auth_required"] and not o["auth_gated"]]
    if relevant_paths:
        _rel = {str(p) for p in relevant_paths}
        # relevant-first ordering (stable); a path is relevant if any hint is a
        # substring of the op path or vice-versa (template vs concrete tolerance).
        def _is_rel(o):
            p = o.get("path") or ""
            return any(r in p or p in r for r in _rel)
        gated = sorted(gated, key=lambda o: 0 if _is_rel(o) else 1)
    lines = [
        "# SUT AUTHORIZATION MODEL (derived from OpenAPI — route catalog)",
        f"# {len(gated)} authz-gated ops, {len(exempt)} exempt (auth-only, "
        "NOT an RBAC privilege — never assert role breadth from these).",
        "# scope/permission are the REAL contract; assert against these, never guess.",
    ]
    for o in gated:
        lines.append(
            f"{o['method']} {o['path']} [{o['scope']}] "
            f"perm~{o['permission_guess'] or '?'} ok={o['success_status']}")
        if sum(len(x) for x in lines) > max_chars:
            lines.append("# … (truncated)")
            break
    return "\n".join(lines)


if __name__ == "__main__":  # smoke check
    _sample = {"paths": {
        "/v1/regions/global/organizations/{orgId}/iam/groups": {
            "get": {"operationId": "listGroups", "x-visibility": ["public", "internal"],
                    "responses": {"200": {}, "401": {}, "403": {}}},
            "post": {"operationId": "createGroup", "x-visibility": ["public", "internal"],
                     "responses": {"201": {}, "401": {}, "403": {}}}},
        "/v1/regions/global/organizations": {
            "get": {"operationId": "listOrganizations",
                    "responses": {"200": {}, "401": {}}},  # exempt: no 403
            "post": {"operationId": "createOrganization", "x-visibility": "internal",
                     "responses": {"202": {}, "401": {}, "403": {}}}},
        "/v1/regions/{region}/projects/{project}/compute/clusters": {
            "get": {"operationId": "listClusters",
                    "responses": {"200": {}, "401": {}, "403": {}}}},
    }}
    # (1) GENERIC default — no SUT vocabulary: scope = the owning container's
    # own name; universal axes (auth_gated/success/visibility) are exact.
    g = {o["operationId"]: o for o in extract_authz_model(_sample)["operations"]}
    assert g["listGroups"]["scope"] == "ORGANIZATIONS"      # container name
    assert g["listClusters"]["scope"] == "PROJECTS"
    assert g["createOrganization"]["scope"] == "GLOBAL"     # root_tier
    assert g["listGroups"]["permission_guess"] == "iam.groups.read"
    assert g["createGroup"]["permission_guess"] == "iam.groups.create"
    assert g["listOrganizations"]["auth_gated"] is False    # exempt (401 only)
    assert g["createOrganization"]["success_status"] == 202
    assert g["listGroups"]["visibility"] == "common"

    # (2) SUT PROFILE (config-layer data) refines to the SUT's taxonomy.
    _profile = {"scope_tiers": {"organizations": "ORG", "projects": "PROJECT",
                                "regions": "PLATFORM"},
                "domain_aliases": {"organizations": "org"}}
    s = {o["operationId"]: o for o in extract_authz_model(_sample, _profile)["operations"]}
    assert s["listGroups"]["scope"] == "ORG"
    assert s["listClusters"]["scope"] == "PROJECT"
    assert s["listGroups"]["permission_guess"] == "iam.groups.read"  # inner domain
    print("authz_discovery self-check OK (generic + profile)")
