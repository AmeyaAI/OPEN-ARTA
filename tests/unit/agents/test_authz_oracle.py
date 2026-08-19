"""authz_oracle — deterministic RBAC verdicts (the derived-RBAC keystone)."""
from src.agents import authz_oracle as O

CATALOG = {"role_permissions": {
    "iam-admin": ["iam.groups.read", "iam.groups.create"],
    "organizations-viewer": ["org.members.read"],
    "platform-admin": ["platform.admin.org.create"],
}}
PROFILE = {"special_orgs": ["vendor"], "platform_tier": "PLATFORM"}

OP_LISTGROUPS = {"operationId": "listGroups", "scope": "ORG",
                 "permission": "iam.groups.read", "auth_gated": True, "success_status": 200}
OP_CREATEGROUP = {"operationId": "createGroup", "scope": "ORG",
                  "permission": "iam.groups.create", "auth_gated": True, "success_status": 201}
OP_EXEMPT = {"operationId": "listOrganizations", "scope": "GLOBAL",
             "auth_gated": False, "success_status": 200}
OP_PLATFORM = {"operationId": "createOrganization", "scope": "GLOBAL",
               "permission": "platform.admin.org.create", "auth_gated": True, "success_status": 202}

U3 = {"id": "U3", "principal_type": "operator", "home_org": "vendor",
      "bindings": [{"role": "iam-admin", "scope": "platform", "target": ""}]}
U21 = {"id": "U21", "principal_type": "customer", "home_org": "testcustomer",
       "bindings": [{"role": "iam-admin", "scope": "org", "target": "testcustomer"}]}
U20 = {"id": "U20", "principal_type": "customer", "home_org": "testcustomer",
       "bindings": [{"role": "organizations-viewer", "scope": "org", "target": "testcustomer"}]}
U1 = {"id": "U1", "principal_type": "operator", "home_org": "vendor",
      "bindings": [{"role": "platform-admin", "scope": "platform", "target": ""}]}


def _v(op, p, org):
    return O.expected_status(op, p, org, CATALOG, PROFILE)


def test_ts6_special_org_carveout():
    # operator iam-admin @ platform, evaluated inside its OWN special org -> 403.
    r = _v(OP_LISTGROUPS, U3, "vendor")
    assert r["status"] == 403 and r["tag"] == "TS-6"


def test_platform_grant_crosses_into_non_special_org():
    r = _v(OP_LISTGROUPS, U3, "customerA")
    assert r["status"] == 200 and r["tag"] == "TS-3"


def test_ts3_positive_org_grant():
    r = _v(OP_LISTGROUPS, U21, "testcustomer")
    assert r["status"] == 200 and r["tag"] == "TS-3"
    assert _v(OP_CREATEGROUP, U21, "testcustomer")["status"] == 201   # declared 2xx


def test_ts7_cross_tenant():
    r = _v(OP_LISTGROUPS, U21, "otherorg")     # holds it, but wrong org
    assert r["status"] == 403 and r["tag"] == "TS-7"


def test_ts4_negative_lacks_permission():
    r = _v(OP_LISTGROUPS, U20, "testcustomer")  # org-viewer lacks iam.groups.read
    assert r["status"] == 403 and r["tag"] == "TS-4"


def test_exempt_is_success_for_everyone():
    assert _v(OP_EXEMPT, U20, "testcustomer")["status"] == 200
    assert _v(OP_EXEMPT, U3, "vendor")["tag"] == "TS-3"


def test_platform_op_needs_platform_grant():
    assert _v(OP_PLATFORM, U1, "")["status"] == 202          # platform-admin holds it
    assert _v(OP_PLATFORM, U20, "testcustomer")["status"] == 403  # customer lacks it


def test_evaluate_matrix_shape_and_target_defaulting():
    model = {"operations": [OP_LISTGROUPS, OP_EXEMPT],
             "role_permissions": CATALOG["role_permissions"]}
    # X-case principal with a cross-tenant target override
    x1 = {**U21, "id": "X1", "test_target_org": "bigcustomer"}
    cells = O.evaluate_matrix(model, [U21, x1], profile=PROFILE)
    assert len(cells) == 4                                   # 2 ops x 2 principals
    by = {(c["operationId"], c["principal_id"]): c for c in cells}
    assert by[("listGroups", "U21")]["target_org"] == "testcustomer"  # home org
    assert by[("listGroups", "X1")]["target_org"] == "bigcustomer"    # override
    assert by[("listGroups", "X1")]["tag"] == "TS-7"                  # cross-tenant
    assert by[("listOrganizations", "U21")]["expected_status"] == 200  # exempt


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_AUTHZ_ORACLE_DISABLE", "1")
    assert O.evaluate_matrix({"operations": [OP_LISTGROUPS]}, [U21], profile=PROFILE) == []


# ── pluggable role MECHANISMS (each SUT differs) ──

def test_oauth_scope_mechanism():
    prof = {"authz_mechanism": "oauth_scope"}
    op = {"operationId": "readThing", "auth_gated": True, "success_status": 200,
          "required_scope": "things:read"}
    assert O.expected_status(op, {"scopes": ["things:read"]}, "", {}, prof)["status"] == 200
    assert O.expected_status(op, {"scopes": ["x"]}, "", {}, prof)["tag"] == "TS-4"
    # exempt still short-circuits regardless of mechanism
    ex = {"operationId": "pub", "auth_gated": False, "success_status": 200}
    assert O.expected_status(ex, {"scopes": []}, "", {}, prof)["status"] == 200


def test_simple_rbac_mechanism():
    prof = {"authz_mechanism": "simple_rbac"}
    op = {"operationId": "adminOnly", "auth_gated": True, "success_status": 201,
          "allowed_roles": ["admin", "owner"]}
    assert O.expected_status(op, {"roles": ["owner"]}, "", {}, prof)["status"] == 201
    assert O.expected_status(op, {"roles": ["viewer"]}, "", {}, prof)["tag"] == "TS-4"


def test_unknown_mechanism_falls_back_to_default():
    # never a silent wrong verdict — falls back to rbac_scoped_catalog
    r = O.expected_status(OP_LISTGROUPS, U21, "testcustomer", CATALOG,
                          {"authz_mechanism": "no-such"})
    assert r["tag"] == "TS-3"


def test_register_custom_mechanism():
    O.register_authz_model("always_403",
                           lambda op, p, org, ctx: {"status": 403, "tag": "TS-4", "reason": "x"})
    r = O.expected_status(OP_LISTGROUPS, U21, "testcustomer", CATALOG,
                          {"authz_mechanism": "always_403"})
    assert r["status"] == 403


def test_default_mechanism_is_rbac_scoped_catalog():
    # no authz_mechanism in profile => the catalog-RBAC model (carve-out).
    r = O.expected_status(OP_LISTGROUPS, U3, "vendor", CATALOG, {"special_orgs": ["vendor"]})
    assert r["tag"] == "TS-6"
