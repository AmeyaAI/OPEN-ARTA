"""R77.6.γ — single source of truth for the dispatcher↔consumer env-var contract.

This registry documents EVERY env var the dispatcher sets and EVERY env
var test consumers (Playwright config, Newman CLI flags, k6 scripts,
Axe a11y specs, pytest, ZAP YAMLs) read. Drift between sides — a
setter without a reader, or a reader without a setter — is caught by
the consistency test below.

When adding a new ``TARGET_*`` var or a new tool dispatcher, update this
registry FIRST. The tests in
``tests/unit/shared/test_env_var_contract.py`` will fail otherwise — by
design.

Reasoning for separate sets:
- ``DISPATCHER_REQUIRED`` — vars the dispatcher unconditionally sets on
  ``test_env`` regardless of auth method / suite / project. Failing to
  set one of these breaks every dispatched test.
- ``DISPATCHER_CONDITIONAL`` — vars whose presence depends on runtime
  state (auth method, suite filter, single-test mode, discovery cache).
  Every reader of these must tolerate absence.
- ``CONSUMER_READS`` — vars each tool's test code (or config file)
  reads via ``process.env`` / ``__ENV`` / ``--env-var`` / template
  substitution. Used by the consistency test to verify every reader
  has at least one setter.
"""
from __future__ import annotations

from typing import Set, Dict

# Vars the dispatcher MUST set in test_env for every run.
# Source: src/api/routers/execution.py:1438-1450 base test_env.
DISPATCHER_REQUIRED: Set[str] = {
    "TARGET_BASE_URL",
    "TARGET_API_BASE_URL",
    "BASE_URL",
    "API_BASE_URL",
    "TARGET_TEST_DIR",
    "TARGET_ARTIFACTS_DIR",
    "TARGET_HTML_REPORT_DIR",
    "TARGET_RESULTS_PATH",
    "TARGET_AUTH_METHOD",
    "TARGET_AUTH_STATE_PATH",
}

# Vars the dispatcher conditionally sets. Comments describe the trigger.
DISPATCHER_CONDITIONAL: Dict[str, str] = {
    "TARGET_AUTH_COOKIE_NAME":   "set when auth_method == 'cookie'",
    "TARGET_AUTH_COOKIE_VALUE":  "set when auth_method == 'cookie'",
    "TARGET_AUTH_BEARER_TOKEN":  "set when auth_method == 'bearer'",
    "TARGET_AUTH_USERNAME":      "set when auth_method == 'basic'",
    "TARGET_AUTH_PASSWORD":      "set when auth_method == 'basic'",
    "TARGET_AUTH_LOCALSTORAGE":  "set when auth_method == 'cookie' (R77.6.α: falls through to storage-state JSON when projects.json creds.localStorage is empty)",
    "TARGET_AUTH_ROLES":         "set when multi-role auth is configured",
    "TARGET_TEST_MATCH":         "set when project filter applies (always for project-scoped runs)",
    "TARGET_TEST_GREP":          "set in single-test mode only",
    "TARGET_HAR_PATH":           "set per Playwright spec (R76)",
    "A11Y_REPORT_PATH":          "set per Axe stage (required by *_a11y.spec.ts)",
    "ARTA_HAR_OUT":              "set for the discovery probe project only",
    # Newman --env-var keys (NOT subprocess env; injected via Postman var scope)
    # are registered here so the consistency test can verify each name has
    # a setter path. The dispatcher derives these from the same test_env
    # values listed above.
    "AUTH_TOKEN":                "k6: derived from TARGET_AUTH_BEARER_TOKEN OR TARGET_AUTH_COOKIE_VALUE",
    "K6_TARGET_URL":             "k6: alias for BASE_URL",
}

# Vars each tool's consumer reads. Used by the consistency test:
# every entry here MUST appear in DISPATCHER_REQUIRED or
# DISPATCHER_CONDITIONAL. When adding a new reader, register the
# setter too — the test fails loudly on drift.
#
# Sources verified live:
# - playwright: src/automation/playwright/playwright.config.ts +
#   src/automation/common/playwright.base.config.ts +
#   src/automation/common/auth-setup.ts
# - newman:    --env-var args set in src/api/routers/execution.py:4034-4103
# - k6:        src/automation/k6/checkout-performance.js __ENV.* refs
# - axe:       src/automation/playwright/req_am_*_a11y.spec.ts
# - zap:       YAML template substitution at execution.py:5628 (no env vars)
# - pytest:    src/automation/python_tests/arta_runtime/__init__.py:119
CONSUMER_READS: Dict[str, Set[str]] = {
    "playwright": {
        "TARGET_TEST_MATCH",
        "TARGET_TEST_GREP",
        "TARGET_AUTH_STATE_PATH",
        "TARGET_RESULTS_PATH",
        "TARGET_HAR_PATH",
        "TARGET_TEST_DIR",
        "TARGET_HTML_REPORT_DIR",
        "TARGET_BASE_URL",
        "TARGET_AUTH_COOKIE_NAME",
        "TARGET_AUTH_COOKIE_VALUE",
        "TARGET_AUTH_METHOD",
        "TARGET_AUTH_BEARER_TOKEN",
        "TARGET_AUTH_USERNAME",
        "TARGET_AUTH_PASSWORD",
        "TARGET_AUTH_LOCALSTORAGE",
        "TARGET_AUTH_ROLES",
        "ARTA_HAR_OUT",
        "BASE_URL",
        "API_BASE_URL",
    },
    "newman": {
        "TARGET_AUTH_COOKIE_NAME",
        "TARGET_AUTH_COOKIE_VALUE",
        "TARGET_AUTH_BEARER_TOKEN",
        "TARGET_BASE_URL",
        "TARGET_API_BASE_URL",
        "TARGET_AUTH_STATE_PATH",
    },
    "k6": {
        "BASE_URL",
        "K6_TARGET_URL",
        "AUTH_TOKEN",
        # R77.6.β adds dynamic __ENV.* path-param vars resolved via R43
        # at dispatch — they're project-specific (SUBSCRIPTION_ID,
        # SCHEMA_ID, etc.) and not enumerable in a static registry.
    },
    "axe": {
        "A11Y_REPORT_PATH",
        "BASE_URL",
        # Axe specs run under Playwright so all Playwright TARGET_AUTH_*
        # are transitively read; not duplicated here to keep the registry
        # focused on direct reads.
    },
    "zap": set(),   # YAML template substitution; no subprocess env vars
    "pytest": {
        "ARTA_ANALYTICS_BACKEND",   # optional operator knob; stub if absent
    },
}

# Vars set internally by the dispatcher for its OWN bookkeeping (artifact
# paths, HTML report dirs) that no tool consumer reads directly. The
# consistency test excludes these from "orphan setter" checks.
INTERNAL_USE_ONLY: Set[str] = {
    "TARGET_ARTIFACTS_DIR",
    "TARGET_HTML_REPORT_DIR",
}

# Vars consumers may read but the dispatcher does NOT set — they come
# from operator-level config (deployment env vars, container env, etc.).
# The consistency test exempts these from "reader without setter" checks
# because the harness explicitly delegates them outside its scope.
OPERATOR_SET_ONLY: Set[str] = {
    "ARTA_ANALYTICS_BACKEND",   # pytest analytics: operator points to a real backend module
}
