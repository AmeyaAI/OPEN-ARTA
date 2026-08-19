"""P3 — GET /api/discovery/projects/{id}/authz-model. Direct-call unit test
(reads config-layer files; no DB/HTTP)."""
import asyncio
from pathlib import Path

import src.agents.authz_discovery as A
from src.api.routers.discovery import authz_model

SPEC = {"paths": {
    "/v1/orgs/{id}/groups": {"get": {"operationId": "listGroups",
        "responses": {"200": {}, "401": {}, "403": {}}}},
    "/v1/orgs": {"get": {"operationId": "listOrganizations",
        "responses": {"200": {}, "401": {}}}},   # exempt
}}


def test_fail_open_no_model(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    out = asyncio.run(authz_model("no-such"))
    assert out["built"] is False


def test_built_summary_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    m = A.extract_authz_model(SPEC); m["project_id"] = "pid"
    A.persist_authz_model("pid", m)
    out = asyncio.run(authz_model("pid"))
    assert out["built"] is True
    assert out["operation_count"] == 2
    assert out["summary"]["authz_gated"] == 1
    assert out["summary"]["exempt_auth_only"] == 1
    # principals/catalog not seeded -> graceful zeros + default mechanism
    assert out["role_count"] == 0 and out["principal_count"] == 0
    assert out["mechanism"] == "rbac_scoped_catalog"


def test_killswitch(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_AUTHZ_DIR", Path(tmp_path))
    monkeypatch.setenv("ARTA_AUTHZ_MODEL_ENDPOINT_DISABLE", "1")
    assert asyncio.run(authz_model("pid"))["disabled"] is True
