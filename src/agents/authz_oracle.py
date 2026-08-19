"""Phase 6d — the deterministic RBAC ORACLE (the keystone of derived RBAC gen).

Given the three ingested inputs — route catalog (op -> permission + scope,
authz_discovery), permission catalog (role -> permissions, authz_catalog), and
principals (login -> role@scope, authz_principals) — this computes, WITHOUT an
LLM, the expected authorization outcome for every (operation, principal, target
org) cell: `expected_status(...) -> (status, scenario_tag)`.

This is what makes RBAC generation DERIVED, not guessed. ARTA previously had the
LLM invent 'operator sees all', which contradicts the real model (a platform
grant is 403 on its own special org — the carve-out). The oracle mirrors the
authorization ENGINE the manual matrix is validated against.

Scenario tags (the manual matrix's TS classes):
  TS-3  positive — the role holds the permission at the right scope -> success
  TS-4  negative — lacks the permission / no applicable role -> 403
  TS-6  special-org carve-out — a platform grant is IGNORED inside a special
        (operator/provider) org -> 403
  TS-7  cross-tenant — the principal holds the permission but bound to a
        DIFFERENT org than the target -> 403
  (TS-1 no/expired token -> 401 and TS-5 scope-tier-inert are handled at the
   execution / policy layers; the oracle assumes an authenticated principal and
   the route-catalog scope.)

GENERIC core + SUT POLICY in the profile: the decision logic is generic; the
only SUT-specific inputs are `special_orgs` (carve-out) and the tier<->binding
scope names — both config-layer profile DATA, never src/ literals.

DETERMINISTIC (no LLM). Killswitch ARTA_AUTHZ_ORACLE_DISABLE=1.
"""
from __future__ import annotations

import logging
import os

from .authz_principals import resolve_effective_permissions

log = logging.getLogger("arta.authz_oracle")

# Default tier -> principal-binding-scope correspondence. Generic: a route
# catalog tier (PLATFORM/ORG/PROJECT/GLOBAL) maps to the binding scope a
# principal declares (platform/org/project). GLOBAL authz-gated routes are
# platform-governed. A SUT with other tier names overrides via the profile.
_DEFAULT_TIER_BINDING = {
    "PLATFORM": "platform", "ORG": "org", "PROJECT": "project", "GLOBAL": "platform",
}


def _perm_of(op: dict) -> str | None:
    return op.get("permission") or op.get("permission_guess")


def expected_status(op: dict, principal: dict, target_org: str,
                    catalog: dict, profile: dict | None = None) -> dict:
    """The authorization verdict for one (operation, principal, target org) cell.

    Returns {status, tag, reason}. `target_org` is the org the operation runs
    against (the matrix generator supplies it per the test design: a customer's
    home org, an operator's carve-out/target org, or a foreign org for
    cross-tenant X-cases)."""
    profile = profile or {}
    success = op.get("success_status") or 200
    tier_binding = {**_DEFAULT_TIER_BINDING, **(profile.get("tier_binding") or {})}
    special_orgs = set(profile.get("special_orgs") or [])

    # Exempt (not authz-gated): public / self-service -> success for everyone.
    if not op.get("auth_gated"):
        return {"status": success, "tag": "TS-3", "reason": "exempt (not authz-gated)"}

    perm = _perm_of(op)
    scope = op.get("scope")
    binding_scope = tier_binding.get(scope, str(scope or "").lower())
    eff = resolve_effective_permissions(principal, catalog)
    plat_perms = set(eff.get("platform|", []))

    # PLATFORM / GLOBAL authz-gated: needs the permission via a platform binding.
    if binding_scope == "platform":
        if perm and perm in plat_perms:
            return {"status": success, "tag": "TS-3", "reason": "platform grant holds"}
        return {"status": 403, "tag": "TS-4", "reason": "no platform grant"}

    # ORG-scoped: evaluate against the TARGET org.
    if binding_scope == "org":
        org_here = set(eff.get(f"org|{target_org}", []))
        if perm and perm in org_here:
            return {"status": success, "tag": "TS-3",
                    "reason": f"org grant at {target_org}"}
        if perm and perm in plat_perms:
            # a platform grant crossing INTO an org: ignored inside a special
            # (operator/provider) org (carve-out), else it crosses (e.g. X2).
            if target_org in special_orgs:
                return {"status": 403, "tag": "TS-6",
                        "reason": f"platform grant carved out inside special org {target_org}"}
            return {"status": success, "tag": "TS-3",
                    "reason": f"platform grant crosses into {target_org}"}
        # holds the perm, but bound to a DIFFERENT org -> cross-tenant isolation.
        for key, perms in eff.items():
            if key.startswith("org|") and key != f"org|{target_org}" and perm in perms:
                return {"status": 403, "tag": "TS-7",
                        "reason": "holds permission but bound to a different org"}
        return {"status": 403, "tag": "TS-4", "reason": "no applicable org grant"}

    # PROJECT (or any other tier): needs a matching project/tier binding.
    tier_perms: set[str] = set()
    for key, perms in eff.items():
        if key.startswith(f"{binding_scope}|"):
            tier_perms.update(perms)
    if perm and perm in tier_perms:
        return {"status": success, "tag": "TS-3", "reason": f"{binding_scope} grant holds"}
    return {"status": 403, "tag": "TS-4", "reason": f"no {binding_scope} grant"}


