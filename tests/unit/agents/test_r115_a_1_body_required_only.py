"""R115.A.1 — body-required-only contract gen.

Pre-R115.A.1: `_example_for_schema` fallback emitted `list(props.keys())[:3]`
when `required: []` was empty → 3 optional fields treated as required →
SUT 400 "Missing required field X" cascade (102 × 400 in run-8da91d).

R115.A.1: emit ONLY fields explicitly listed in `required:`. Schema-empty
required means body is `{}`.
"""
from __future__ import annotations

from src.agents.contract_test_generator import _example_for_schema


def test_r115_a_1_required_array_present_emits_those_fields():
    """Schema with `required: [foo, bar]` → body has foo + bar."""
    schema = {
        "type": "object",
        "properties": {
            "foo": {"type": "string"},
            "bar": {"type": "integer"},
            "baz": {"type": "boolean"},  # not in required
        },
        "required": ["foo", "bar"],
    }
    body = _example_for_schema(schema)
    assert isinstance(body, dict)
    assert "foo" in body
    assert "bar" in body
    assert "baz" not in body, (
        f"R115.A.1: optional 'baz' must NOT be emitted; got: {body}"
    )


def test_r115_a_1_empty_required_emits_empty_body():
    """Schema with NO `required:` declaration → body is `{}`."""
    schema = {
        "type": "object",
        "properties": {
            "foo": {"type": "string"},
            "bar": {"type": "integer"},
            "baz": {"type": "boolean"},
        },
        # No 'required' key
    }
    body = _example_for_schema(schema)
    assert body == {}, (
        f"R115.A.1: empty required must produce empty body; got: {body}"
    )


def test_r115_a_1_explicit_empty_required_emits_empty_body():
    """Schema with `required: []` (explicit empty) → body is `{}`."""
    schema = {
        "type": "object",
        "properties": {
            "foo": {"type": "string"},
            "bar": {"type": "integer"},
        },
        "required": [],
    }
    body = _example_for_schema(schema)
    assert body == {}, (
        f"R115.A.1: explicit-empty required must produce empty body; got: {body}"
    )


def test_r115_a_1_unknown_required_field_skipped():
    """If `required:` lists a field NOT in `properties:`, skip it gracefully."""
    schema = {
        "type": "object",
        "properties": {
            "foo": {"type": "string"},
        },
        "required": ["foo", "phantom_field"],
    }
    body = _example_for_schema(schema)
    assert "foo" in body
    assert "phantom_field" not in body, (
        f"unknown required field must be skipped; got: {body}"
    )
