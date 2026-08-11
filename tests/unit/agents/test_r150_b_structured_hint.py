"""R150.B — structured sample-payload block in R130.H recipe-gen hint.

Pre-R150.B: R130.H surfaced "top-20 keys" as a flat comma-separated list.
The LLM still invented prefixes like `insight_*` because it couldn't see
WHICH endpoint returns WHICH structure — it just saw an undifferentiated
bag of key names.

Post-R150.B: per-endpoint structured sample shows the actual response
shape with concrete placeholder types so the LLM grounds recipe.columns
against real SUT keys. R150.B is ADDITIVE — the existing R130.H base
+ R131.B + R132.B constraints still apply unchanged.

Killswitch: `ARTA_R150_B_STRUCTURED_HINT_DISABLE=1` reverts to R130.H
base behavior (top-20 keys only).
"""
from __future__ import annotations

import os

from src.agents.dataset_recipe import DatasetRecipeAgent


# ─── _r150_b_render_shape unit tests (pure function on dataclass-shaped dict)


def test_r150_b_render_flat_object():
    shape = {
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "active": {"type": "boolean"},
        },
    }
    out = DatasetRecipeAgent._r150_b_render_shape(shape)
    assert '"metric": "<string>"' in out
    assert '"value": <number>' in out
    assert '"active": <boolean>' in out


def test_r150_b_render_nested_object():
    shape = {
        "type": "object",
        "properties": {
            "insight": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "value": {"type": "number"},
                },
            },
        },
    }
    out = DatasetRecipeAgent._r150_b_render_shape(shape)
    # Both nesting layers visible
    assert '"insight":' in out
    assert '"metric": "<string>"' in out


def test_r150_b_render_array_items_descend():
    shape = {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "value": {"type": "integer"},
                    },
                },
            },
        },
    }
    out = DatasetRecipeAgent._r150_b_render_shape(shape)
    assert '"records":' in out
    assert "[{" in out or "[\n" in out
    assert '"id": "<string>"' in out


def test_r150_b_render_max_depth_caps_recursion():
    """Beyond max_depth, render returns empty (prevents pathological depth)."""
    deep = {"type": "object", "properties": {}}
    inner = deep
    for _ in range(8):
        new_inner = {"type": "object", "properties": {}}
        inner["properties"]["child"] = new_inner
        inner = new_inner
    out = DatasetRecipeAgent._r150_b_render_shape(deep, indent=0, max_depth=3)
    # Output is bounded — no stack-overflow / no infinite chain
    assert len(out) < 600


def test_r150_b_render_unknown_type_safe():
    assert DatasetRecipeAgent._r150_b_render_shape({"type": "weird"}) == "<any>"
    assert DatasetRecipeAgent._r150_b_render_shape({}) == "<any>"
    assert DatasetRecipeAgent._r150_b_render_shape(None) == ""  # type: ignore[arg-type]


# ─── _r150_b_compose_sample_payloads tests ──────────────────────────────────


def _make_agent() -> DatasetRecipeAgent:
    """DatasetRecipeAgent init requires LLM client; we bypass by
    constructing without going through __init__'s client setup since the
    R150.B helpers we're testing don't touch self._client."""
    return DatasetRecipeAgent.__new__(DatasetRecipeAgent)


def test_r150_b_compose_empty_captured_returns_empty():
    agent = _make_agent()
    assert agent._r150_b_compose_sample_payloads([]) == ""


def test_r150_b_compose_skips_endpoints_with_no_shape():
    """Endpoints with empty/None response_body_shape are skipped — the
    block surfaces only informative shapes."""
    agent = _make_agent()
    captured = [
        {"method": "GET", "path": "/a", "response_body_shape": None},
        {"method": "GET", "path": "/b", "response_body_shape": {}},
        {"method": "GET", "path": "/c"},
    ]
    assert agent._r150_b_compose_sample_payloads(captured) == ""


def test_r150_b_compose_surfaces_endpoints_with_real_shapes():
    """KEYSTONE — endpoints with real shapes get rendered with structured
    sample payloads + HARD CONSTRAINT line."""
    agent = _make_agent()
    captured = [
        {
            "method": "POST",
            "path": "/api/v1/insight/request",
            "response_body_shape": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "properties": {
                            "records": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "metric_name": {"type": "string"},
                                        "value": {"type": "number"},
                                    },
                                },
                            },
                        },
                    },
                    "meta": {
                        "type": "object",
                        "properties": {"total_count": {"type": "integer"}},
                    },
                },
            },
        },
    ]
    out = agent._r150_b_compose_sample_payloads(captured)
    assert out != ""
    assert "[R150.B" in out
    assert "POST /api/v1/insight/request" in out
    assert "metric_name" in out
    assert "HARD CONSTRAINT" in out
    assert "R131.B" in out  # snake_case-flatten reference


def test_r150_b_compose_prefers_deeper_shapes():
    """Endpoints with more keys (deeper structure) rank higher — the
    LLM gets the most-informative samples first."""
    agent = _make_agent()
    captured = [
        {
            "method": "GET",
            "path": "/shallow",
            "response_body_shape": {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "number"}},
            },
        },
        {
            "method": "GET",
            "path": "/deep",
            "response_body_shape": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "string"},
                            "y": {"type": "number"},
                            "z": {"type": "boolean"},
                            "nested": {
                                "type": "object",
                                "properties": {
                                    "q": {"type": "string"},
                                    "r": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    ]
    out = agent._r150_b_compose_sample_payloads(captured, max_endpoints=2)
    # /deep should appear EARLIER than /shallow (deeper structure ranks first)
    assert out.index("/deep") < out.index("/shallow")


def test_r150_b_compose_char_budget_enforced():
    """Char budget caps total output — extra endpoints get omitted with
    a marker so the LLM hint stays bounded."""
    agent = _make_agent()
    # Build many endpoints; budget will exclude later ones
    captured = []
    for i in range(20):
        captured.append({
            "method": "GET",
            "path": f"/api/endpoint_{i}",
            "response_body_shape": {
                "type": "object",
                "properties": {
                    f"field_{j}": {"type": "string"} for j in range(8)
                },
            },
        })
    out = agent._r150_b_compose_sample_payloads(captured, max_chars=600)
    # Budget enforced (with some slack for the omitted-marker line)
    assert len(out) <= 1200
    # And some indication that endpoints were omitted (either explicit
    # marker OR fewer endpoints than the full list)
    visible_endpoints = sum(out.count(f"/api/endpoint_{i}") for i in range(20))
    assert visible_endpoints < 20


def test_r150_b_killswitch_returns_empty():
    """ARTA_R150_B_STRUCTURED_HINT_DISABLE=1 reverts to R130.H base."""
    agent = _make_agent()
    captured = [
        {
            "method": "GET",
            "path": "/api/v1/foo",
            "response_body_shape": {
                "type": "object",
                "properties": {
                    "a": {"type": "string"},
                    "b": {"type": "number"},
                },
            },
        },
    ]
    os.environ["ARTA_R150_B_STRUCTURED_HINT_DISABLE"] = "1"
    try:
        assert agent._r150_b_compose_sample_payloads(captured) == ""
    finally:
        del os.environ["ARTA_R150_B_STRUCTURED_HINT_DISABLE"]
