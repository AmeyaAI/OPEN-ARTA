"""R111.A + R111.F + R111.K + R111.L — BEFORE/AFTER hint completeness.

Live evidence (run-99dbcf): PW PASS = 0; LLM continues to produce
`bad_playwright_api` and `unknown_endpoint` violations across all 3
retries because the validator hints were PROSE-ONLY for 6 of 9 kinds.
R110.B's BEFORE/AFTER format for `import { request }` dropped on-disk
residual 3+ → 1. R111 ports the same idiom to the remaining 5 PW kinds
+ k6 `unset_env_var` + pytest `assertion_value_drift`.

Every test asserts the hint contains "BEFORE" and "AFTER" markers PLUS
a concrete code snippet (Playwright/k6/pytest syntax).
"""
from __future__ import annotations

from src.agents.grounding_validator import (
    validate_playwright_api_usage,
    validate_playwright_grounded,
    validate_k6_grounded,
)


# ── R111.A — bad_playwright_api Pattern 1 (testInfo.fixture) ──────────────


def test_r111_a_pattern1_testinfo_fixture_has_before_after():
    # Pattern 1 regex matches `_test.test.info(...).fixture` directly.
    src = """\
import { test, expect } from '@playwright/test';
test('foo', async ({ page }) => {
  const data = _test.test.info().fixture;
});
"""
    vs = validate_playwright_api_usage(src)
    p1_vs = [v for v in vs if "fixture" in v.symbol]
    assert p1_vs, f"expected Pattern 1 violation: {[v.symbol for v in vs]}"
    hint = p1_vs[0].hint
    assert "BEFORE" in hint and "AFTER" in hint, f"hint missing markers: {hint[:200]}"
    assert "test.extend" in hint or "myFixture" in hint, f"hint missing example: {hint[:200]}"


# ── R111.A — bad_playwright_api Pattern 2 (toBeOK on locator) ────────────


