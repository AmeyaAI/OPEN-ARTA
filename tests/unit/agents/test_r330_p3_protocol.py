"""R330 P3 — multi-protocol awareness wiring."""
import json

from src.agents.protocol_discovery import classify_protocol
from src.agents.architecture_discovery import _classify_protocol
from src.agents.api_discovery import _detect_response_type_from_code


def test_classifier_moved_and_aliased():
    # single source of truth: the old private name is the same function
    assert _classify_protocol is classify_protocol
    assert classify_protocol("/api/users") == "rest"
    assert classify_protocol("/query-engine/event/response-stream") == "sse"
    assert classify_protocol("/graphql") == "graphql"
    assert classify_protocol("/ws/notifications") == "websocket"
    # boundary guards still hold (asset must not match sse)
    assert classify_protocol("/api/assets") == "rest"


def test_store_merge_preserves_protocol(tmp_path, monkeypatch):
    from src.agents import api_discovery as ad
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    (tmp_path / "p1.json").write_text(json.dumps(
        [{"method": "GET", "path": "/stream/events", "source": "network"}]))
    ad.save_captured_endpoints("p1", [
        {"method": "GET", "path": "/stream/events", "source": "network",
         "protocol": "sse", "status": 200}])
    saved = json.loads((tmp_path / "p1.json").read_text())
    assert saved[0]["protocol"] == "sse"


def test_response_type_scoped_to_endpoint_window():
    # StreamingResponse near an UNRELATED endpoint must not mark THIS one sse
    ctx = (
        "def stream_events():\n    return StreamingResponse(gen())\n"
        + "\n" * 100
        + "def list_users():\n    return JSONResponse(users)\n"
    )
    assert _detect_response_type_from_code(ctx, "svc", path="/api/users") == "json"
    assert _detect_response_type_from_code(ctx, "svc", path="/api/stream/events") == "sse"
    # no path → legacy service-level heuristic preserved
    assert _detect_response_type_from_code(ctx, "svc") == "sse"
