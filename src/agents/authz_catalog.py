"""Phase 6b — permission-catalog ingestion (the PERMISSION-catalog half of the
RBAC oracle; sibling to authz_discovery's route catalog).

The manual RBAC matrix has TWO inputs: a route catalog (operation -> permission
+ scope — authz_discovery derives this from OpenAPI) and a PERMISSION catalog
(role -> permissions — this module). authz_discovery's permission per operation
is a heuristic; the permission catalog is the authoritative role->permission
binding the oracle needs to decide expected_status per principal.

This module ingests a permission catalog in the common declarative shape:

    permissionGroups: {<group>: [<perm slug>, ...]}   # shared permission families
    roles:
      - slug: <role>
        bindingScopes: [<scope>, ...]                 # tiers the role may bind at
        permissionGroups: [<group>, ...]              # referenced families
        permissions: [<perm slug>, ...]               # the role's own slugs
    bindingScopes: [...]                               # (informational)

into a canonical form the oracle consumes:

    {permission_slugs: {..}, role_permissions: {role: [effective perms]},
     role_binding_scopes: {role: [scopes]}}

GENERIC + SUT-DATA split (same discipline as authz_discovery): the PARSER +
consumer are generic platform code; the CATALOG itself is SUT DATA loaded from
the config layer (`.arta/authz_catalog/<pid>.json`), never hard-coded in src/.
Field names are configurable for SUTs whose catalog uses different keys.

DETERMINISTIC (no LLM). Fail-open. Killswitch ARTA_AUTHZ_CATALOG_DISABLE=1.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("arta.authz_catalog")

_CATALOG_DIR = Path(".arta/authz_catalog")

# Default field names for the declarative catalog shape above. A SUT whose
# catalog uses other keys overrides these via the authz_profile
# (catalog_field_map) — still config-layer data, never a src/ literal.
_DEFAULT_FIELDS = {
    "permission_groups": "permissionGroups",
    "roles": "roles",
    "role_slug": "slug",
    "role_own_permissions": "permissions",
    "role_referenced_groups": "permissionGroups",
    "role_binding_scopes": "bindingScopes",
}


def parse_permission_matrix(doc: dict, fields: dict | None = None) -> dict:
    """Resolve a declarative permission catalog into canonical role->permission
    bindings. Effective role permissions = own `permissions` UNION every
    referenced permission group. Generic: only the (configurable) field names
    are read; no SUT-specific slugs are assumed."""
    f = {**_DEFAULT_FIELDS, **(fields or {})}
    doc = doc or {}
    groups = doc.get(f["permission_groups"]) or {}
    if not isinstance(groups, dict):
        groups = {}
    all_slugs: set[str] = set()
    for g_perms in groups.values():
        for s in (g_perms or []):
            all_slugs.add(str(s))

    role_permissions: dict[str, list[str]] = {}
    role_binding_scopes: dict[str, list[str]] = {}
    for role in (doc.get(f["roles"]) or []):
        if not isinstance(role, dict):
            continue
        slug = role.get(f["role_slug"])
        if not slug:
            continue
        eff: set[str] = set(str(s) for s in (role.get(f["role_own_permissions"]) or []))
        for gname in (role.get(f["role_referenced_groups"]) or []):
            eff.update(str(s) for s in (groups.get(gname) or []))
        all_slugs.update(eff)
        role_permissions[slug] = sorted(eff)
        role_binding_scopes[slug] = list(role.get(f["role_binding_scopes"]) or [])
    return {
        "permission_slugs": sorted(all_slugs),
        "role_permissions": role_permissions,
        "role_binding_scopes": role_binding_scopes,
        "role_count": len(role_permissions),
        "permission_count": len(all_slugs),
    }


def extract_permissions_from_openapi(openapi_doc: dict,
                                     extension_key: str = "x-permission") -> dict:
    """Generic adapter: operation -> permission from an OpenAPI extension, for
    SUTs whose spec carries the permission per operation (many enterprise APIs
    do; the SUT probed here does not — its permissions live in a separate
    matrix). Returns {operationId: perm}. Empty when the extension is absent."""
    out: dict[str, str] = {}
    for path, item in ((openapi_doc or {}).get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            perm = op.get(extension_key)
            opid = op.get("operationId") or f"{method.upper()} {path}"
            if isinstance(perm, str) and perm.strip():
                out[opid] = perm.strip()
    return out


def _catalog_path(project_id: str) -> Path:
    return _CATALOG_DIR / f"{project_id}.json"


def load_permission_catalog(project_id: str | None) -> dict | None:
    """Load the parsed permission catalog (SUT DATA) from the config layer.
    None when absent — the model stays heuristic-only. Never raises."""
    if not project_id or os.environ.get("ARTA_AUTHZ_CATALOG_DISABLE") == "1":
        return None
    p = _catalog_path(project_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        log.warning("authz_catalog: load failed for %s: %s", project_id, exc)
        return None


def persist_permission_catalog(project_id: str, catalog: dict) -> Path:
    _CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    p = _catalog_path(project_id)
    p.write_text(json.dumps(catalog, indent=2))
    return p


def apply_permission_catalog(model: dict, catalog: dict,
                             op_permissions: dict | None = None) -> dict:
    """Promote authz_discovery's heuristic op-permissions against the
    authoritative catalog and attach role bindings for the oracle.

    - If an operation's permission is known from the OpenAPI extension
      (`op_permissions[operationId]`), that value wins -> permission_source
      "catalog".
    - Else if the heuristic guess IS a real slug in the catalog, promote it
      (guess -> permission, source "catalog-confirmed") — the catalog confirms
      the route-catalog derivation without needing authz.go.
    - Else the guess stays heuristic (honest — never fabricate a match).

    Attaches role_permissions + role_binding_scopes to the model so the
    downstream oracle can decide expected_status per principal."""
    slugs = set(catalog.get("permission_slugs") or [])
    op_permissions = op_permissions or {}
    confirmed = promoted = 0
    for op in model.get("operations") or []:
        opid = op.get("operationId")
        explicit = op_permissions.get(opid)
        if explicit:
            op["permission"] = explicit
            op["permission_source"] = "catalog"
            confirmed += 1
        elif op.get("permission_guess") and op["permission_guess"] in slugs:
            op["permission"] = op["permission_guess"]
            op["permission_source"] = "catalog-confirmed"
            promoted += 1
        else:
            op["permission"] = op.get("permission_guess")
            # permission_source stays "heuristic"
    model["role_permissions"] = catalog.get("role_permissions") or {}
    model["role_binding_scopes"] = catalog.get("role_binding_scopes") or {}
    model.setdefault("summary", {}).update({
        "permission_source": "catalog (with heuristic fallback for unmatched ops)",
        "permission_catalog_applied": True,
        "permissions_from_extension": confirmed,
        "permissions_catalog_confirmed": promoted,
        "permissions_heuristic_only": sum(
            1 for o in model.get("operations") or []
            if o.get("permission_source") == "heuristic"),
        "roles_in_catalog": len(model["role_permissions"]),
    })
    log.info("authz_catalog: applied to %s — %d explicit, %d confirmed, %d roles",
             model.get("project_id"), confirmed, promoted, len(model["role_permissions"]))
    return model


if __name__ == "__main__":  # smoke check — mirrors the real catalog shape
    _doc = {
        "permissionGroups": {"selfServiceUniversal": ["iam.apikeys.read", "iam.profile.read"]},
        "roles": [
            {"slug": "iam-admin", "bindingScopes": ["platform", "org"],
             "permissionGroups": ["selfServiceUniversal"],
             "permissions": ["iam.groups.read", "iam.groups.create"]},
            {"slug": "iam-viewer", "bindingScopes": ["platform", "org"],
             "permissions": ["iam.groups.read"]},
        ],
    }
    cat = parse_permission_matrix(_doc)
    assert cat["role_count"] == 2
    assert "iam.groups.read" in cat["role_permissions"]["iam-admin"]
    assert "iam.apikeys.read" in cat["role_permissions"]["iam-admin"]   # via group
    assert "iam.groups.create" not in cat["role_permissions"]["iam-viewer"]
    assert cat["role_binding_scopes"]["iam-admin"] == ["platform", "org"]

    _model = {"project_id": "x", "operations": [
        {"operationId": "listGroups", "permission_guess": "iam.groups.read",
         "permission_source": "heuristic"},
        {"operationId": "listClusters", "permission_guess": "compute.clusters.read",
         "permission_source": "heuristic"}]}
    apply_permission_catalog(_model, cat)
    ops = {o["operationId"]: o for o in _model["operations"]}
    assert ops["listGroups"]["permission_source"] == "catalog-confirmed"  # real slug
    assert ops["listClusters"]["permission_source"] == "heuristic"        # not in catalog
    assert _model["role_permissions"]["iam-admin"]
    print("authz_catalog self-check OK:", _model["summary"])
