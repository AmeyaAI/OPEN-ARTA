"""R146.D — PW strict role-name validator for empty role_names catalog.

Iter 5 (run-2b3b3d) evidence: 41 of 145 PW FAILs were locator-timeouts
on hallucinated `getByRole('button', { name: 'Continue with Google' })`.
R45.3 discovery captured aria_labels + texts but raced with SPA hydration
→ role_names empty. R78.6 short-circuited on empty role_names → no gen-
time rejection → hallucinated specs reached runtime + failed.

R146.D fires ONLY when role_names empty BUT aria_labels OR texts populated
(proves discovery ran). Rejects every getByRole+name as
`catalog_role_name_unknown_strict` so R57.1 retry-with-hint corrects OR
R102.A stamps truthful BLOCKED.
"""
from __future__ import annotations
from src.agents.grounding_validator import validate_playwright_grounded


def test_r146_d_strict_fires_when_role_names_empty_but_labels_populated():
    """The Iter 5 root-cause scenario: catalog has aria_labels but empty
    role_names → R146.D rejects hallucinated getByRole+name calls."""
    content = """
    test('foo', async ({ page }) => {
      await page.getByRole('button', { name: 'Continue with Google' }).click();
    });
    """
    dom_catalog = {"testids": []}
    stable_selectors = {
        "role_names": set(),                # empty — the timing-race case
        "aria_labels": {"email", "password", "submit"},
        "texts": {"sign in", "forgot password"},
    }
    violations = validate_playwright_grounded(
        content,
        project_id="test",
        dom_catalog=dom_catalog,
        stable_selectors=stable_selectors,
    )
    strict_violations = [
        v for v in violations
        if v.kind == "catalog_role_name_unknown_strict"
    ]
    assert len(strict_violations) == 1
    assert "Continue with Google" in strict_violations[0].symbol


def test_r146_d_strict_does_not_fire_when_role_names_populated():
    """When role_names catalog has data, existing R78.6 handles it; R146.D
    must NOT fire (would create duplicate violations)."""
    content = """
    test('foo', async ({ page }) => {
      await page.getByRole('button', { name: 'Login' }).click();
    });
    """
    dom_catalog = {"testids": []}
    stable_selectors = {
        "role_names": {("button", "Login"), ("button", "Submit")},
        "aria_labels": {"email"},
        "texts": set(),
    }
    violations = validate_playwright_grounded(
        content,
        project_id="test",
        dom_catalog=dom_catalog,
        stable_selectors=stable_selectors,
    )
    strict_violations = [
        v for v in violations
        if v.kind == "catalog_role_name_unknown_strict"
    ]
    assert len(strict_violations) == 0  # R78.6 fuzzy match path handles it


def test_r146_d_strict_does_not_fire_when_catalog_entirely_empty():
    """Cold-start case (all signals empty) → R55.5 WARN already handles;
    R146.D must NOT add noise."""
    content = """
    test('foo', async ({ page }) => {
      await page.getByRole('button', { name: 'X' }).click();
    });
    """
    stable_selectors = {
        "role_names": set(),
        "aria_labels": set(),
        "texts": set(),
    }
    violations = validate_playwright_grounded(
        content,
        project_id="test",
        dom_catalog={"testids": []},
        stable_selectors=stable_selectors,
    )
    strict_violations = [
        v for v in violations
        if v.kind == "catalog_role_name_unknown_strict"
    ]
    assert len(strict_violations) == 0  # cold-start path took the R55.5 return


def test_r146_d_killswitch_honored(monkeypatch):
    """ARTA_R146_D_STRICT_ROLE_DISABLE=1 reverts to pre-R146.D behavior."""
    monkeypatch.setenv("ARTA_R146_D_STRICT_ROLE_DISABLE", "1")
    content = """
    test('foo', async ({ page }) => {
      await page.getByRole('button', { name: 'Y' }).click();
    });
    """
    stable_selectors = {
        "role_names": set(),
        "aria_labels": {"email"},  # would trigger R146.D without killswitch
        "texts": set(),
    }
    violations = validate_playwright_grounded(
        content,
        project_id="test",
        dom_catalog={"testids": []},
        stable_selectors=stable_selectors,
    )
    strict_violations = [
        v for v in violations
        if v.kind == "catalog_role_name_unknown_strict"
    ]
    assert len(strict_violations) == 0
