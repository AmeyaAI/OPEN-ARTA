"""R124.N — Newman pm.test API misuse validator.

Live evidence (run-d52a8c): 33 Newman FAILs from LLM-emitted
`pm.assertEqual(...)`, `pm.assertNotNull(...)`, etc. — APIs that
don't exist in Postman's pm runtime. R124.N flags these at gen time;
R57.1 retry-with-hint shows the LLM the AFTER fix.
"""
from __future__ import annotations

from src.agents.grounding_validator import validate_newman_pm_api_usage


def test_r124_n_pm_assert_equal_flagged():
    """pm.assertEqual( is flagged as bad_pm_api."""
    script = """
pm.test('foo', function () {
    pm.assertEqual(pm.response.code, 200, 'status check');
});
"""
    out = validate_newman_pm_api_usage(script)
    bad = [v for v in out if v.kind == "bad_pm_api" and "pm.assertEqual" in v.symbol]
    assert len(bad) == 1, f"pm.assertEqual must flag; got {[v.symbol for v in out]}"
    assert bad[0].tool == "newman"


def test_r124_n_pm_test_pm_expect_not_flagged():
    """Valid Postman APIs (pm.test + pm.expect) must NOT be flagged."""
    script = """
pm.test('status check', function () {
    pm.expect(pm.response.code).to.equal(200);
    pm.response.to.have.status(200);
});
"""
    out = validate_newman_pm_api_usage(script)
    bad = [v for v in out if v.kind == "bad_pm_api"]
    assert bad == [], f"valid pm APIs must not flag; got {bad}"


def test_r124_n_hint_contains_before_after():
    """Violation hint follows R110.B BEFORE/AFTER idiom."""
    script = "pm.assertNotNull(pm.response.json().field);"
    out = validate_newman_pm_api_usage(script)
    assert len(out) == 1
    hint = out[0].hint
    assert "BEFORE" in hint and "AFTER" in hint, f"hint missing markers: {hint[:200]}"
    assert "pm.expect" in hint, "hint must point to the correct alternative"


def test_r124_n_idempotent_multiple_same_api():
    """Multiple uses of same invalid API → only one violation (dedup)."""
    script = """
pm.assertEqual(a, 1);
pm.assertEqual(b, 2);
pm.assertEqual(c, 3);
"""
    out = validate_newman_pm_api_usage(script)
    eq_violations = [v for v in out if "pm.assertEqual" in v.symbol]
    assert len(eq_violations) == 1, (
        f"multiple uses of same API should produce ONE violation; got {len(eq_violations)}"
    )
