"""authz_catalog — permission-catalog ingestion (role->permission bindings).

Fixture mirrors the real declarative catalog shape (permissionGroups + roles
with own permissions + referenced groups + bindingScopes)."""
import json
from pathlib import Path

from src.agents import authz_catalog as C

MATRIX = {
    "permissionGroups": {
        "selfServiceUniversal": ["iam.apikeys.read", "iam.profile.read"],
        "platformOnlyPerms": ["platform.admin.org.create", "platform.admin.org.delete"],
    },
    "roles": [
        {"slug": "iam-admin", "domain": "iam", "tier": "admin",
         "bindingScopes": ["platform", "org"],
         "permissionGroups": ["selfServiceUniversal"],
         "permissions": ["iam.groups.read", "iam.groups.create", "iam.groups.delete"]},
        {"slug": "iam-viewer", "domain": "iam", "tier": "viewer",
         "bindingScopes": ["platform", "org"],
         "permissionGroups": ["selfServiceUniversal"],
         "permissions": ["iam.groups.read"]},
        {"slug": "platform-admin", "domain": "platform", "tier": "admin",
         "bindingScopes": ["platform"],
         "permissionGroups": ["selfServiceUniversal", "platformOnlyPerms"],
         "permissions": []},
    ],
}


def test_effective_permissions_union_own_and_groups():
    cat = C.parse_permission_matrix(MATRIX)
    assert cat["role_count"] == 3
    admin = set(cat["role_permissions"]["iam-admin"])
    assert {"iam.groups.read", "iam.groups.create"} <= admin        # own
    assert "iam.apikeys.read" in admin                              # via group
    viewer = set(cat["role_permissions"]["iam-viewer"])
    assert "iam.groups.create" not in viewer                        # viewer lacks it
    padmin = set(cat["role_permissions"]["platform-admin"])
    assert "platform.admin.org.create" in padmin                    # group-only role


def test_binding_scopes_captured():
    cat = C.parse_permission_matrix(MATRIX)
    assert cat["role_binding_scopes"]["iam-admin"] == ["platform", "org"]
    assert cat["role_binding_scopes"]["platform-admin"] == ["platform"]


def test_configurable_field_names():
    # A SUT catalog using different keys still parses via the field map.
    doc = {"groups": {"g": ["p.a"]},
           "principals": [{"name": "r1", "grants": ["p.b"], "families": ["g"]}]}
    cat = C.parse_permission_matrix(doc, fields={
        "permission_groups": "groups", "roles": "principals",
        "role_slug": "name", "role_own_permissions": "grants",
        "role_referenced_groups": "families"})
    assert set(cat["role_permissions"]["r1"]) == {"p.a", "p.b"}


def test_apply_promotes_real_slug_leaves_unknown_heuristic():
    cat = C.parse_permission_matrix(MATRIX)
    model = {"project_id": "x", "operations": [
        {"operationId": "listGroups", "permission_guess": "iam.groups.read",
         "permission_source": "heuristic"},
        {"operationId": "listClusters", "permission_guess": "compute.clusters.read",
         "permission_source": "heuristic"},
        {"operationId": "noGuess", "permission_guess": None,
         "permission_source": "heuristic"}]}
    C.apply_permission_catalog(model, cat)
    o = {x["operationId"]: x for x in model["operations"]}
    assert o["listGroups"]["permission_source"] == "catalog-confirmed"  # real slug
    assert o["listGroups"]["permission"] == "iam.groups.read"
    assert o["listClusters"]["permission_source"] == "heuristic"        # not a slug
    assert o["noGuess"]["permission"] is None
    assert model["role_permissions"]["iam-admin"]
    assert model["summary"]["permissions_catalog_confirmed"] == 1


def test_explicit_extension_permission_wins():
    cat = C.parse_permission_matrix(MATRIX)
    model = {"operations": [
        {"operationId": "op1", "permission_guess": "wrong.guess",
         "permission_source": "heuristic"}]}
    C.apply_permission_catalog(model, cat, op_permissions={"op1": "iam.groups.read"})
    assert model["operations"][0]["permission"] == "iam.groups.read"
    assert model["operations"][0]["permission_source"] == "catalog"


def test_extract_permissions_from_openapi_extension():
    doc = {"paths": {"/x": {"get": {"operationId": "getX", "x-permission": "iam.x.read",
                                    "responses": {"200": {}}}}}}
    assert C.extract_permissions_from_openapi(doc) == {"getX": "iam.x.read"}
    assert C.extract_permissions_from_openapi(doc, "x-other") == {}


def test_persist_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "_CATALOG_DIR", Path(tmp_path))
    cat = C.parse_permission_matrix(MATRIX)
    C.persist_permission_catalog("pid-1", cat)
    assert C.load_permission_catalog("pid-1")["role_count"] == 3
    assert C.load_permission_catalog("no-such") is None


def test_killswitch(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_CATALOG_DIR", Path(tmp_path))
    C.persist_permission_catalog("pid-2", C.parse_permission_matrix(MATRIX))
    monkeypatch.setenv("ARTA_AUTHZ_CATALOG_DISABLE", "1")
    assert C.load_permission_catalog("pid-2") is None
