"""authz_matrix_gen — oracle cells -> Newman collection (derived RBAC delivery)."""
from src.agents import authz_matrix_gen as G

GET_NEG = {"operationId": "listGroups", "method": "GET",
           "path": "/v1/organizations/{orgId}/iam/groups", "principal_id": "U20",
           "target_org": "testorg", "expected_status": 403, "tag": "TS-4",
           "permission": "iam.groups.read", "scope": "ORG"}
GET_POS = {**GET_NEG, "principal_id": "U21", "expected_status": 200, "tag": "TS-3"}
POST_OK = {"operationId": "createGroup", "method": "POST",
           "path": "/v1/organizations/{orgId}/iam/groups", "principal_id": "U21",
           "target_org": "testorg", "expected_status": 201, "tag": "TS-3"}
POST_403 = {**POST_OK, "principal_id": "U20", "expected_status": 403, "tag": "TS-4"}


def test_r154_skips_successful_mutations_emits_safe_cells():
    out = G.generate_newman_collection([GET_NEG, GET_POS, POST_OK, POST_403])
    s = out["stats"]
    assert s["emitted"] == 3                      # 2 GET + rejected POST
    assert s["skipped_successful_mutations"] == 1  # the 201 POST
    assert s["total_cells"] == 4


def test_successful_mutations_opt_in():
    out = G.generate_newman_collection([POST_OK], include_successful_mutations=True)
    assert out["stats"]["emitted"] == 1
    assert out["stats"]["skipped_successful_mutations"] == 0


def test_org_param_substituted_others_are_vars():
    cell = {**GET_NEG, "path": "/v1/regions/{region}/organizations/{orgId}/x"}
    item = G.cell_to_newman_item(cell, base_url_var="base_url",
                                 token_var_for=G._default_token_var,
                                 org_params=G._DEFAULT_ORG_PARAMS)
    raw = item["request"]["url"]["raw"]
    assert "testorg" in raw                    # {orgId} -> target org
    assert "{{region}}" in raw                  # other param -> env var
    assert raw.startswith("{{base_url}}")


def test_per_principal_bearer_and_assertion():
    item = G.cell_to_newman_item(GET_POS, base_url_var="base_url",
                                 token_var_for=G._default_token_var,
                                 org_params=G._DEFAULT_ORG_PARAMS)
    assert item["request"]["header"][0]["value"] == "Bearer {{U21_token}}"
    exec_ = "".join(item["event"][0]["script"]["exec"])
    assert "200" in exec_ and "TS-3" in exec_
    assert item["_arta_authz"]["principal_id"] == "U21"


def test_custom_token_var_and_org_params():
    out = G.generate_newman_collection(
        [GET_POS], token_var_for=lambda pid: f"tok_{pid}",
        org_param_names=["orgId"])
    hdr = out["collection"]["item"][0]["request"]["header"][0]["value"]
    assert hdr == "Bearer {{tok_U21}}"


def test_collection_shape_is_valid_postman():
    out = G.generate_newman_collection([GET_NEG])
    c = out["collection"]
    assert c["info"]["schema"].endswith("collection.json")
    assert isinstance(c["item"], list) and c["item"]


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_AUTHZ_MATRIX_GEN_DISABLE", "1")
    out = G.generate_newman_collection([GET_NEG])
    assert out["collection"] is None and out["stats"]["disabled"]