def test_r111_a_pattern2_toBeOK_on_locator_has_before_after():
    # Detection key: receiver starts with `page.` (inline locator chain).
    src = """\
import { test, expect } from '@playwright/test';
test('foo', async ({ page }) => {
  await expect(page.getByRole('button', { name: 'Save' })).toBeOK();
});
"""
    vs = validate_playwright_api_usage(src)
    p2_vs = [v for v in vs if "toBeOK" in v.symbol]
    assert p2_vs, f"expected Pattern 2 violation: {[v.symbol for v in vs]}"
    hint = p2_vs[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "page.request" in hint, f"hint missing page.request example: {hint[:200]}"
    assert "toBeVisible" in hint or "toHaveURL" in hint


# ── R111.A — bad_playwright_api Pattern 3 (page.fixture) ─────────────────


def test_r111_a_pattern3_page_fixture_has_before_after():
    src = """\
import { test, expect } from '@playwright/test';
test('foo', async ({ page }) => {
  const data = await page.fixture('userData');
});
"""
    vs = validate_playwright_api_usage(src)
    p3_vs = [v for v in vs if "page.fixture" in v.symbol]
    assert p3_vs, f"expected Pattern 3 violation: {[v.symbol for v in vs]}"
    hint = p3_vs[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "{ page, userData }" in hint or "test.extend" in hint


# ── R111.A — hallucinated_role has BEFORE/AFTER w/ catalog alternatives ─


def test_r111_a_hallucinated_role_has_before_after():
    src = "page.getByRole('phantom', { name: 'X' }).click();"
    vs = validate_playwright_grounded(
        src,
        project_id="x",
        dom_catalog={"testids": []},
        stable_selectors={"role_names": {("button", "Save"), ("main", "Home")}},
    )
    rv = [v for v in vs if v.kind == "hallucinated_role"]
    assert rv, f"expected hallucinated_role: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "button" in hint or "main" in hint, f"hint missing catalog role: {hint[:200]}"


# ── R111.A — hallucinated_role_name has BEFORE/AFTER w/ catalog names ────


def test_r111_a_hallucinated_role_name_has_before_after():
    src = "page.getByRole('button', { name: 'Phantom Name' }).click();"
    vs = validate_playwright_grounded(
        src,
        project_id="x",
        dom_catalog={"testids": []},
        stable_selectors={"role_names": {("button", "Save"), ("button", "Cancel")}},
    )
    rv = [v for v in vs if v.kind == "hallucinated_role_name"]
    assert rv, f"expected hallucinated_role_name: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "Save" in hint or "Cancel" in hint


# ── R111.A — hallucinated_testid has BEFORE/AFTER w/ catalog testids ─────


def test_r111_a_hallucinated_testid_has_before_after():
    src = "await page.getByTestId('phantom-id').click();"
    vs = validate_playwright_grounded(
        src,
        project_id="x",
        dom_catalog={"testids": ["save-btn", "cancel-btn"]},
        stable_selectors=None,
    )
    rv = [v for v in vs if v.kind == "hallucinated_testid"]
    assert rv, f"expected hallucinated_testid: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "save-btn" in hint or "cancel-btn" in hint


# ── R111.F — PW unknown_endpoint has BEFORE/AFTER w/ alternatives ────────


def test_r111_f_pw_unknown_endpoint_has_before_after():
    src = """\
test('foo', async ({ page }) => {
  await page.request.post(`${apiBase}/v1/phantom`);
});
"""
    vs = validate_playwright_grounded(
        src,
        project_id="x",
        captured_endpoints=[
            {"method": "POST", "path": "/api/v1/datasets"},
            {"method": "POST", "path": "/api/v1/projects"},
        ],
    )
    rv = [v for v in vs if v.kind == "unknown_endpoint"]
    assert rv, f"expected unknown_endpoint: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    # Either alternative path or guidance referring to captured set
    assert "captured_endpoints" in hint or "Captured" in hint


# ── R111.F — Newman unknown_endpoint has BEFORE/AFTER w/ captured paths ──


def test_r111_f_newman_unknown_endpoint_has_before_after():
    from src.agents.grounding_validator import validate_newman_grounded
    collection = {
        "item": [{
            "name": "GET phantom",
            "request": {
                "method": "GET",
                "url": {"raw": "{{base_url}}/v1/phantom", "host": ["{{base_url}}"], "path": ["v1", "phantom"]},
                "header": [],
            },
            "event": [{"listen": "test", "script": {"exec": ["pm.test('ok', () => pm.response.to.have.status(200));"]}}],
        }],
    }
    vs = validate_newman_grounded(
        collection,
        project_id="x",
        env_vars={"base_url": ""},
        captured_endpoints=[
            {"method": "GET", "path": "/api/v1/datasets"},
            {"method": "GET", "path": "/api/v1/projects"},
        ],
    )
    rv = [v for v in vs if v.kind == "unknown_endpoint"]
    assert rv, f"expected unknown_endpoint: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "Captured" in hint or "{{base_url}}" in hint


# ── R111.K — k6 unset_env_var has BEFORE/AFTER ───────────────────────────


def test_r111_k_k6_unset_env_var_has_before_after():
    src = "http.get(`${__ENV.PHANTOM_VAR}/api/path`);"
    vs = validate_k6_grounded(src, env_vars={"SOME_VAR": "x", "OTHER_VAR": "y"})
    rv = [v for v in vs if v.kind == "unset_env_var"]
    assert rv, f"expected unset_env_var: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    # k6-specific syntax in AFTER
    assert "__ENV." in hint


# ── R111.L — pytest assertion_value_drift has BEFORE/AFTER ───────────────


def test_r111_l_pytest_assertion_drift_has_before_after():
    from src.agents.grounding_validator import validate_pytest_grounded
    src = """\
def test_revenue():
    assert response.insight.metric == 'phantom_metric'
"""
    recipe = {
        "expected_outputs": {"metric": "sales", "value": 125000},
    }
    vs = validate_pytest_grounded(src, recipe=recipe)
    rv = [v for v in vs if v.kind == "assertion_value_drift"]
    assert rv, f"expected assertion_value_drift: {[v.kind for v in vs]}"
    hint = rv[0].hint
    assert "BEFORE" in hint and "AFTER" in hint
    assert "tolerant_assert" in hint or "getattr" in hint
