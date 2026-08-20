"""Business-Workflow per-test traceability — which captured CallChain(s) a test
exercises. Completes the Req→AC→WORKFLOW→Code→API→Data→Test chain. Deterministic,
no LLM; O(matched_keys)/test via a prebuilt inverted index."""
import src.agents.traceability_gate as tg

_CHAINS = [
    {"chain_id": "ch-auth-create", "nodes": [
        {"method": "POST", "path_template": "/v1/auth/login"},
        {"method": "GET", "path_template": "/v1/users/me"},
        {"method": "POST", "path_template": "/v1/orgs"}]},
    {"chain_id": "ch-read", "nodes": [
        {"method": "GET", "path_template": "/v1/users/me"},
        {"method": "GET", "path_template": "/v1/orgs"}]},
    {"chain_id": "ch-unrelated", "nodes": [
        {"method": "GET", "path_template": "/v1/billing/invoices"}]},
]


def test_index_and_stamp_link_the_right_chains():
    idx = tg.build_chain_index(_CHAINS)
    # a test exercising login + users/me matches both chains that contain them
    out = tg.workflow_stamp(["POST:/v1/auth/login", "GET:/v1/users/me"], idx)
    by = {w["chain_id"]: w for w in out["workflows"]}
    assert out["workflow_count"] == 2                     # auth-create + read
    assert by["ch-auth-create"]["matched_count"] == 2     # both keys in this chain
    assert by["ch-auth-create"]["endpoint_count"] == 3
    assert by["ch-read"]["matched_count"] == 1            # only users/me
    # sorted by matched_count desc → the fuller chain first
    assert out["workflows"][0]["chain_id"] == "ch-auth-create"
    assert "ch-unrelated" not in by                       # no endpoint overlap


def test_fail_open_and_cap():
    assert tg.workflow_stamp([], tg.build_chain_index(_CHAINS))["workflow_count"] == 0
    assert tg.workflow_stamp(["GET:/x"], None)["workflow_count"] == 0
    assert tg.build_chain_index(None) == {}
    # cap at 5
    many = [{"chain_id": f"c{i}", "nodes": [{"method": "GET", "path_template": "/v1/users/me"}]}
            for i in range(9)]
    out = tg.workflow_stamp(["GET:/v1/users/me"], tg.build_chain_index(many))
    assert out["workflow_count"] == 9 and len(out["workflows"]) == 5


def test_read_traceability_aggregates_workflow_linked(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "_TRACE_DIR", tmp_path)
    tg.persist_traceability("p", "TC-1", "REQ-1",
                            {"traceable": True, "reason": "matched",
                             "workflows": {"workflow_count": 2}})
    tg.persist_traceability("p", "TC-2", "REQ-1",
                            {"traceable": True, "reason": "matched"})   # no workflows
    agg = tg.read_traceability("p")
    assert agg["workflow_linked_count"] == 1
