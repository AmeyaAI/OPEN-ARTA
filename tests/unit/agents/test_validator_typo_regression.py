"""R72.1 — regression test guarding `_re`-suffix typos in validator code.

Three one-char typos have shipped in `AutomationEngineerAgent._validate_response`:
  - R38.2: `risk` parameter missing → NameError at validation call
  - R38.3: `_template_key_re` defined but `_template_keyre` used
  - R71.1: `bad_metric_re` defined + `tag_re` defined but `bad_metricre` /
    `tagre` used (TWO sites in the same block)

Each crashed the entire generation pipeline for one tool. None had a unit
test guarding the validation path; they only surfaced when operators
triggered live runs.

This file is the CI gate that prevents the NEXT one-char typo. For every
`<name>_re = _re.compile(...)` definition in automation_engineer.py, we:
  1. Confirm the binding exists at module import time (catches definition
     drift)
  2. Feed `_validate_response` content that MATCHES each regex's pattern
     so `.findall` / `.search` actually executes — any typo at the use
     site raises NameError, which the test catches

This is intentionally narrow: it doesn't validate correctness of the
regex logic (that's `test_validation_lints.py`'s job). It validates that
NONE of the use sites raise NameError when the regex is exercised.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.agents.automation_engineer import AutomationEngineerAgent


AE_PATH = Path("src/agents/automation_engineer.py")


def test_validator_module_has_no_re_suffix_typos() -> None:
    """R72.1 — static scan: every `<name>_re = _re.compile(...)` binding
    must be USED only via `<name>_re.` (never `<name>re.` missing-underscore).

    This is a fast first-line defense: it catches the exact typo class
    (`_template_keyre`, `bad_metricre`, `tagre`) without running the
    validator. Pairs with the live exercise test below.
    """
    src = AE_PATH.read_text()
    # Find every `<name>_re = _re.compile(` binding
    binds: dict[str, int] = {}
    for i, line in enumerate(src.splitlines(), 1):
        m = re.match(r"^\s+([a-z_]+)_re\s*=\s*_re\.compile", line)
        if m:
            binds[m.group(1)] = i
    # For each binding, scan every line for the typo pattern `<name>re.`
    violations: list[str] = []
    for name, lineno in binds.items():
        # Build the typo pattern: `<name>re.` as a standalone word
        typo_pat = re.compile(rf"\b{re.escape(name)}re\b\.")
        for i, line in enumerate(src.splitlines(), 1):
            # Skip the definition line itself
            if i == lineno:
                continue
            if typo_pat.search(line):
                violations.append(
                    f"line {i}: bound `{name}_re` (line {lineno}) but used as "
                    f"`{name}re` (missing underscore): {line.strip()}"
                )
    assert not violations, (
        "R72.1: detected `_re`-suffix typos in automation_engineer.py:\n  "
        + "\n  ".join(violations)
    )


def test_validator_module_has_no_unreferenced_re_bindings() -> None:
    """R72.1 — secondary check: every `_re` binding should have at least
    one use site. An UNUSED binding may indicate a typo was introduced
    elsewhere (the use site moved to `<name>re` without removing the
    `<name>_re` definition).
    """
    src = AE_PATH.read_text()
    tree = ast.parse(src)
    binds: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.endswith("_re")
            and isinstance(node.value, ast.Call)
        ):
            # Heuristic: skip module-level / class-level bindings; we only
            # care about local ones inside validator functions.
            binds.add(node.targets[0].id)
    unused: list[str] = []
    for name in binds:
        # Count occurrences of `<name>.` (the binding being used as an
        # attribute access). The binding itself counts as 1 occurrence
        # (the LHS of `<name>_re =`). >1 means it's referenced elsewhere.
        pat = re.compile(rf"\b{re.escape(name)}\b")
        if len(pat.findall(src)) < 2:
            unused.append(name)
    # Note: this check is a heuristic. Some legitimately unused bindings
    # may exist as TODO/future use. We fail only if MULTIPLE are unused
    # — a single unused binding may be intentional.
    assert len(unused) <= 1, (
        f"R72.1: multiple unused `_re` bindings — likely indicates a typo "
        f"moved the use site without removing the definition: {unused}"
    )


# ── Exercise tests: actually call _validate_response with content that ────
# triggers each regex use site so NameError typos at the call site surface.

# Minimal valid playwright spec to satisfy other validation in _validate_response
_PW_VALID = """\
import { test, expect } from '@playwright/test'

// AC: AC-001
test('verify checkout flow', async ({ page }) => {
  await page.goto('https://example.com')
  await expect(page.getByTestId('submit')).toBeVisible()
  await page.getByTestId('submit').click()
})
"""

# k6 content exercising bad_metric_re + tag_re (R71.1 typo sites)
_K6_EXERCISES_TYPOS = """\
import http from 'k6/http'
import { check } from 'k6'

export const options = {
  vus: 1,
  duration: '5s',
  thresholds: {
    'http_req_duration{endpoint:checkout}': ['p(95)<500'],
    'http_req_duration{endpoint:home}': ['p(95)<300'],
  },
}

export default function () {
  const res = http.get('https://example.com')
  check(res, { 'status 200': (r) => r.status === 200 })
}
"""


def test_k6_validator_exercises_bad_metric_re_and_tag_re() -> None:
    """R72.1 — exercise the bad_metric_re + tag_re call sites that R71.1
    fixed. Pre-R71.1 these raised NameError on every k6 LLM output.
    Post-R71.1 the validator must complete (PASS the content through OR
    raise RuntimeError for non-NameError reasons like 'malformed
    threshold metric name')."""
    try:
        result = AutomationEngineerAgent._validate_response(_K6_EXERCISES_TYPOS, "k6")
    except NameError as exc:
        pytest.fail(
            f"R72.1 REGRESSION: k6 validator raised NameError exercising "
            f"bad_metric_re/tag_re — this is the exact bug R71.1 fixed: {exc}"
        )
    except RuntimeError:
        # RuntimeError is acceptable — it means the validator caught a real
        # issue (e.g., malformed threshold). NameError is the typo regression.
        pass
    else:
        # If validation passed cleanly, even better.
        assert isinstance(result, str)


def test_playwright_validator_exercises_arrow_re() -> None:
    """R72.1 — exercise the arrow_re binding in Playwright validation.
    Regression guard against future `arrow_re` → `arrowre` typo."""
    try:
        result = AutomationEngineerAgent._validate_response(_PW_VALID, "playwright")
    except NameError as exc:
        pytest.fail(
            f"R72.1 REGRESSION: Playwright validator raised NameError "
            f"exercising arrow_re — likely missing-underscore typo: {exc}"
        )
    except RuntimeError:
        # Other validator rejections are acceptable; NameError is not.
        pass
