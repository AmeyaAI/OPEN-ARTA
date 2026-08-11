"""R332 — deterministic hard-wait autofix: rewrite the BMAD-forbidden
`page.waitForTimeout(N)` into a condition-based wait so it never costs an LLM
retry against the F1 validator (retry-storm root cause). SUT-agnostic."""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent as A


def test_rewrites_page_wait_for_timeout():
    src = "await page.waitForTimeout(3000);\nawait page.click('#x');"
    out, n = A._autofix_hard_wait(src)
    assert n == 1
    assert "waitForTimeout" not in out
    assert "await page.waitForLoadState('networkidle').catch(() => {})" in out
    assert "await page.click('#x')" in out  # untouched


def test_preserves_receiver():
    out, n = A._autofix_hard_wait("await this.page.waitForTimeout(500);")
    assert n == 1
    assert "await this.page.waitForLoadState('networkidle').catch(() => {})" in out


def test_multiple_occurrences_and_var_arg():
    src = "await page.waitForTimeout(1000);\nawait page.waitForTimeout(delayMs);"
    out, n = A._autofix_hard_wait(src)
    assert n == 2
    assert "waitForTimeout" not in out


def test_no_op_when_absent():
    src = "await page.getByRole('button').click();"
    out, n = A._autofix_hard_wait(src)
    assert n == 0 and out == src


def test_result_passes_f1_forbidden_pattern():
    # The rewritten output must NOT match the F1 waitForTimeout reject pattern.
    import re
    out, _ = A._autofix_hard_wait("await page.waitForTimeout(2000);")
    assert not re.search(r"page\.waitForTimeout\s*\(", out)


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R332_HARD_WAIT_AUTOFIX_DISABLE", "1")
    src = "await page.waitForTimeout(3000);"
    out, n = A._autofix_hard_wait(src)
    assert n == 0 and out == src
