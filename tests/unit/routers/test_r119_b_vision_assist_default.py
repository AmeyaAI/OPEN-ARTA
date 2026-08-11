"""R119.B regression tests for the TARGET_VISION_ASSIST auto-default.

Pre-R119.B: R115.C's vision_assist.ts helpers (imported by every PW spec
using long-timeout toBeVisible) reference `process.env.TARGET_VISION_ASSIST`.
R30.5's pre-dispatch var-scan flagged this as "unresolved" → BLOCKED.

Smoke run-af070d evidence: 10 of 11 PW specs BLOCKED by R30.5 with
`unresolved_vars=['TARGET_VISION_ASSIST']` (operator-facing "fill via
Settings" CTA when the var is actually ARTA-internal opt-in).

R119.B auto-defaults the var to "0" (vision-assist OFF) at PW dispatch
env construction. Operator-supplied "1" still wins via the existing
merge precedence (os.environ > env_config > default).
"""
from __future__ import annotations

import inspect
import re

import src.api.routers.execution as _execution_mod


def test_r119_b_default_in_dispatch_env_constructor():
    """The PW dispatch env construction must include TARGET_VISION_ASSIST
    with the (os.environ → env_config → '0') precedence chain."""
    # Read the module source directly — the dispatch env is built in
    # an inline dict inside _run_playwright (not a separate helper),
    # so we verify the source contains the R119.B fallback shape.
    source = inspect.getsource(_execution_mod)
    # The R119.B marker comment must be present
    assert "R119.B KEYSTONE" in source, "R119.B comment missing"
    # The auto-default chain must look like:
    #   os.environ.get("TARGET_VISION_ASSIST")
    #     or (env_config or {}).get("TARGET_VISION_ASSIST")
    #     or "0"
    pattern = re.compile(
        r'"TARGET_VISION_ASSIST"\s*:\s*\(\s*'
        r'os\.environ\.get\(\s*"TARGET_VISION_ASSIST"\s*\)\s*'
        r'or\s*\(env_config\s*or\s*\{\}\)\.get\(\s*"TARGET_VISION_ASSIST"\s*\)\s*'
        r'or\s*"0"\s*\)',
        re.MULTILINE | re.DOTALL,
    )
    assert pattern.search(source), (
        "R119.B auto-default precedence chain not found in execution.py"
    )


def test_r119_b_operator_override_wins():
    """The precedence chain must let an operator override the default via
    env_config (Settings → Environments → Variables) — the auto-default
    is just a fallback for ARTA-internal cases where the operator
    deliberately leaves it unset."""
    source = inspect.getsource(_execution_mod)
    # The fallback expression uses nested parens (env_config or {}) — find
    # the precedence chain by string search across the full module source.
    # os.environ comes BEFORE env_config which comes BEFORE the literal "0".
    # We use rfind on the default to disambiguate from other "0" occurrences.
    anchor = source.find('"TARGET_VISION_ASSIST": (')
    assert anchor > 0, "R119.B anchor not found"
    # Slice a 1000-char window after the anchor to inspect the expression
    window = source[anchor:anchor + 1000]
    pos_envvar = window.find('os.environ.get("TARGET_VISION_ASSIST")')
    pos_envcfg = window.find('(env_config or {}).get("TARGET_VISION_ASSIST")')
    pos_default = window.find('"0"')
    assert 0 < pos_envvar < pos_envcfg < pos_default, (
        f"Precedence order broken: os.environ@{pos_envvar} < "
        f"env_config@{pos_envcfg} < default@{pos_default}\n"
        f"window head:\n{window[:400]}"
    )
