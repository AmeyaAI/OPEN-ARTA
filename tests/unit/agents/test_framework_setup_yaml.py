"""F8-22: YAML-validate every CI template the framework_setup_agent ships.

The agent emits CI workflow templates verbatim to user projects. A YAML syntax
regression here would silently break every project that runs `setup_framework`
until someone tries to push the broken pipeline. Validating each template at
unit-test time is cheap and catches regressions like:
  - misplaced indentation after a `script:` block
  - unescaped colons in inline shell
  - the F8-11 jq refactor accidentally breaking a multi-line block
"""
from __future__ import annotations

import pytest
import yaml

from src.agents.framework_setup_agent import CI_TEMPLATES


# Jenkinsfile is Groovy, not YAML — skip if present.
_YAML_FLAVOURS = [
    name for name in CI_TEMPLATES
    if name not in {"jenkins"}  # Jenkins uses Groovy DSL, not YAML
]


@pytest.mark.parametrize("flavour", _YAML_FLAVOURS)
def test_ci_template_is_valid_yaml(flavour: str) -> None:
    template = CI_TEMPLATES[flavour]
    assert template.strip(), f"Template {flavour} is empty"
    try:
        loaded = yaml.safe_load(template)
    except yaml.YAMLError as exc:
        pytest.fail(f"{flavour} CI template is not valid YAML: {exc}")

    # Every flavour we emit must be a mapping (a workflow file is always a dict)
    assert isinstance(loaded, dict), \
        f"{flavour} template parsed but did not yield a top-level mapping (got {type(loaded).__name__})"


@pytest.mark.parametrize("flavour", _YAML_FLAVOURS)
def test_ci_template_calls_arta_quality_gate(flavour: str) -> None:
    """Every CI flavour must include the call to /api/gates."""
    template = CI_TEMPLATES[flavour]
    assert "/api/gates" in template, \
        f"{flavour} template lost its /api/gates call (F8-11 regression check)"


@pytest.mark.parametrize("flavour", _YAML_FLAVOURS)
def test_ci_template_uses_jq_for_payload(flavour: str) -> None:
    """F8-11 regression check: payload must be built via `jq -n --arg` (safe
    against shell-quote injection in build_id), not interpolated `\\"...\\"`.
    """
    template = CI_TEMPLATES[flavour]
    # If the gate-call is present (it must be per the previous test), it has
    # to use jq -n --arg to build the JSON payload.
    assert "jq -n --arg" in template, \
        f"{flavour} template builds gate payload without jq -n --arg — F8-11 regressed"
