"""P1 — authz understanding feeds generation: the grounding block + the
per-item template matcher. Deterministic, no LLM."""
from pathlib import Path

from src.agents import authz_discovery as A
from src.agents.automation_engineer import AutomationEngineerAgent as AE

SPEC = {"paths": {
    "/v1/regions/global/organizations/{orgId}/iam/groups": {
        "get": {"operationId": "listGroups", "x-visibility": ["public", "internal"],
                "responses": {"200": {}, "401": {}, "403": {}}},
        "post": {"operationId": "createGroup",
                 "responses": {"201": {}, "401": {}, "403": {}}}},
    "/v1/regions/global/organizations": {
        "get": {"operationId": "listOrganizations",       # exempt (401 only)
                "responses": {"200": {}, "401": {}}}},
}}


def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    m = A.extract_authz_model(SPEC)
    m["project_id"] = "pid"
    A.persist_authz_model("pid", m)


def test_summary_fail_open_no_model(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    assert A.summarize_authz_for_prompt("no-such") == ""


def test_summary_names_gated_and_exempt(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    block = A.summarize_authz_for_prompt("pid")
    assert "SUT AUTHORIZATION MODEL" in block
    assert "listGroups".lower() in block.lower() or "iam/groups" in block
    assert "exempt" in block.lower()          # exempt set called out
    # the exempt op must NOT be listed as a gated privilege line
    assert "GET /v1/regions/global/organizations [" not in block


def test_relevant_paths_ranks_relevant_first_not_truncated(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    # tiny budget: only the relevant op should survive truncation
    block = A.summarize_authz_for_prompt(
        "pid", max_chars=1,
        relevant_paths={"/v1/regions/global/organizations/{orgId}/iam/groups"})
    assert "iam/groups" in block               # relevant op present despite budget


def test_grounding_block_killswitch(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("ARTA_AUTHZ_GROUNDING_DISABLE", "1")
    assert AE._authz_grounding_block("pid", "list groups", False) == ""


def test_grounding_block_returns_block(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.delenv("ARTA_AUTHZ_GROUNDING_DISABLE", raising=False)
    block = AE._authz_grounding_block("pid", "", False)
    assert "SUT AUTHORIZATION MODEL" in block


def test_authz_op_for_template_match():
    ops = A.extract_authz_model(SPEC)["operations"]
    # concrete org id matches the {orgId} template segment
    hit = AE._authz_op_for("GET", "/v1/regions/global/organizations/vendor/iam/groups", ops)
    assert hit and hit["operationId"] == "listGroups"
    assert hit["success_status"] == 200 and hit["auth_gated"] is True
    # method mismatch -> no hit
    assert AE._authz_op_for("DELETE", "/v1/regions/global/organizations/x/iam/groups", ops) is None
    # segment-count mismatch -> no hit
    assert AE._authz_op_for("GET", "/v1/regions/global/organizations", ops) is not None  # exempt op
    assert AE._authz_op_for("GET", "/v1/a/b/c/d/e/f/g", ops) is None
