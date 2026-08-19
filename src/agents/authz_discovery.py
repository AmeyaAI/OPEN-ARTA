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

# Domain segments ARTA recognises in a path (the manual matrix's permission
# prefixes). Order matters only for logging; membership is what's used.
_KNOWN_DOMAINS = ("iam", "compute", "infrastructure", "organizations",
                  "auth", "workflow", "storage", "notifications", "audit")

# A path segment is a variable placeholder when wrapped in braces.
_PARAM_RE = re.compile(r"^\{.+\}$")


def _segments(path: str) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def derive_scope(path: str) -> str:
    """PLATFORM | ORG | PROJECT | GLOBAL from path structure alone.

    Matches the manual matrix's Scope column:
      - a {project} placeholder anywhere         -> PROJECT
      - a `/platform/` or `/admin/` segment      -> PLATFORM
      - `/organizations/{param}/<more>` subroute -> ORG
      - else (root lists, region-scoped platform
        resources, /organizations root mutation) -> GLOBAL/PLATFORM fallback
    Ambiguous region-scoped compute/infra routes resolve PLATFORM (they carry
    platform permissions in the matrix); this is refined when the roleperms.go
    catalog lands.
    """
    segs = _segments(path)
    low = [s.lower() for s in segs]
    if any(s in ("{project}", "{projectid}", "{project_id}") for s in low):
        return "PROJECT"
    if "platform" in low or "admin" in low:
        return "PLATFORM"
    # `/organizations/{param}` (with or without a deeper subpath) => ORG-scoped:
    # the target is that org or its children (getOrganization, listMembers,
    # listProjectsInOrg …). Bare `/organizations` (list/create, no id) is NOT
    # ORG — it falls through to GLOBAL. NOTE: platform-admin mutations on an org
    # root (deleteOrganization, suspend/resume) are ORG by path but PLATFORM by
    # permission; only the roleperms.go catalog can split those — a known
    # heuristic limit corrected at the catalog step.
    for i, s in enumerate(low):
        if s in ("organizations", "organization") and i + 1 < len(segs):
            if _PARAM_RE.match(segs[i + 1]):
                return "ORG"
    # region-scoped compute/infrastructure without a project => platform-tier
    if "compute" in low or "infrastructure" in low:
        return "PLATFORM"
    return "GLOBAL"


def derive_permission(path: str, method: str) -> dict:
    """Best-effort domain.resource.verb, tagged heuristic. Reliable parts
    (domain, verb) are separated from the approximate part (resource) so the
    later roleperms.go catalog can override cleanly."""
    segs = _segments(path)
    low = [s.lower() for s in segs]
    verb = _VERB_BY_METHOD.get((method or "").lower())
    # LAST (most-specific) known domain wins: in `/organizations/{orgId}/iam/
    # groups`, `organizations` is the SCOPE container and `iam` is the
    # permission domain (matrix: iam.groups.read, not org.*).
    di = next((i for i in range(len(low) - 1, -1, -1)
               if low[i] in _KNOWN_DOMAINS), None)
    domain = low[di] if di is not None else None
    # normalise the org(anizations) domain prefix to the matrix's `org.`
    domain_prefix = "org" if domain == "organizations" else domain
    resource = None
    if di is not None:
        # first NON-param segment after the domain is the resource
        for s in segs[di + 1:]:
            if not _PARAM_RE.match(s):
                resource = s.lower().replace("-", "")
                break
    guess = None
    if domain_prefix and resource and verb:
        guess = f"{domain_prefix}.{resource}.{verb}"
    return {
        "domain": domain_prefix,
        "resource": resource,
        "verb": verb,
        "permission_guess": guess,
        "permission_source": "heuristic",
    }


def _map_visibility(x_visibility) -> str:
    """x-visibility -> the matrix's Vis column. Both portal surfaces (public
    AND internal) => common; a single surface keeps its name; empty => common."""
    if x_visibility is None:
        return "common"
    vals = x_visibility if isinstance(x_visibility, list) else [x_visibility]
    vals = [str(v).strip().lower() for v in vals if str(v).strip()]
    if not vals:
        return "common"
    has_pub, has_int = "public" in vals, "internal" in vals
    if has_pub and has_int:
        return "common"
    if has_int:
        return "internal"
    if has_pub:
        return "public"
    return "common"


def _success_status(responses: dict) -> int | None:
    for code in sorted(responses or {}):
        if str(code).startswith("2"):
            try:
                return int(code)
            except (TypeError, ValueError):
                continue
    return None


def extract_authz_model(openapi_doc: dict) -> dict:
    """Parse an OpenAPI dict into the per-operation authz catalog. Works on a
    bundled OR $ref'd spec: only response KEYS ('401'/'403'), x-visibility,
    operationId and the path string are read — none of which need ref
    resolution."""
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
            perm = derive_permission(path, method)
            ops.append({
                "operationId": op.get("operationId") or f"{method.upper()} {path}",
                "method": method.upper(),
                "path": path,
                "scope": derive_scope(path),
                "visibility": _map_visibility(op.get("x-visibility")),
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
            "permission_source": "heuristic (roleperms.go catalog not yet ingested)",
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
    model = extract_authz_model(doc)
    if not model["operations"]:
        return None
    model["project_id"] = project_id
    persist_authz_model(project_id, model)
    s = model["summary"]
    log.info("authz_discovery: %s -> %d ops (%d authz-gated, %d exempt) "
             "scopes=%s domains=%s", project_id, model["operation_count"],
             s["authz_gated"], s["exempt_auth_only"], s["by_scope"], s["domains"])
    return model


def summarize_authz_for_prompt(project_id: str, *, max_chars: int = 2000) -> str:
    """Compact authz catalog for later RBAC gen grounding. Empty when no model.
    Lists authz-gated operations with scope + derived permission so the
    generator asserts against the REAL scope/permission rather than inventing
    'operator sees all'. The exempt set is called out so gen never treats an
    exempt 200 as an RBAC privilege."""
    model = load_authz_model(project_id)
    if not model:
        return ""
    ops = model["operations"]
    gated = [o for o in ops if o["auth_gated"]]
    exempt = [o for o in ops if o["auth_required"] and not o["auth_gated"]]
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
    m = extract_authz_model(_sample)
    assert m["operation_count"] == 5, m["operation_count"]
    by_id = {o["operationId"]: o for o in m["operations"]}
    assert by_id["listGroups"]["scope"] == "ORG"
    assert by_id["listGroups"]["permission_guess"] == "iam.groups.read"
    assert by_id["createGroup"]["permission_guess"] == "iam.groups.create"
    assert by_id["listOrganizations"]["auth_gated"] is False  # exempt
    assert by_id["createOrganization"]["scope"] == "GLOBAL"
    assert by_id["createOrganization"]["success_status"] == 202
    assert by_id["listClusters"]["scope"] == "PROJECT"
    assert by_id["listGroups"]["visibility"] == "common"
    assert by_id["createOrganization"]["visibility"] == "internal"
    print("authz_discovery self-check OK:", m["summary"])
