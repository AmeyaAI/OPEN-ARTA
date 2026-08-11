"""R313.J (operator-approved) — reground a fabricated server-state getByText
('running') to the SUT's state-column LABEL ('Current State'), verifying the state
field is displayed while the API verifies the value. Precisely gated: fabricated +
state-value + a state-label present in the catalog."""
from __future__ import annotations

from src.agents.grounding_validator import _r313_j_reground_state_getbytext

CATALOG = {"Current State", "State", "Servers", "Name", "Health"}


def test_regrounds_fabricated_state_value_to_label():
    new, n = _r313_j_reground_state_getbytext(
        "await expect(page.getByText('running')).toBeVisible();", CATALOG)
    assert n == 1 and "getByText('State')" in new and "getByText('running')" not in new


def test_leaves_valid_catalog_text():
    spec = "await expect(page.getByText('Servers')).toBeVisible();"
    new, n = _r313_j_reground_state_getbytext(spec, CATALOG)
    assert n == 0 and new == spec


def test_leaves_non_state_fabricated_text():
    # a fabricated text that is NOT a state value must not be touched by R313.J
    spec = "await expect(page.getByText('Frobnicate')).toBeVisible();"
    new, n = _r313_j_reground_state_getbytext(spec, CATALOG)
    assert n == 0


def test_uses_captured_state_domain_values():
    # 'registered' isn't in the static vocab but is in the captured domain
    spec = "await expect(page.getByText('registered')).toBeVisible();"
    new, n = _r313_j_reground_state_getbytext(
        spec, CATALOG, {"currentState": {"registered", "queued"}})
    assert n == 1 and "getByText('State')" in new


def test_noop_without_state_label_in_catalog():
    # no state/status label to reground to → leave the fabricated text (block stays)
    spec = "await expect(page.getByText('running')).toBeVisible();"
    new, n = _r313_j_reground_state_getbytext(spec, {"Servers", "Name"})
    assert n == 0


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_R313_J_REGROUND_DISABLE", "1")
    spec = "await expect(page.getByText('running')).toBeVisible();"
    new, n = _r313_j_reground_state_getbytext(spec, CATALOG)
    assert n == 0


def test_empty_catalog_noop():
    spec = "await expect(page.getByText('running')).toBeVisible();"
    assert _r313_j_reground_state_getbytext(spec, None) == (spec, 0)
