"""R150.E — PW response-assertion field grounding (analogue to R111.G for Newman).

Pre-R150.E: PW spec content `expect(json.X).toEqual(...)` was never
checked against the SUT's actual response shapes. Live Iter 9 evidence:
23 × `waitForResponse` timeouts + ~41 × `locator.click` timeouts traced
to LLM-emitted assertions on invented field paths (e.g.,
`expect(json.insight.metric).toEqual('sales')` where the SUT's actual
response uses `data.records[0].metric_name`).

Post-R150.E: a new `_r150_e_validate_pw_response_assertions` helper +
new `expected_outputs` parameter on `validate_playwright_grounded` cross-
reference each PW assertion path against:
  (a) captured_endpoints[*].response_body_shape (via R150.G nested-path
      extractor; R150.A populates the underlying shape data).
  (b) recipe.expected_outputs keys (when caller provides them).

If neither source contains the path, emit
`GroundingViolation(kind="pw_assertion_field_unknown")` → R102.A stamp +
R102.C dispatch BLOCK chain (kind-agnostic).

Killswitch: `ARTA_R150_E_PW_ASSERT_VALIDATOR_DISABLE=1`.
"""
from __future__ import annotations

import os

from src.agents.grounding_validator import (
    _r150_e_collect_grounded_paths,
    _r150_e_validate_pw_response_assertions,
    validate_playwright_grounded,
)


# ─── _r150_e_collect_grounded_paths unit tests ──────────────────────────────


def _make_endpoint(method: str, path: str, shape: dict | None) -> dict:
    return {"method": method, "path": path, "response_body_shape": shape}


def test_r150_e_collect_captured_shapes_only():
    """Captured endpoints union into the grounded set; recipe absent."""
    captured = [
        _make_endpoint("GET", "/api/v1/insight", {
            "type": "object",
            "properties": {
                "data": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                    },
                },
            },
        }),
    ]
    grounded = _r150_e_collect_grounded_paths(captured, None)
    assert "data" in grounded
    assert "data.value" in grounded


def test_r150_e_collect_recipe_outputs_only():
    """Recipe expected_outputs keys flow through when captured absent."""
    expected = {"revenue": 1000, "cost": 200, "margin": 800}
    grounded = _r150_e_collect_grounded_paths(None, expected)
    assert grounded == {"revenue", "cost", "margin"}


def test_r150_e_collect_union_both_sources():
    """Both sources unioned — caller can rely on either."""
    captured = [
        _make_endpoint("GET", "/x", {
            "type": "object",
            "properties": {"alpha": {"type": "string"}},
        }),
    ]
    expected = {"beta": "value"}
    grounded = _r150_e_collect_grounded_paths(captured, expected)
    assert "alpha" in grounded
    assert "beta" in grounded


def test_r150_e_collect_empty_when_both_empty():
    """Cold-start contract: no captures, no recipe → empty set."""
    assert _r150_e_collect_grounded_paths(None, None) == set()
    assert _r150_e_collect_grounded_paths([], {}) == set()


# ─── _r150_e_validate_pw_response_assertions tests ──────────────────────────


def _captured_with_insight_metric() -> list[dict]:
    return [
        _make_endpoint("POST", "/api/v1/insight", {
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
        }),
    ]


def test_r150_e_grounded_assertion_passes():
    """KEYSTONE — `expect(json.insight.metric).toEqual(...)` where the SUT
    returns `insight.metric` → no violation."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  const json = await response.json();\n"
        "  expect(json.insight.metric).toEqual('sales');\n"
        "});\n"
    )
    violations = _r150_e_validate_pw_response_assertions(
        content,
        captured_endpoints=_captured_with_insight_metric(),
    )
    assert violations == [], (
        f"grounded path should not flag; got: {[v.symbol for v in violations]}"
    )


def test_r150_e_ungrounded_assertion_flagged():
    """KEYSTONE — `expect(json.insight.phantom).toEqual(...)` where the
    SUT only returns `insight.metric` + `insight.value` → flag."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  const json = await response.json();\n"
        "  expect(json.insight.phantom).toEqual('hallucinated');\n"
        "});\n"
    )
    violations = _r150_e_validate_pw_response_assertions(
        content,
        captured_endpoints=_captured_with_insight_metric(),
    )
    assert len(violations) == 1
    assert violations[0].kind == "pw_assertion_field_unknown"
    assert "insight.phantom" in violations[0].symbol
    assert violations[0].tool == "playwright"


