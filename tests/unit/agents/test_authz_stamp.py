"""P2 — traceability authz stamp (authorization dimension per test)."""
import src.agents.traceability_gate as tg


def test_authz_stamp_gated_and_exempt():
    model = {"operations": [
        {"method": "GET", "path": "/v1/orgs/{id}/groups", "auth_gated": True,
         "permission_guess": "iam.groups.read", "scope": "ORG",
         "visibility": "common", "success_status": 200},
        {"method": "GET", "path": "/v1/orgs", "auth_gated": False,  # exempt
         "permission_guess": None, "scope": "GLOBAL", "success_status": 200},
    ]}
    matched = ["GET:/v1/orgs/{id}/groups", "GET:/v1/orgs"]
    stamp = tg.authz_stamp(matched, model)
    assert stamp["gated_count"] == 1                       # only the gated one
    g = stamp["gated_endpoints"][0]
    assert g["key"] == "GET:/v1/orgs/{id}/groups"
    assert g["permission"] == "iam.groups.read" and g["scope"] == "ORG"
    assert g["expected_status"] == 200


def test_authz_stamp_fail_open():
    assert tg.authz_stamp(["GET:/x"], None)["gated_count"] == 0
    assert tg.authz_stamp(None, {"operations": []})["gated_count"] == 0
    assert tg.authz_stamp([], {"operations": [{"method": "GET", "path": "/x",
                                               "auth_gated": True}]})["gated_count"] == 0
