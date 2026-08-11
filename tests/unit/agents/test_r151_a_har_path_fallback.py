"""R151.A KEYSTONE — discovery_executor.execute workflow_id → project_id fallback chain.

Pre-R151.A: line 321 read `workflow_id = str(getattr(ctx, "workflow_id", "no_id"))`.
When `ctx.workflow_id is None` (attr present but None), `getattr` returned None →
`str(None) == "None"` → HAR landed at `.arta/discovery/None/discovery.har`.
Fresh probes never updated `discovered_endpoints/<pid>.json` for non-orchestrator
callers (e.g., `Ctx().workflow_id = None`).

Post-R151.A: 3-level fallback chain
    str(getattr(ctx, "workflow_id", None) or (project or {}).get("id") or "no_id")

R90.2 synthetic-ctx pattern (projects.py:3165-3169) continues to work — it sets a
real string for ctx.workflow_id and still wins. The fix only activates when the
ctx.workflow_id is falsy.
"""
from __future__ import annotations

import inspect
import re

import src.agents.discovery_executor as discovery_executor


# ── Source-level guard: the fallback chain shape ───────────────────────────


def test_r151_a_fallback_chain_present_in_source():
    """The source of discovery_executor.execute MUST contain the
    3-level fallback chain. Regression guard against accidental revert
    to `getattr(ctx, "workflow_id", "no_id")` (the buggy form).
    """
    src = inspect.getsource(discovery_executor.execute)
    # The fallback chain: getattr(..., None) or project["id"] or "no_id"
    assert "getattr(ctx, \"workflow_id\", None)" in src, (
        "R151.A: 3-level fallback requires `getattr(ctx, 'workflow_id', None)`"
    )
    # Project["id"] fallback present
    assert re.search(r"\(project or \{\}\)\.get\(\"id\"\)", src), (
        "R151.A: project[id] fallback link missing in workflow_id derivation"
    )
    # Final legacy sentinel preserved
    assert "\"no_id\"" in src
    # Buggy form ABSENT as actual CODE (it's still referenced in the
    # R151.A doc comment as the prior buggy form). The check: the only
    # remaining occurrence must be inside a comment line (prefixed by `#`).
    # Count non-comment occurrences — should be 0.
    non_comment_occurrences = 0
    for line in src.splitlines():
        # Strip whitespace + check if line starts with `#` (Python comment)
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if 'getattr(ctx, "workflow_id", "no_id")' in line:
            non_comment_occurrences += 1
    assert non_comment_occurrences == 0, (
        f"R151.A: buggy direct-default form must NOT be in actual code; "
        f"found {non_comment_occurrences} non-comment occurrence(s)"
    )


def test_r151_a_fallback_chain_uses_or_not_get_default():
    """The fallback chain uses `or` for None-coalescing, NOT `getattr`
    default — because the attr exists but is None for the buggy caller.
    """
    src = inspect.getsource(discovery_executor.execute)
    # `or (project or {}).get("id")` literal
    assert "or (project or {}).get(\"id\")" in src, (
        "R151.A: fallback must use `or` (None-coalesces None values from attr)"
    )


# ── Behavioral check: the fallback resolves correctly ──────────────────────


def _resolve_workflow_id_simulating_r151a(ctx, project: dict) -> str:
    """Reproduce R151.A's fallback chain. We can't run the full execute()
    in a unit test (it spawns playwright), but the resolution logic is
    self-contained — verify it produces the right ID for each ctx/project
    combination.
    """
    return str(
        getattr(ctx, "workflow_id", None)
        or (project or {}).get("id")
        or "no_id"
    )


class _CtxWithWorkflowId:
    def __init__(self, wf_id):
        self.workflow_id = wf_id


def test_r151_a_explicit_workflow_id_wins():
    """Orchestrator-style ctx (workflow_id = real string) WINS over
    project["id"]. Backward compat for R90.2 synthetic-ctx path."""
    ctx = _CtxWithWorkflowId("wf-orchestrator-abc")
    project = {"id": "pid-different"}
    assert _resolve_workflow_id_simulating_r151a(ctx, project) == "wf-orchestrator-abc"


def test_r151_a_none_workflow_id_falls_back_to_project_id():
    """The R151.A core fix: when ctx.workflow_id IS None (attr exists,
    value is None — the original bug shape), fall back to project["id"].
    HAR path lands at canonical project-scoped location."""
    ctx = _CtxWithWorkflowId(None)
    project = {"id": "pid-fallback-canonical"}
    assert _resolve_workflow_id_simulating_r151a(ctx, project) == "pid-fallback-canonical"


def test_r151_a_missing_both_falls_back_to_no_id():
    """When ctx.workflow_id is None AND project has no id (cold-start
    unit test scenarios), fall back to legacy 'no_id' sentinel. No
    regression — the original default is preserved for this edge case.
    """
    ctx = _CtxWithWorkflowId(None)
    project = {}
    assert _resolve_workflow_id_simulating_r151a(ctx, project) == "no_id"


def test_r151_a_empty_string_workflow_id_falls_back():
    """Empty string is also falsy → should fall back to project_id.
    Defensive against operators passing literal "" as workflow_id."""
    ctx = _CtxWithWorkflowId("")
    project = {"id": "pid-empty-fallback"}
    assert _resolve_workflow_id_simulating_r151a(ctx, project) == "pid-empty-fallback"


def test_r151_a_no_workflow_id_attribute_falls_back():
    """When ctx has NO workflow_id attribute at all (truly bare Ctx),
    getattr returns None → fall back chain proceeds. Mirrors the
    original `getattr(..., "no_id")` legacy code path for missing attrs.
    """
    class _BareCtx:
        pass
    ctx = _BareCtx()
    project = {"id": "pid-bare-ctx"}
    assert _resolve_workflow_id_simulating_r151a(ctx, project) == "pid-bare-ctx"
