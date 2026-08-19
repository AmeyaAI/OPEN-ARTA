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


# ── Source-Code-Component stamp (Code→API dimension per test) ──────────────────

def test_map_threads_source_file_from_captured():
    # A source-derived backend route persisted into the captured store carries
    # the handler file — the live Code→API data path.
    cap = [{"method": "GET", "path": "/api/collection/fieldset/definition",
            "file": "svc-repo:handlers/fieldset.go", "source": "github"}]
    res = tg.build_requirement_endpoint_map(
        cap, "generate fieldset definition for the collection")
    by_path = {e["path"]: e for e in res["endpoints"]}
    assert by_path["/api/collection/fieldset/definition"]["source_file"] == "svc-repo:handlers/fieldset.go"


def test_map_threads_source_file_from_source_endpoints():
    cap = [{"method": "GET", "path": "/api/collection/organizations"}]
    src = [{"method": "GET", "path": "/api/collection/fieldset/definition",
            "file": "svc-repo:handlers/fieldset.go"}]
    res = tg.build_requirement_endpoint_map(
        cap, "generate fieldset definition for the collection", source_endpoints=src)
    by_path = {e["path"]: e for e in res["endpoints"]}
    assert by_path["/api/collection/fieldset/definition"]["source_file"] == "svc-repo:handlers/fieldset.go"
    assert by_path.get("/api/collection/organizations", {}).get("source_file") is None


def test_source_component_stamp():
    mapped = [
        {"method": "GET", "path": "/v1/orgs/{id}/groups", "source_file": "iam:groups.go"},
        {"method": "GET", "path": "/v1/orgs", "source_file": None},  # no source file
    ]
    matched = ["GET:/v1/orgs/{id}/groups", "GET:/v1/orgs"]
    sc = tg.source_component_stamp(matched, mapped)
    assert sc["component_count"] == 1
    assert sc["components"][0] == {"key": "GET:/v1/orgs/{id}/groups", "file": "iam:groups.go"}
    assert tg.source_component_stamp([], mapped)["component_count"] == 0
    assert tg.source_component_stamp(matched, None)["component_count"] == 0
