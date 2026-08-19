"""Phase 6e — RBAC matrix GENERATOR (the derived-RBAC delivery step).

The oracle (authz_oracle.evaluate_matrix) produces one verdict per (operation,
principal) cell: {operationId, method, path, principal_id, login, target_org,
expected_status, tag, permission, scope}. This module turns those cells into
concrete, runnable Newman test items — each hitting the operation as a specific
principal and asserting the oracle's expected status. That is the whole point of
the derived pipeline: tests whose expectation is COMPUTED from the SUT's authz
model, not guessed.

MECHANISM-INDEPENDENT: the generator consumes the uniform cell shape regardless
of whether the verdict came from rbac_scoped_catalog, oauth_scope, or simple_rbac.

NON-MUTATION SAFETY (R154): read-side (GET/HEAD) cells and any cell whose
expected outcome is a REJECTION (4xx) are safe — a rejected write never mutates.
Cells that would SUCCEED on a mutating method (POST/PUT/PATCH/DELETE expecting
2xx) are SKIPPED by default and only emitted with `include_successful_mutations`
(which requires the operator's R154 destructive opt-in at dispatch). The skipped
count is reported — never silently dropped.

Per-principal auth: each item sends `Authorization: Bearer {{<token_var>}}` where
the token var (default `<principal_id>_token`) resolves from the config-layer env
(the durable-var resolution). Path params: the org container -> target_org; other
params -> `{{<param>}}` env vars.

DETERMINISTIC (no LLM). Killswitch ARTA_AUTHZ_MATRIX_GEN_DISABLE=1.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("arta.authz_matrix_gen")

_PARAM_RE = re.compile(r"\{([^}]+)\}")
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Path-param names that denote the ORG container (substituted with target_org).
# Generic: covers the common id spellings; a SUT can extend via `org_param_names`.
_DEFAULT_ORG_PARAMS = ("orgid", "organizationid", "org_id", "organization_id")


def _default_token_var(principal_id: str) -> str:
    return f"{principal_id}_token"


def _template_url(path: str, target_org: str, org_params: tuple) -> str:
    """Substitute the org container param with target_org; every other path
    param becomes a `{{param}}` Newman variable (resolved from the env, or left
    for the operator to fill)."""
    def _sub(m):
        name = m.group(1)
        if name.lower().replace("-", "").replace("_", "") in org_params:
            return target_org or f"{{{{{name}}}}}"
        return f"{{{{{name}}}}}"
    return _PARAM_RE.sub(_sub, path)


def _test_script(expected_status: int, tag: str) -> list[str]:
    return [
        f"// {tag} — derived RBAC expectation",
        f"pm.test('{tag}: expects {expected_status}', function () {{",
        f"    pm.expect(pm.response.code).to.eql({int(expected_status)});",
        "});",
    ]


def cell_to_newman_item(cell: dict, *, base_url_var: str, token_var_for,
                        org_params: tuple) -> dict:
    method = (cell.get("method") or "GET").upper()
    url = f"{{{{{base_url_var}}}}}" + _template_url(
        cell.get("path") or "", cell.get("target_org") or "", org_params)
    token_var = token_var_for(cell.get("principal_id") or "")
    name = (f"[{cell.get('tag')}] {cell.get('operationId')} as "
            f"{cell.get('principal_id')}@{cell.get('target_org') or '-'} "
            f"-> {cell.get('expected_status')}")
    return {
        "name": name,
        "request": {
            "method": method,
            "header": [{"key": "Authorization", "value": f"Bearer {{{{{token_var}}}}}"}],
            "url": {"raw": url},
        },
        "event": [{"listen": "test", "script": {
            "type": "text/javascript",
            "exec": _test_script(cell.get("expected_status") or 200, cell.get("tag") or "")}}],
        # provenance for the dashboard / traceability
        "_arta_authz": {
            "principal_id": cell.get("principal_id"), "tag": cell.get("tag"),
            "permission": cell.get("permission"), "scope": cell.get("scope"),
            "expected_status": cell.get("expected_status"),
        },
    }


def generate_newman_collection(cells: list[dict], *, collection_name: str = "authz-matrix",
                               base_url_var: str = "base_url", token_var_for=None,
                               org_param_names=None,
                               include_successful_mutations: bool = False) -> dict:
    """Build a Newman collection from oracle cells, applying R154 non-mutation
    safety. Returns {collection, stats}. Empty collection when disabled."""
    if os.environ.get("ARTA_AUTHZ_MATRIX_GEN_DISABLE") == "1":
        return {"collection": None, "stats": {"emitted": 0, "disabled": True}}
    token_var_for = token_var_for or _default_token_var
    org_params = tuple(n.lower().replace("-", "").replace("_", "")
                       for n in (org_param_names or _DEFAULT_ORG_PARAMS))
    items: list[dict] = []
    skipped_mutation = 0
    for c in cells or []:
        method = (c.get("method") or "GET").upper()
        status = c.get("expected_status") or 200
        is_success = 200 <= int(status) < 300
        # Non-mutation guarantee: a mutating method that would SUCCEED actually
        # writes — skip unless the operator opts in (R154 dispatch gate).
        if method in _MUTATING and is_success and not include_successful_mutations:
            skipped_mutation += 1
            continue
        items.append(cell_to_newman_item(
            c, base_url_var=base_url_var, token_var_for=token_var_for, org_params=org_params))
    collection = {
        "info": {"name": collection_name,
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "item": items,
    }
    stats = {"emitted": len(items), "skipped_successful_mutations": skipped_mutation,
             "total_cells": len(cells or [])}
    if skipped_mutation:
        log.info("authz_matrix_gen: %d successful-mutation cell(s) SKIPPED (R154 "
                 "non-mutation default; set include_successful_mutations + the "
                 "destructive opt-in to emit them)", skipped_mutation)
    return {"collection": collection, "stats": stats}


if __name__ == "__main__":  # smoke check
    cells = [
        {"operationId": "listGroups", "method": "GET",
         "path": "/v1/regions/global/organizations/{orgId}/iam/groups",
         "principal_id": "U20", "target_org": "testcustomer", "expected_status": 403,
         "tag": "TS-4", "permission": "iam.groups.read", "scope": "ORG"},
        {"operationId": "listGroups", "method": "GET",
         "path": "/v1/regions/global/organizations/{orgId}/iam/groups",
         "principal_id": "U21", "target_org": "testcustomer", "expected_status": 200,
         "tag": "TS-3", "permission": "iam.groups.read", "scope": "ORG"},
        {"operationId": "createGroup", "method": "POST",   # successful mutation -> skipped
         "path": "/v1/regions/global/organizations/{orgId}/iam/groups",
         "principal_id": "U21", "target_org": "testcustomer", "expected_status": 201,
         "tag": "TS-3"},
        {"operationId": "createGroup", "method": "POST",   # rejected mutation -> safe, emitted
         "path": "/v1/regions/global/organizations/{orgId}/iam/groups",
         "principal_id": "U20", "target_org": "testcustomer", "expected_status": 403,
         "tag": "TS-4"},
    ]
    out = generate_newman_collection(cells)
    assert out["stats"]["emitted"] == 3, out["stats"]         # 2 GET + 1 rejected POST
    assert out["stats"]["skipped_successful_mutations"] == 1  # the 201 POST
    item0 = out["collection"]["item"][0]
    assert "testcustomer" in item0["request"]["url"]["raw"]   # org substituted
    assert "U20_token" in item0["request"]["header"][0]["value"]
    assert "403" in "".join(item0["event"][0]["script"]["exec"])
    print("authz_matrix_gen self-check OK:", out["stats"])