def evaluate_matrix(model: dict, principals: list[dict], *,
                    profile: dict | None = None,
                    target_for=None) -> list[dict]:
    """The full authorization matrix: one verdict per (operation, principal)
    cell. `target_for(op, principal) -> org` chooses the org each cell runs
    against (default: the principal's home_org, or its cross-tenant
    test_target_org override). Returns flat cells for the generator."""
    if os.environ.get("ARTA_AUTHZ_ORACLE_DISABLE") == "1":
        return []
    catalog = {"role_permissions": model.get("role_permissions") or {}}
    profile = profile or {}

    def _default_target(op, p):
        return p.get("test_target_org") or p.get("home_org") or ""

    pick = target_for or _default_target
    cells: list[dict] = []
    for op in model.get("operations") or []:
        for p in principals:
            org = pick(op, p)
            v = expected_status(op, p, org, catalog, profile)
            cells.append({
                "operationId": op.get("operationId"),
                "method": op.get("method"), "path": op.get("path"),
                "principal_id": p.get("id"), "login": p.get("login"),
                "target_org": org, "expected_status": v["status"],
                "tag": v["tag"], "reason": v["reason"],
                "permission": _perm_of(op), "scope": op.get("scope"),
            })
    return cells


if __name__ == "__main__":  # smoke check — the carve-out + cross-tenant keystones
    catalog = {"role_permissions": {
        "iam-admin": ["iam.groups.read", "iam.groups.create"],
        "organizations-viewer": ["org.members.read"],
        "platform-admin": ["platform.admin.org.create"]}}
    profile = {"special_orgs": ["vendor"], "platform_tier": "PLATFORM"}
    op_listGroups = {"operationId": "listGroups", "scope": "ORG",
                     "permission": "iam.groups.read", "auth_gated": True, "success_status": 200}
    op_exempt = {"operationId": "listOrganizations", "scope": "GLOBAL",
                 "auth_gated": False, "success_status": 200}

    # operator iam-admin @ platform, evaluated INSIDE its own special org (vendor)
    u3 = {"id": "U3", "principal_type": "operator", "home_org": "vendor",
          "bindings": [{"role": "iam-admin", "scope": "platform", "target": ""}]}
    assert expected_status(op_listGroups, u3, "vendor", catalog, profile)["tag"] == "TS-6"   # carve-out
    assert expected_status(op_listGroups, u3, "customerA", catalog, profile)["tag"] == "TS-3"  # crosses into customer

    # customer iam-admin @ org=testcustomer
    u21 = {"id": "U21", "principal_type": "customer", "home_org": "testcustomer",
           "bindings": [{"role": "iam-admin", "scope": "org", "target": "testcustomer"}]}
    assert expected_status(op_listGroups, u21, "testcustomer", catalog, profile)["tag"] == "TS-3"  # positive
    assert expected_status(op_listGroups, u21, "otherorg", catalog, profile)["tag"] == "TS-7"      # cross-tenant

    # customer org-viewer lacks iam.groups.read
    u20 = {"id": "U20", "principal_type": "customer", "home_org": "testcustomer",
           "bindings": [{"role": "organizations-viewer", "scope": "org", "target": "testcustomer"}]}
    assert expected_status(op_listGroups, u20, "testcustomer", catalog, profile)["tag"] == "TS-4"  # negative

    # exempt op -> everyone 200
    assert expected_status(op_exempt, u20, "testcustomer", catalog, profile)["status"] == 200
    print("authz_oracle self-check OK (TS-3/4/6/7 + exempt)")
