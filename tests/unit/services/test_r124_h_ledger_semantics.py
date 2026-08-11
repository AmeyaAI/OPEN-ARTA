"""R124.H — improvement_loop ledger key renamed for truthfulness.

Pre-R124.H: `actions["ticket"]` ledger key implied Jira tickets were
created. They weren't — R37.4 fires Jira create ONLY when
JIRA_URL/EMAIL/API_TOKEN are configured. Run-d52a8c showed `ticket: 1951`
on the operator dashboard with Jira disabled → operator confusion.

Post-R124.H: `actions["sut_regression_signaled"]` is the canonical
truthful key. `actions["ticket"]` is preserved as a back-compat alias
(both increment in lockstep) so legacy dashboards don't break.
"""
from __future__ import annotations


def test_r124_h_actions_dict_has_canonical_key():
    """Both `sut_regression_signaled` AND `ticket` are present in actions dict
    init — the canonical key + the back-compat alias."""
    import inspect
    from src.api.services import improvement_loop
    src = inspect.getsource(improvement_loop)
    # Both keys initialized
    assert '"sut_regression_signaled"' in src, (
        "R124.H: canonical key `sut_regression_signaled` must be in actions dict init"
    )
    assert '"ticket"' in src, (
        "R124.H: back-compat alias `ticket` must remain in actions dict init"
    )


def test_r124_h_both_keys_increment_together():
    """Sut_regression branch increments BOTH keys in lockstep (no divergence)."""
    import inspect
    from src.api.services import improvement_loop
    src = inspect.getsource(improvement_loop)
    # Locate the sut_regression branch
    idx = src.find('elif cat == "sut_regression":')
    assert idx > 0, "sut_regression branch must exist"
    branch_src = src[idx:idx + 2000]
    assert 'actions["sut_regression_signaled"] += 1' in branch_src
    assert 'actions["ticket"] += 1' in branch_src


def test_r124_h_ledger_value_truthful():
    """Document the semantic contract: `sut_regression_signaled` reflects
    DETECTION (post-classification count), NOT filed-to-Jira count.

    R37.4 actually fires Jira creates; this ledger field is upstream of
    that (counts the rows R37.4 would file IF Jira were configured)."""
    import inspect
    from src.api.services import improvement_loop
    src = inspect.getsource(improvement_loop)
    # Look for R124.H docstring comment that documents the semantic
    assert "DETECTION not Jira FILING" in src, (
        "R124.H: code-level comment must document the detection-vs-filing semantic"
    )
