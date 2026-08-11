"""R77.6.γ — dispatcher↔consumer env-var contract consistency tests.

These tests catch drift in the variable harness:
  - A test consumer references an env var the dispatcher never sets
    → tool fails with "value undefined" / 401 / URL malformed
  - The dispatcher sets a var nothing reads → dead setter

Both classes of drift are silent in production. By failing CI loudly,
this test forces contributors to update
``src/shared/env_var_contract.py`` when adding a new TARGET_* or a new
tool dispatcher.
"""
from __future__ import annotations

import pytest

from src.shared.env_var_contract import (
    DISPATCHER_REQUIRED,
    DISPATCHER_CONDITIONAL,
    CONSUMER_READS,
    INTERNAL_USE_ONLY,
    OPERATOR_SET_ONLY,
)


def test_every_consumer_reader_has_a_setter():
    """Each env var any consumer reads must EITHER be set by the
    dispatcher (REQUIRED or CONDITIONAL) OR be explicitly delegated to
    operator-level config (OPERATOR_SET_ONLY). Closes R73's 'no
    central registry' gap.
    """
    accounted_for = (
        DISPATCHER_REQUIRED
        | set(DISPATCHER_CONDITIONAL.keys())
        | OPERATOR_SET_ONLY
    )
    for tool, reads in CONSUMER_READS.items():
        missing = reads - accounted_for
        assert not missing, (
            f"R77.6.γ: tool {tool!r} reads env var(s) {missing!r} but "
            f"the dispatcher never sets them and they're not registered "
            f"as OPERATOR_SET_ONLY. Either add the missing setter to "
            f"src/api/routers/execution.py + register it in "
            f"src/shared/env_var_contract.py, mark OPERATOR_SET_ONLY "
            f"with a comment explaining the delegation, OR if the read "
            f"was wrong, remove the reader."
        )


def test_no_orphaned_dispatcher_vars():
    """Every var the dispatcher sets should be read by at least one
    consumer (or marked INTERNAL_USE_ONLY). Catches dead setters that
    accumulate as a tool stops using a var but the dispatcher keeps
    writing it.
    """
    all_reads: set[str] = set()
    for reads in CONSUMER_READS.values():
        all_reads.update(reads)
    all_set = DISPATCHER_REQUIRED | set(DISPATCHER_CONDITIONAL.keys())
    orphans = all_set - all_reads - INTERNAL_USE_ONLY
    assert not orphans, (
        f"R77.6.γ: dispatcher sets env var(s) {orphans!r} but no "
        f"registered consumer reads them. Either register the reader "
        f"in CONSUMER_READS, mark INTERNAL_USE_ONLY, or remove the "
        f"setter from execution.py."
    )


def test_required_and_conditional_are_disjoint():
    """A var must be either always-set OR conditionally-set, not both —
    avoids ambiguity about whether it's safe to assume presence."""
    overlap = DISPATCHER_REQUIRED & set(DISPATCHER_CONDITIONAL.keys())
    assert not overlap, (
        f"R77.6.γ: vars {overlap!r} appear in BOTH "
        f"DISPATCHER_REQUIRED and DISPATCHER_CONDITIONAL. Pick one — "
        f"required means always set; conditional means depends on auth "
        f"method/suite/mode."
    )


def test_target_auth_localstorage_is_registered():
    """R77.6.α specifically — TARGET_AUTH_LOCALSTORAGE was the keystone
    missing setter. Verify it's now registered as a setter AND a reader.
    Belt-and-braces: pre-R77.6.α auth-setup.ts read it but execution.py
    only set it from projects.json (empty when paste flow uses the
    storage-state file). R77.6.α fixed the setter; this test prevents
    silent drift if someone removes either side.
    """
    assert "TARGET_AUTH_LOCALSTORAGE" in DISPATCHER_CONDITIONAL, (
        "TARGET_AUTH_LOCALSTORAGE missing from DISPATCHER_CONDITIONAL"
    )
    assert "TARGET_AUTH_LOCALSTORAGE" in CONSUMER_READS["playwright"], (
        "TARGET_AUTH_LOCALSTORAGE missing from playwright consumer reads"
    )


def test_a11y_report_path_registered_for_axe():
    """Axe specs throw if A11Y_REPORT_PATH is unset — verify the
    contract reflects this hard dependency."""
    assert "A11Y_REPORT_PATH" in DISPATCHER_CONDITIONAL
    assert "A11Y_REPORT_PATH" in CONSUMER_READS["axe"]
