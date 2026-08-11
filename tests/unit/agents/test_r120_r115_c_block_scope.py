"""R120 regression tests for R115.C block-scoped wrap.

Pre-R120: R115.C emitted `const _r115_c_loc = ...` per wrap site at
function scope. With 2+ wraps in the same `test()` body, TypeScript
rejected the second `const` declaration with:
  SyntaxError: Identifier '_r115_c_loc' has already been declared.

Live evidence (run-349a9f, post-R119.B): 7 of 9 dispatched PW specs
hit this error → rc=1 tests=0 (compile failure before any test()
block could execute). Pre-R119.B this was hidden behind R30.5
TARGET_VISION_ASSIST blocks.

R120 wraps each R115.C injection inside a `{ ... }` block so the
const declarations are block-scoped. Two adjacent wraps coexist
because each has its own lexical scope.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


def test_r120_block_scoped_wrap_emits_braces():
    """A single R115.C wrap output should be enclosed in `{ }` block
    to scope const declarations locally."""
    src = """import { test, expect } from '@playwright/test';

test('foo', async ({ page }) => {
  await expect(page.getByRole('button', { name: 'Save' })).toBeVisible({ timeout: 15000 });
});
"""
    out, n = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    assert n == 1, f"Expected 1 wrap; got {n}"
    # R120 marker present
    assert "R120 block-scoped" in out, f"R120 marker missing:\n{out}"
    # Each wrap opens with `{` and contains the const declaration inside
    # Look for `{ // R115.C — ... R120` pattern
    import re as _re
    block_open = _re.search(r"\{\s*//\s*R115\.C.*R120 block-scoped", out)
    assert block_open, f"Expected block-opening `{{ // R115.C ... R120` pattern; got:\n{out}"


def test_r120_two_adjacent_wraps_no_duplicate_const():
    """Two adjacent long-timeout `toBeVisible` calls inside ONE test()
    function — pre-R120 this produced duplicate `const _r115_c_loc`
    declarations and TypeScript SyntaxError. Post-R120 each wrap is
    block-scoped → both `const _r115_c_loc` declarations live in
    separate lexical scopes → no duplicate-identifier error."""
    src = """import { test, expect } from '@playwright/test';

test('two-buttons', async ({ page }) => {
  await expect(page.getByRole('button', { name: 'Save' })).toBeVisible({ timeout: 15000 });
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeVisible({ timeout: 15000 });
});
"""
    out, n = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    assert n == 2, f"Expected 2 wraps; got {n}"
    # 2 `const _r115_c_loc` declarations + 2 opening/closing braces for blocks
    assert out.count("const _r115_c_loc") == 2
    # Verify each `const _r115_c_loc` lives INSIDE its own `{` block
    # by checking that the number of R120 block-opens equals wrap count
    assert out.count("R120 block-scoped") == 2, (
        f"Expected 2 R120 block-scoped markers; got "
        f"{out.count('R120 block-scoped')}\n{out}"
    )


def test_r120_block_scoping_preserves_assertion_semantics():
    """The wrapped output must still emit `expect(...).toBe(true)`
    inside the block scope — the assertion semantics (assert visible)
    are NOT broken by R120 block-scoping."""
    src = """import { test, expect } from '@playwright/test';

test('foo', async ({ page }) => {
  await expect(page.getByRole('button', { name: 'Save' })).toBeVisible({ timeout: 15000 });
});
"""
    out, _n = AutomationEngineerAgent._r115_c_inject_vision_fallback(src)
    # The final assertion must still be present, and must reference _r115_c_visible
    assert "expect(_r115_c_visible," in out
    assert "must be visible" in out
    assert ".toBe(true)" in out
