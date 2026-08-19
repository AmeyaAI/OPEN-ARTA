"""authz_discovery — OpenAPI authorization-model ingestion (route-catalog half).

Fixtures mirror a REAL SUT contract shape (confirmed against
api/contract/paths/*): operations carry operationId, x-visibility, x-status,
and 401/403 responses; the permission string is NOT in the spec (derived
heuristically). Expected values cross-checked against the v5.6 Authorization
Coverage Matrix."""
import json
from pathlib import Path

from src.agents import authz_discovery as A

# One representative operation per scope class + the exempt case, in the exact
# shape the the SUT contract emits.
SPEC = {"paths": {
    "/v1/regions/global/organizations/{orgId}/iam/groups": {
        "get":  {"operationId": "listGroups", "x-visibility": ["public", "internal"],
                 "x-status": "mvp", "responses": {"200": {}, "401": {}, "403": {}}},
        "post": {"operationId": "createGroup", "x-visibility": ["public", "internal"],
                 "responses": {"201": {}, "401": {}, "403": {}}},
    },
    "/v1/regions/global/organizations": {
        "get":  {"operationId": "listOrganizations",            # exempt: 401 only
                 "responses": {"200": {}, "401": {}}},
        "post": {"operationId": "createOrganization", "x-visibility": "internal",
                 "responses": {"202": {}, "401": {}, "403": {}}},
    },
    "/v1/regions/global/organizations/{id}": {
        "get":  {"operationId": "getOrganization",
                 "responses": {"200": {}, "401": {}, "403": {}}},
    },
    "/v1/regions/global/organizations/{id}/members": {
        "get":  {"operationId": "listOrganizationMembers",
                 "responses": {"200": {}, "401": {}, "403": {}}},
    },
    "/v1/regions/{region}/projects/{project}/compute/clusters": {
        "get":  {"operationId": "listClusters",
                 "responses": {"200": {}, "401": {}, "403": {}}},
    },
    "/v1/regions/global/iam/platform/apikeys": {
        "get":  {"operationId": "listPlatformApiKeys", "x-visibility": "internal",
                 "responses": {"200": {}, "401": {}, "403": {}}},
    },
}}


def _ops():
    return {o["operationId"]: o for o in A.extract_authz_model(SPEC)["operations"]}


def test_scope_derivation_matches_matrix():
    o = _ops()
    assert o["listGroups"]["scope"] == "ORG"               # org-scoped subroute
    assert o["getOrganization"]["scope"] == "ORG"          # org-id resource
    assert o["listClusters"]["scope"] == "PROJECT"         # {project} param
    assert o["listPlatformApiKeys"]["scope"] == "PLATFORM" # /platform/ segment
    assert o["listOrganizations"]["scope"] == "GLOBAL"     # bare root list


def test_exempt_vs_authz_gated():
    o = _ops()
    # THE key axis: listOrganizations is 200* exempt (401 only, no 403) — never
    # an RBAC privilege. Prevents 'operator sees all via listOrganizations'.
    assert o["listOrganizations"]["auth_gated"] is False
    assert o["listOrganizations"]["auth_required"] is True
    assert o["createOrganization"]["auth_gated"] is True


def test_success_status_from_declared_2xx():
    o = _ops()
    assert o["listGroups"]["success_status"] == 200
    assert o["createGroup"]["success_status"] == 201
    assert o["createOrganization"]["success_status"] == 202


def test_permission_heuristic_domain_resource_verb():
    o = _ops()
    # inner domain wins over the org scope container
    assert o["listGroups"]["permission_guess"] == "iam.groups.read"
    assert o["createGroup"]["permission_guess"] == "iam.groups.create"
    assert o["listOrganizationMembers"]["permission_guess"] == "org.members.read"
    assert o["listGroups"]["permission_source"] == "heuristic"


def test_visibility_mapping():
    o = _ops()
    assert o["listGroups"]["visibility"] == "common"        # public+internal
    assert o["createOrganization"]["visibility"] == "internal"
    assert o["listOrganizations"]["visibility"] == "common"  # empty -> common


def test_summary_counts():
    m = A.extract_authz_model(SPEC)
    s = m["summary"]
    assert s["exempt_auth_only"] == 1        # only listOrganizations
    assert s["authz_gated"] == m["operation_count"] - 1
    assert "iam" in s["domains"] and "org" in s["domains"]


def test_persist_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    model = A.extract_authz_model(SPEC)
    model["project_id"] = "pid-1"
    A.persist_authz_model("pid-1", model)
    back = A.load_authz_model("pid-1")
    assert back["operation_count"] == model["operation_count"]
    assert Path(tmp_path / "pid-1.json").is_file()


def test_build_from_doc_and_summary_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    m = A.build_authz_model("pid-2", openapi_doc=SPEC)
    assert m and m["operation_count"] == 8
    block = A.summarize_authz_for_prompt("pid-2")
    assert "AUTHORIZATION MODEL" in block
    assert "iam.groups.read" in block
    # exempt op must NOT appear as an authz-gated privilege line
    assert "listOrganizations" not in block


def test_killswitch(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    monkeypatch.setenv("ARTA_AUTHZ_INGEST_DISABLE", "1")
    assert A.build_authz_model("pid-3", openapi_doc=SPEC) is None


def test_no_spec_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    assert A.build_authz_model("pid-4", openapi_doc={"paths": {}}) is None
    assert A.build_authz_model("pid-5", openapi_doc=None) is None
