"""R211 Phase C — shared test-data / body synthesis (reuses _example_for_schema)."""
from __future__ import annotations

from src.agents.test_data import synthesize_body, build_request_bodies


def test_synthesize_required_only_body():
    schema = {"type": "object", "required": ["name", "datasource_id"],
              "properties": {"name": {"type": "string"},
                             "datasource_id": {"type": "string"},
                             "optional": {"type": "string"}}}
    body = synthesize_body(schema)
    assert set(body.keys()) == {"name", "datasource_id"}   # required-only (R115.A.1)
    assert "optional" not in body


def test_synthesize_fills_session_ids():
    schema = {"type": "object", "required": ["account_id", "name"],
              "properties": {"account_id": {"type": "string"},
                             "name": {"type": "string"}}}
    body = synthesize_body(schema, known_ids={"account_id": "REAL-ACC-1"})
    assert body["account_id"] == "REAL-ACC-1"   # session id filled, not "example"
    assert body["name"] == "example"


def test_synthesize_none_without_schema():
    assert synthesize_body(None) is None
    assert synthesize_body({}) is None


def test_build_request_bodies_from_openapi():
    spec = {"paths": {
        "/api/v1/datasets": {
            "post": {"requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "required": ["name"],
                "properties": {"name": {"type": "string"}}}}}}},
            "get": {},  # no body
        }}}
    bodies = build_request_bodies(openapi_spec=spec)
    assert "POST /api/v1/datasets" in bodies
    assert bodies["POST /api/v1/datasets"] == {"name": "example"}
    assert "GET /api/v1/datasets" not in bodies


def test_build_request_bodies_empty_when_no_schema_source():
    # the measured SUT reality: no requestBody, no request_body_shape → {}
    bodies = build_request_bodies(
        openapi_spec={"paths": {"/x": {"post": {}}}},
        captured_endpoints=[{"method": "POST", "path": "/x"}])
    assert bodies == {}


def test_phase_f_captured_request_bodies_synthesize():
    # Phase F — the probe-captured REAL would-be body becomes the synthesis
    # source (with session-id fields corrected to the live ids).
    crb = [{"method": "POST", "url": "https://sut/api/datasets?x=1",
            "postData": '{"name": "ds1", "account_id": "OLD", "rows": 3}'}]
    bodies = build_request_bodies(
        captured_request_bodies=crb, known_ids={"account_id": "REAL-ACC"})
    assert "POST /api/datasets" in bodies
    b = bodies["POST /api/datasets"]
    assert b["name"] == "ds1" and b["rows"] == 3
    assert b["account_id"] == "REAL-ACC"   # session id corrected


def test_load_captured_request_bodies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.agents.test_data import load_captured_request_bodies
    assert load_captured_request_bodies("pid") == []   # no file → []
    d = tmp_path / ".arta" / "discovered_request_bodies"
    d.mkdir(parents=True)
    (d / "pid.json").write_text(
        '[{"method":"POST","url":"/api/x","postData":"{}"}]')
    rows = load_captured_request_bodies("pid")
    assert len(rows) == 1 and rows[0]["method"] == "POST"
