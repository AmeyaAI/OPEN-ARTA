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

PLUGGABLE MECHANISMS — every SUT handles roles differently, so the oracle is a
REGISTRY of authorization-model strategies, not one fixed rule:
  - rbac_scoped_catalog (default): role -> permission catalog + scope bindings +
    org-tenancy carve-out (permission-catalog RBAC platforms).
  - oauth_scope: OAuth2/OIDC scope claims (the token IS the authorization; no
    catalog, no bindings).
  - simple_rbac: role-gates-operation allowlist (no permission-slug indirection).
Register more with `register_authz_model`. The SUT profile's `authz_mechanism`
selects one (config-layer DATA); the generic framework (route catalog,
principals, matrix emission) is unchanged — only the decision rule swaps.

GENERIC core + SUT POLICY in the profile: the strategies are generic platform
code; which mechanism + its policy inputs (special_orgs, tier names, required
scopes) are config-layer profile DATA, never src/ literals.

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


# ── Pluggable authorization MECHANISMS ────────────────────────────────────────
#
# Every SUT decides "may this principal do this operation?" differently. The
# oracle is therefore a REGISTRY of mechanism strategies, not one fixed rule.
# A strategy is `fn(op, principal, target_org, context) -> {status, tag, reason}`
# where context = {"catalog": <permission catalog>, "profile": <SUT profile>}.
# The SUT profile selects one via `authz_mechanism` (default rbac_scoped_catalog).
# New mechanisms register with `register_authz_model` — the generic framework
# (route catalog, principals, matrix emission) is unchanged; only the decision
# rule swaps. This is the platform/SUT split at the ALGORITHM level: the
# strategies are generic platform code; which one + its policy inputs are SUT
# config-layer DATA.

_AUTHZ_MODELS: dict = {}


def register_authz_model(name: str, fn) -> None:
    _AUTHZ_MODELS[name] = fn


def _exempt_verdict(op: dict) -> dict | None:
    if not op.get("auth_gated"):
        return {"status": op.get("success_status") or 200, "tag": "TS-3",
                "reason": "exempt (not authz-gated)"}
    return None


def _rbac_scoped_catalog(op: dict, principal: dict, target_org: str,
                         context: dict) -> dict:
    """Mechanism: RBAC with a role->permission catalog + scope-tier bindings +
    org-tenancy carve-out (e.g. permission-catalog RBAC platforms). Needs the permission
    catalog (role_permissions) and principal `bindings`. Policy: special_orgs
    (carve-out), tier_binding (tier<->scope names)."""
    exempt = _exempt_verdict(op)
    if exempt:
        return exempt
    profile = context.get("profile") or {}
    catalog = context.get("catalog") or {}
    success = op.get("success_status") or 200
    tier_binding = {**_DEFAULT_TIER_BINDING, **(profile.get("tier_binding") or {})}
    special_orgs = set(profile.get("special_orgs") or [])
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


def _oauth_scope(op: dict, principal: dict, target_org: str, context: dict) -> dict:
    """Mechanism: OAuth2/OIDC scope-based (the token IS the authorization — no
    permission catalog, no role bindings). The principal carries `scopes`; the
    operation's required scope comes from `required_scope` (or an
    `x-required-scope`/OpenAPI security requirement surfaced there), falling
    back to the derived permission used as the scope name. Holds -> success,
    else 403. No carve-out/cross-tenant (scopes are tenant-agnostic tokens)."""
    exempt = _exempt_verdict(op)
    if exempt:
        return exempt
    success = op.get("success_status") or 200
    required = op.get("required_scope") or _perm_of(op)
    scopes = set(principal.get("scopes") or [])
    if required and required in scopes:
        return {"status": success, "tag": "TS-3", "reason": f"token holds scope {required}"}
    return {"status": 403, "tag": "TS-4", "reason": f"token lacks scope {required}"}


def _simple_rbac(op: dict, principal: dict, target_org: str, context: dict) -> dict:
    """Mechanism: plain role-gates-operation RBAC (no permission-slug
    indirection). The operation lists `allowed_roles`; the principal has
    `roles`. Any overlap -> success, else 403. For SUTs that annotate routes
    with a role allowlist directly (Keycloak-role-gated endpoints, simple
    admin/user gates)."""
    exempt = _exempt_verdict(op)
    if exempt:
        return exempt
    success = op.get("success_status") or 200
    allowed = set(op.get("allowed_roles") or [])
    held = set(principal.get("roles") or [])
    if allowed and (allowed & held):
        return {"status": success, "tag": "TS-3", "reason": "role in allowlist"}
    return {"status": 403, "tag": "TS-4", "reason": "no role in allowlist"}


register_authz_model("rbac_scoped_catalog", _rbac_scoped_catalog)
register_authz_model("oauth_scope", _oauth_scope)
register_authz_model("simple_rbac", _simple_rbac)


def expected_status(op: dict, principal: dict, target_org: str,
                    catalog: dict, profile: dict | None = None) -> dict:
    """The authorization verdict for one (operation, principal, target org) cell,
    via the SUT-selected mechanism. Returns {status, tag, reason}. `target_org`
    is the org the operation runs against (the matrix generator supplies it per
    the test design). The mechanism defaults to rbac_scoped_catalog; the SUT
    profile's `authz_mechanism` selects another (oauth_scope, simple_rbac, or a
    registered custom one). An unknown mechanism falls back to the default with
    a loud warning — never a silent wrong verdict."""
    profile = profile or {}
    mech = profile.get("authz_mechanism") or "rbac_scoped_catalog"
    fn = _AUTHZ_MODELS.get(mech)
    if fn is None:
        log.warning("authz_oracle: unknown authz_mechanism %r — falling back to "
                    "rbac_scoped_catalog", mech)
        fn = _rbac_scoped_catalog
    return fn(op, principal, target_org, {"catalog": catalog, "profile": profile})


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

    # ── different SUT, different role mechanism ──
    # OAuth scope-based: principal carries scopes; op needs a scope.
    oauth_profile = {"authz_mechanism": "oauth_scope"}
    op_scope = {"operationId": "readThing", "auth_gated": True, "success_status": 200,
                "required_scope": "things:read"}
    tok_ok = {"id": "T1", "scopes": ["things:read", "things:write"]}
    tok_no = {"id": "T2", "scopes": ["other:read"]}
    assert expected_status(op_scope, tok_ok, "", {}, oauth_profile)["status"] == 200
    assert expected_status(op_scope, tok_no, "", {}, oauth_profile)["status"] == 403

    # simple role allowlist: op lists allowed_roles; principal has roles.
    simple_profile = {"authz_mechanism": "simple_rbac"}
    op_gated = {"operationId": "adminOnly", "auth_gated": True, "success_status": 200,
                "allowed_roles": ["admin"]}
    assert expected_status(op_gated, {"roles": ["admin"]}, "", {}, simple_profile)["status"] == 200
    assert expected_status(op_gated, {"roles": ["user"]}, "", {}, simple_profile)["status"] == 403

    # unknown mechanism -> falls back to default (no crash).
    assert expected_status(op_listGroups, u21, "testcustomer", catalog,
                           {"authz_mechanism": "does-not-exist"})["tag"] == "TS-3"
    print("authz_oracle self-check OK (rbac carve-out/cross-tenant + oauth_scope + simple_rbac)")