def test_r150_e_intermediate_path_accepted():
    """`expect(json.insight).toEqual(...)` when `insight.metric` is
    grounded → no violation. The intermediate-path access is valid
    because the longer `insight.metric` path implies `insight` exists.
    Mirrors R150.G intermediate-prefix logic."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  const json = await response.json();\n"
        "  expect(json.insight).toMatchObject({metric: 'sales'});\n"
        "});\n"
    )
    violations = _r150_e_validate_pw_response_assertions(
        content,
        captured_endpoints=_captured_with_insight_metric(),
    )
    assert violations == [], (
        f"intermediate path should pass; got: {[v.symbol for v in violations]}"
    )


def test_r150_e_recipe_expected_outputs_grounds_path():
    """When recipe.expected_outputs declares a key, PW assertions on it
    pass — even when the captured shape doesn't include it (recipe takes
    precedence; the test will tell us if SUT really returns it)."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  expect(json.revenue).toBe(1000);\n"
        "});\n"
    )
    violations = _r150_e_validate_pw_response_assertions(
        content,
        captured_endpoints=None,
        expected_outputs={"revenue": 1000, "cost": 200},
    )
    assert violations == []


def test_r150_e_array_index_normalized():
    """`records[0].id` is normalized to `records.id` to match R150.G's
    array-items-in-place convention (no index notation in grounded paths)."""
    captured = [
        _make_endpoint("GET", "/api/v1/records", {
            "type": "object",
            "properties": {
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "value": {"type": "number"},
                        },
                    },
                },
            },
        }),
    ]
    content = (
        "test('foo', async ({ page }) => {\n"
        "  expect(json.records[0].id).toEqual('rec-001');\n"
        "});\n"
    )
    violations = _r150_e_validate_pw_response_assertions(
        content, captured_endpoints=captured,
    )
    assert violations == [], (
        f"array-index normalization should accept; got: "
        f"{[v.symbol for v in violations]}"
    )


def test_r150_e_killswitch_disables_validator():
    """`ARTA_R150_E_PW_ASSERT_VALIDATOR_DISABLE=1` → empty list."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  expect(json.insight.phantom).toEqual('x');\n"
        "});\n"
    )
    os.environ["ARTA_R150_E_PW_ASSERT_VALIDATOR_DISABLE"] = "1"
    try:
        violations = _r150_e_validate_pw_response_assertions(
            content, captured_endpoints=_captured_with_insight_metric(),
        )
    finally:
        del os.environ["ARTA_R150_E_PW_ASSERT_VALIDATOR_DISABLE"]
    assert violations == [], "killswitch must suppress all violations"


def test_r150_e_cold_start_safe_empty_grounded_skips():
    """When BOTH captured + expected_outputs are empty, validator returns
    [] (cold-start contract — never blocks a project that hasn't
    completed discovery refresh)."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  expect(json.anything).toEqual('x');\n"
        "});\n"
    )
    violations = _r150_e_validate_pw_response_assertions(
        content, captured_endpoints=None,
    )
    assert violations == []


# ─── End-to-end via validate_playwright_grounded ────────────────────────────


def test_r150_e_end_to_end_via_public_entry():
    """Verify R150.E hooks into `validate_playwright_grounded` correctly:
    flagged paths surface via the standard public API. Regression guard
    for the additive wire-up."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  const json = await response.json();\n"
        "  expect(json.insight.phantom_field).toEqual('x');\n"
        "});\n"
    )
    violations = validate_playwright_grounded(
        content,
        project_id="test-project",
        captured_endpoints=_captured_with_insight_metric(),
    )
    # At least one violation surfaces with the R150.E kind
    kinds = [v.kind for v in violations]
    assert "pw_assertion_field_unknown" in kinds, (
        f"R150.E must hook into validate_playwright_grounded; got kinds: {kinds}"
    )


def test_r150_e_expected_outputs_parameter_accepted_by_public_api():
    """Caller can pass `expected_outputs=...` to the public validator —
    regression guard for the signature extension."""
    content = (
        "test('foo', async ({ page }) => {\n"
        "  expect(json.revenue).toBe(1000);\n"
        "});\n"
    )
    violations = validate_playwright_grounded(
        content,
        project_id="test-project",
        captured_endpoints=None,
        expected_outputs={"revenue": 1000},
    )
    # No pw_assertion_field_unknown violations because revenue is in expected_outputs
    kinds = [v.kind for v in violations]
    assert "pw_assertion_field_unknown" not in kinds
