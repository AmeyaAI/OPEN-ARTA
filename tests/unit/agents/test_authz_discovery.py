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


# A SUT profile (config-layer DATA) that maps the generic container names to a
# specific taxonomy — the the SUT shape, expressed as data, NOT baked in src/.
PROFILE = {
    "scope_tiers": {"organizations": "ORG", "projects": "PROJECT", "regions": "PLATFORM"},
    "scope_platform_markers": ["platform", "admin"],
    "domain_aliases": {"organizations": "org"},
}


def _ops(profile=None):
    return {o["operationId"]: o
            for o in A.extract_authz_model(SPEC, profile)["operations"]}


def test_generic_default_scope_is_container_name_no_sut_vocab():
    # NO profile: scope = the owning container's OWN name, derived purely from
    # path structure. Works for any SUT vocabulary (organizations/tenants/…).
    o = _ops()
    assert o["listGroups"]["scope"] == "ORGANIZATIONS"
    assert o["listClusters"]["scope"] == "PROJECTS"
    assert o["listOrganizations"]["scope"] == "GLOBAL"     # no owning container


def test_profile_maps_scope_to_sut_taxonomy():
    # WITH the SUT profile: generic container names fold to the SUT's tiers.
    o = _ops(PROFILE)
    assert o["listGroups"]["scope"] == "ORG"
    assert o["getOrganization"]["scope"] == "ORG"
    assert o["listClusters"]["scope"] == "PROJECT"
    assert o["listPlatformApiKeys"]["scope"] == "PLATFORM"  # platform marker
    assert o["listOrganizations"]["scope"] == "GLOBAL"


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


def test_permission_heuristic_generic_and_aliased():
    # GENERIC (no profile): inner domain from path segments; container prefix
    # kept verbatim (organizations.members.read).
    g = _ops()
    assert g["listGroups"]["permission_guess"] == "iam.groups.read"
    assert g["createGroup"]["permission_guess"] == "iam.groups.create"
    assert g["listOrganizationMembers"]["permission_guess"] == "organizations.members.read"
    assert g["listGroups"]["permission_source"] == "heuristic"
    # PROFILE domain_aliases normalises the container prefix -> org.
    p = _ops(PROFILE)
    assert p["listOrganizationMembers"]["permission_guess"] == "org.members.read"


def test_visibility_mapping():
    o = _ops()
    assert o["listGroups"]["visibility"] == "common"        # public+internal
    assert o["createOrganization"]["visibility"] == "internal"
    assert o["listOrganizations"]["visibility"] == "common"  # empty -> common


def test_configurable_visibility_extension_key():
    # A SUT using a different extension key still folds correctly.
    spec = {"paths": {"/v1/x/{id}/thing": {"get": {
        "operationId": "getThing", "x-surface": ["public", "internal"],
        "responses": {"200": {}, "401": {}, "403": {}}}}}}
    o = {x["operationId"]: x for x in A.extract_authz_model(
        spec, {"visibility_extension": "x-surface"})["operations"]}
    assert o["getThing"]["visibility"] == "common"


def test_summary_counts_and_profile_provenance():
    generic = A.extract_authz_model(SPEC)
    s = generic["summary"]
    assert s["exempt_auth_only"] == 1        # only listOrganizations
    assert s["authz_gated"] == generic["operation_count"] - 1
    assert "iam" in s["domains"] and "organizations" in s["domains"]
    assert s["profile_applied"] is False
    assert A.extract_authz_model(SPEC, PROFILE)["summary"]["profile_applied"] is True


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


def test_profile_loaded_from_config_layer(tmp_path, monkeypatch):
    # A SUT profile is DATA in the config layer, not code — build_authz_model
    # picks it up per project and applies it.
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path / "authz"))
    monkeypatch.setattr(A, "_PROFILE_DIR", Path(tmp_path / "profiles"))
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "pid-6.json").write_text(json.dumps(PROFILE))
    m = A.build_authz_model("pid-6", openapi_doc=SPEC)
    assert m["summary"]["profile_applied"] is True
    by_id = {o["operationId"]: o for o in m["operations"]}
    assert by_id["listGroups"]["scope"] == "ORG"         # profile mapped
    # missing profile => pure generic, never raises
    assert A.load_authz_profile("no-such-pid") == A.GENERIC_DEFAULT_PROFILE
