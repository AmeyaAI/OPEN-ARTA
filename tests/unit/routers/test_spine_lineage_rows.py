"""Run-report Trace-panel per-test spine rows — the charter chain
Req→AC→WORKFLOW→CODE→API→DATA→TC→Script. Each stamp renders one row, defensively
(nothing when absent)."""
from src.api.routers.execution import _spine_lineage_rows


def _joined(meta):
    return "\n".join(_spine_lineage_rows(meta))


def test_all_three_spine_rows_render_from_stamps():
    meta = {
        "workflows": {"workflow_count": 2, "workflows": [
            {"chain_id": "c1", "endpoint_count": 3, "matched_count": 2}]},
        "source_components": {"component_count": 1, "components": [
            {"key": "GET:/x", "file": "svc/Handler.java"}]},
        "data_objects": {"object_count": 1, "entities": ["OrderDto"]},
    }
    html = _joined(meta)
    assert "Workflow" in html and "2 workflow(s)" in html and "2/3 endpoints" in html
    assert "Code" in html and "svc/Handler.java" in html
    assert "Data" in html and "OrderDto" in html
    assert len(_spine_lineage_rows(meta)) == 3


def test_rows_absent_when_stamp_missing_or_empty():
    assert _spine_lineage_rows({}) == []
    assert _spine_lineage_rows(None) == []
    # present but empty counts → no row
    assert _spine_lineage_rows({"workflows": {"workflow_count": 0},
                                "data_objects": {"object_count": 0}}) == []
    # only the present stamp renders
    rows = _spine_lineage_rows({"data_objects": {"object_count": 1, "entities": ["E"]}})
    assert len(rows) == 1 and "Data" in rows[0]


def test_html_is_escaped():
    rows = _spine_lineage_rows({"data_objects": {"object_count": 1, "entities": ["<script>"]}})
    assert "<script>" not in rows[0] and "&lt;script&gt;" in rows[0]
