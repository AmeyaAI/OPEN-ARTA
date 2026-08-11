"""R74.4 — unit tests for the shared R43 env-var patterns module.

This module is the single source of truth for the predicate AND
the value-producer that R43 (in `execution.py`) and R72.2 (in
`grounding_validator.py`) both depend on. Pre-R74.4 the patterns
were duplicated; this test suite locks the canonical behavior so
future contributors can't add a pattern to one site without the
other.
"""
from __future__ import annotations

import pytest

from src.shared.env_var_patterns import (
    is_r43_substitutable_name,
    resolve_r43_synthetic_value,
    GENERIC_CONFIG_NAMES,
)


@pytest.mark.parametrize("name,expected", [
    # *_id patterns
    ("user_id", True), ("subscription_id", True), ("baseline_id", True),
    ("uuid", True), ("user_uuid", True),
    # *_name patterns
    ("project_name", True), ("name", True),
    # *_count patterns
    ("item_count", True), ("count", True),
    # *_path patterns (R48.2)
    ("folder_path", True), ("path", True),
    # *_type patterns (R48.2)
    ("data_type", True), ("type", True), ("entry_type", True),
    # generic config names (R48.2)
    ("context", True), ("scope", True), ("tenant", True), ("namespace", True),
    # *_version patterns (R71.3)
    ("version", True), ("api_version", True), ("subscription_version", True),
    # *cookie* (R48.1)
    ("cookie_value", True), ("cookie0_value", True), ("session_cookie", True),
    # NOT substitutable — auth-only
    ("auth_token", False), ("bearer_token", False), ("jwt_token", False),
    # NOT substitutable — novel hallucinations
    ("apiKey", False), ("region", False), ("env_label", False),
    # Edge cases
    ("", False),
])
def test_is_r43_substitutable_name(name: str, expected: bool) -> None:
    """Comprehensive coverage of the predicate's pattern table."""
    assert is_r43_substitutable_name(name) is expected, (
        f"Expected is_r43_substitutable_name({name!r}) = {expected}"
    )


def test_is_r43_substitutable_name_handles_non_string() -> None:
    """Defensive: non-string inputs return False without raising."""
    assert is_r43_substitutable_name(None) is False  # type: ignore[arg-type]
    assert is_r43_substitutable_name(123) is False   # type: ignore[arg-type]


def test_resolve_synthetic_id_is_deterministic_uuid() -> None:
    """Same var name always produces the same UUID — tests in the same
    run can reference the same id consistently across SETUP / VERIFY."""
    v1 = resolve_r43_synthetic_value("baseline_id")
    v2 = resolve_r43_synthetic_value("baseline_id")
    assert v1 == v2
    assert v1 is not None
    assert len(v1) == 36  # UUID shape
    assert v1.count("-") == 4
    # Different var → different UUID
    assert resolve_r43_synthetic_value("subscription_id") != v1


def test_resolve_synthetic_value_per_pattern() -> None:
    """Each pattern produces the documented synthetic value."""
    assert resolve_r43_synthetic_value("version") == "v1"
    assert resolve_r43_synthetic_value("api_version") == "v1"
    assert resolve_r43_synthetic_value("project_name") == "arta-synthetic-project_name"
    assert resolve_r43_synthetic_value("item_count") == "1"
    assert resolve_r43_synthetic_value("folder_path") == "/arta-synthetic/path"
    assert resolve_r43_synthetic_value("data_type") == "default"
    assert resolve_r43_synthetic_value("context") == "default"
    assert resolve_r43_synthetic_value("tenant") == "default"


def test_resolve_returns_none_for_cookie_and_auth() -> None:
    """Cookie aliases return None (filled at paste via R48.1, not
    dispatch). Auth tokens return None (no useful synthetic)."""
    # R48.1 paste path
    assert resolve_r43_synthetic_value("cookie_value") is None
    assert resolve_r43_synthetic_value("session_cookie") is None
    # Auth-only
    assert resolve_r43_synthetic_value("auth_token") is None
    assert resolve_r43_synthetic_value("apiKey") is None


def test_predicate_and_resolver_consistent_for_substitutable_at_dispatch() -> None:
    """For names that are R43-substitutable AT DISPATCH (not cookies),
    the predicate AND the value-resolver must agree: predicate True ⇒
    resolver returns non-None."""
    SUBSTITUTABLE_AT_DISPATCH = [
        "user_id", "version", "folder_path", "data_type", "context",
        "project_name", "item_count",
    ]
    for name in SUBSTITUTABLE_AT_DISPATCH:
        assert is_r43_substitutable_name(name)
        assert resolve_r43_synthetic_value(name) is not None, (
            f"Predicate says {name} is substitutable but resolver returned None"
        )


def test_generic_config_names_constant() -> None:
    """The frozenset constant is the canonical generic-config name set."""
    assert "context" in GENERIC_CONFIG_NAMES
    assert "scope" in GENERIC_CONFIG_NAMES
    assert "tenant" in GENERIC_CONFIG_NAMES
    assert "namespace" in GENERIC_CONFIG_NAMES
    # Should be immutable
    with pytest.raises((AttributeError, TypeError)):
        GENERIC_CONFIG_NAMES.add("x")  # type: ignore[attr-defined]
