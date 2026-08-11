"""R102 regression tests — dispatch-time BLOCK + deterministic rewriter.

Three components tested:
- R102.A stamp: prepend comment-header listing violations onto PW spec
- R102.E rewriter: transform `test.beforeEach(async ({...}, use) => {...await use()...})`
  → split into paired before/after hooks (deterministic pass-rate fix)
- R102.C dispatch parser: read the R102.A header back from disk for BLOCK
  decision (validated separately in tests/unit/routers/ — here we just verify
  the header roundtrips correctly).

Live evidence (run-8c03c9): 11 PW FAILs from `_test.request.X` / `await use()` —
R102.E rewrites the `await use()` shape deterministically; R102.A persists
violations for dispatch BLOCK; R102.C emits BLOCKED rows.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent
from src.agents.grounding_validator import GroundingViolation


@pytest.fixture()
def agent():
    return AutomationEngineerAgent(client=AsyncMock())


# ── R102.A: stamp helper ─────────────────────────────────────────────


def test_r102a_stamp_prepends_violation_header(agent: AutomationEngineerAgent):
    """Verifies the comment-header shape R102.C parses on disk."""
    content = "import { test, expect } from '@playwright/test';\n// spec body\n"
    violations = [
        GroundingViolation(
            tool="playwright", kind="bad_playwright_api",
            symbol="await use(", location="line 5",
            hint="use is only valid in fixture definitions",
        ),
    ]
    out = agent._r102_a_stamp_violations(content, violations)
    assert "// _dispatch_block_kind: playwright_grounding_violation" in out
    assert "// _grounding_violations:" in out
    assert '"kind": "bad_playwright_api"' in out
    assert '"symbol": "await use("' in out
    # Original content preserved
    assert "import { test, expect }" in out


def test_r102a_stamp_idempotent(agent: AutomationEngineerAgent):
    """Re-running R102.A on already-stamped content is a no-op."""
    content = "import {test} from '@playwright/test';\n"
    violations = [GroundingViolation(
        tool="playwright", kind="bad_playwright_api",
        symbol="x", location="y", hint="z",
    )]
    first = agent._r102_a_stamp_violations(content, violations)
    second = agent._r102_a_stamp_violations(first, violations)
    assert first == second


def test_r102a_empty_violations_unchanged(agent: AutomationEngineerAgent):
    """No violations → no header → content unchanged."""
    content = "import { test } from '@playwright/test';\n"
    assert agent._r102_a_stamp_violations(content, []) == content


# ── R102.C: dispatch-time parser (header round-trip) ─────────────────


def test_r102c_can_extract_violations_from_stamped_content(
    agent: AutomationEngineerAgent,
):
    """R102.C reads the first 2KB looking for the comment-header. Verify
    the marker string + JSON lines roundtrip correctly."""
    import json
    import re as _re
    content = "import {} from '@playwright/test';\n"
    violations = [
        GroundingViolation(
            tool="playwright", kind="bad_playwright_api",
            symbol="await use(", location="line 5",
            hint="see BEFORE/AFTER example",
        ),
    ]
    stamped = agent._r102_a_stamp_violations(content, violations)
    head = stamped[:2000]
    assert "_dispatch_block_kind: playwright_grounding_violation" in head
    _viol_lines = _re.findall(r'^//\s+(\{[^\n]+\})\s*$', head, _re.MULTILINE)
    assert len(_viol_lines) == 1
    parsed = json.loads(_viol_lines[0])
    assert parsed["kind"] == "bad_playwright_api"
    assert parsed["symbol"] == "await use("


# ── R102.E: deterministic rewriter ───────────────────────────────────


def test_r102e_strips_use_param_when_no_teardown(agent: AutomationEngineerAgent):
    """Simple case: hook has `, use)` param + `await use()` mid-body but
    no teardown. Strip the param + remove the await use() call."""
    src = (
        "test.beforeEach(async ({ page }, use) => {\n"
        "  await seedData(page);\n"
        "  await use();\n"
        "});\n"
    )
    out, count = agent._r102_e_rewrite_hook_use(src)
    assert count == 1
    assert ", use)" not in out
    assert "await use(" not in out
    assert "await seedData(page);" in out


def test_r102e_splits_setup_teardown_into_paired_hooks(
    agent: AutomationEngineerAgent,
):
    """Operator intent preserved: code before await use() → beforeEach;
    code after → matching afterEach."""
    src = (
        "test.beforeEach(async ({ page }, use) => {\n"
        "  await seedData(page);\n"
        "  await use();\n"
        "  await cleanupData(page);\n"
        "});\n"
    )
    out, count = agent._r102_e_rewrite_hook_use(src)
    assert count == 1
    assert "test.beforeEach(async ({ page }) =>" in out
    assert "test.afterEach(async ({ page }) =>" in out
    assert "await seedData(page);" in out
    assert "await cleanupData(page);" in out
    assert "await use(" not in out


def test_r102e_idempotent(agent: AutomationEngineerAgent):
    """Re-run on already-rewritten content is a no-op (no `, use)` left)."""
    src = (
        "test.beforeEach(async ({ page }, use) => {\n"
        "  await seedData(page);\n"
        "  await use();\n"
        "});\n"
    )
    first, c1 = agent._r102_e_rewrite_hook_use(src)
    second, c2 = agent._r102_e_rewrite_hook_use(first)
    assert c1 == 1
    assert c2 == 0
    assert first == second


def test_r102e_clean_hook_unchanged(agent: AutomationEngineerAgent):
    """Hooks without `, use)` are NOT touched."""
    src = (
        "test.beforeEach(async ({ page }) => {\n"
        "  await seedData(page);\n"
        "});\n"
    )
    out, count = agent._r102_e_rewrite_hook_use(src)
    assert count == 0
    assert out == src


def test_r102e_multiple_hooks_all_transformed(agent: AutomationEngineerAgent):
    """Multiple broken hooks in same file → all rewritten."""
    src = (
        "test.beforeEach(async ({ page }, use) => {\n"
        "  await setup1(page);\n"
        "  await use();\n"
        "});\n"
        "test.beforeAll(async ({ browser }, use) => {\n"
        "  await initBrowser(browser);\n"
        "  await use();\n"
        "});\n"
    )
    out, count = agent._r102_e_rewrite_hook_use(src)
    assert count == 2
    assert ", use)" not in out
    assert "await use(" not in out


def test_r102e_test_extend_fixture_NOT_touched(agent: AutomationEngineerAgent):
    """`test.extend({ myFixture: async (deps, use) => { await use(...) }})` is
    the CORRECT fixture-definition pattern. R102.E must NOT rewrite it.

    The regex matches ONLY `test.{before,after}{Each,All}(` — test.extend's
    `{ myFixture:` shape doesn't match, so the fixture definition stays."""
    src = (
        "const test = baseTest.extend({\n"
        "  authedPage: async ({ page }, use) => {\n"
        "    await login(page);\n"
        "    await use(page);\n"
        "  },\n"
        "});\n"
    )
    out, count = agent._r102_e_rewrite_hook_use(src)
    assert count == 0
    assert out == src
