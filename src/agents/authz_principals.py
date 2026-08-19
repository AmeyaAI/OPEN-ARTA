"""Phase 6c — principal-archetype fixtures (the SUBJECTS of the RBAC oracle).

The RBAC oracle decides expected_status(operation, PRINCIPAL). Steps 1-2 gave
the operation side: the route catalog (op -> permission + scope) and the
permission catalog (role -> permissions). This module supplies the principal
side — the test identities that connect a LOGIN to a role@scope in a realm,
mirroring the manual matrix's 27 archetype columns (U1-U25 + cross-tenant X1/X2).

A principal archetype (canonical schema):

    {"id": "U20", "label": "org-viewer @ org=testorg",
     "login": "u20-login-ref",          # credential ref (email or token var)
     "principal_type": "customer",              # operator | customer | service_account
     "home_org": "testorg",
     "bindings": [{"role": "organizations-viewer", "scope": "org",
                   "target": "testorg"}]}

`resolve_effective_permissions` unions each binding's role_permissions (from the
permission catalog) keyed by (scope, target) — the oracle's lookup: does this
principal hold operation P's permission at operation P's scope/org?

GENERIC + SUT-DATA split (same discipline as authz_discovery/authz_catalog):
the loader + validator + resolver are generic platform code; the archetypes
themselves are SUT DATA in the config layer (`.arta/authz_principals/<pid>.json`),
carrying the SUT's real logins + role bindings — never hard-coded in src/.

DETERMINISTIC (no LLM). Fail-open. Killswitch ARTA_AUTHZ_PRINCIPALS_DISABLE=1.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("arta.authz_principals")

_PRINCIPALS_DIR = Path(".arta/authz_principals")

# Canonical binding scopes an archetype may declare. SUT-agnostic tiers; a SUT
# whose route catalog uses other tier labels still matches by string.
_KNOWN_PRINCIPAL_TYPES = ("operator", "customer", "service_account", "api_key")


def load_principals(project_id: str | None) -> list[dict] | None:
    """The SUT's principal archetypes (config-layer DATA). None when absent —
    the matrix generator has no subjects and RBAC gen stays per-AC. Never raises."""
    if not project_id or os.environ.get("ARTA_AUTHZ_PRINCIPALS_DISABLE") == "1":
        return None
    p = _PRINCIPALS_DIR / f"{project_id}.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        log.warning("authz_principals: load failed for %s: %s", project_id, exc)
        return None
    principals = data.get("principals") if isinstance(data, dict) else data
    return principals if isinstance(principals, list) else None


def persist_principals(project_id: str, principals: list[dict]) -> Path:
    _PRINCIPALS_DIR.mkdir(parents=True, exist_ok=True)
    p = _PRINCIPALS_DIR / f"{project_id}.json"
    p.write_text(json.dumps({"principals": principals}, indent=2))
    return p


def validate_principals(principals: list[dict], catalog: dict | None = None) -> dict:
    """Structural + cross-reference validation against the permission catalog.
    Fail-LOUD (returns issues; never raises) so a bad fixture is visible, not a
    silent wrong matrix. Checks: unique ids, a login, known principal_type,
    at least one binding, and every binding role exists in the catalog."""
    issues: list[str] = []
    seen: set[str] = set()
    known_roles = set((catalog or {}).get("role_permissions") or {})
    for i, p in enumerate(principals or []):
        pid = p.get("id") or f"#{i}"
        if not p.get("id"):
            issues.append(f"{pid}: missing id")
        elif p["id"] in seen:
            issues.append(f"{pid}: duplicate id")
        else:
            seen.add(p["id"])
        if not p.get("login"):
            issues.append(f"{pid}: missing login (credential reference)")
        ptype = p.get("principal_type")
        if ptype and ptype not in _KNOWN_PRINCIPAL_TYPES:
            issues.append(f"{pid}: unknown principal_type {ptype!r}")
        bindings = p.get("bindings") or []
        if not bindings and ptype not in ("service_account", "api_key"):
            issues.append(f"{pid}: no role bindings")
        for b in bindings:
            role = b.get("role")
            if known_roles and role and role not in known_roles:
                issues.append(f"{pid}: binding role {role!r} not in permission catalog")
    return {"valid": not issues, "issue_count": len(issues), "issues": issues,
            "principal_count": len(principals or [])}


def resolve_effective_permissions(principal: dict, catalog: dict) -> dict:
    """A principal's effective permissions keyed by (scope, target) — the
    oracle's lookup unit. Unions each binding's role_permissions from the
    catalog. `target` is "" for platform/global bindings (no org)."""
    role_perms = (catalog or {}).get("role_permissions") or {}
    out: dict[tuple[str, str], set[str]] = {}
    for b in principal.get("bindings") or []:
        key = (b.get("scope") or "", b.get("target") or "")
        out.setdefault(key, set()).update(role_perms.get(b.get("role"), []))
    # JSON-friendly: "scope|target" -> sorted perms
    return {f"{s}|{t}": sorted(v) for (s, t), v in out.items()}


def summarize_principals(project_id: str, catalog: dict | None = None) -> dict:
    """Loaded-fixture summary + validation, for the admin surface / oracle
    pre-flight. Empty when no fixtures."""
    principals = load_principals(project_id)
    if not principals:
        return {"principal_count": 0, "loaded": False}
    v = validate_principals(principals, catalog)
    by_type: dict[str, int] = {}
    orgs: set[str] = set()
    for p in principals:
        by_type[p.get("principal_type") or "?"] = by_type.get(p.get("principal_type") or "?", 0) + 1
        if p.get("home_org"):
            orgs.add(p["home_org"])
    return {"loaded": True, "principal_count": len(principals), "by_type": by_type,
            "home_orgs": sorted(orgs), "valid": v["valid"], "issues": v["issues"][:10]}


if __name__ == "__main__":  # smoke check
    _catalog = {"role_permissions": {
        "organizations-viewer": ["org.members.read", "org.projects.read"],
        "iam-admin": ["iam.groups.read", "iam.groups.create"]}}
    _principals = [
        {"id": "U20", "label": "org-viewer @ org=testorg", "login": "u20-login-ref",
         "principal_type": "customer", "home_org": "testorg",
         "bindings": [{"role": "organizations-viewer", "scope": "org", "target": "testorg"}]},
        {"id": "U3", "label": "iam-admin @ platform", "login": "u3-login-ref",
         "principal_type": "operator", "home_org": "vendor",
         "bindings": [{"role": "iam-admin", "scope": "platform", "target": ""}]},
        {"id": "BAD", "login": "", "principal_type": "customer",
         "bindings": [{"role": "no-such-role", "scope": "org", "target": "x"}]},
    ]
    v = validate_principals(_principals, _catalog)
    assert not v["valid"] and v["issue_count"] >= 2, v          # BAD: missing login + bad role
    eff = resolve_effective_permissions(_principals[0], _catalog)
    assert "org.members.read" in eff["org|testorg"]
    eff_admin = resolve_effective_permissions(_principals[1], _catalog)
    assert "iam.groups.create" in eff_admin["platform|"]
    print("authz_principals self-check OK:", v["issue_count"], "issues flagged")
