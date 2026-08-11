"""WS1 — the full-chain writers must be WIRED into the gen + post-run pipelines
(not just defined). Source-asserted so a refactor can't silently unwire them.
Locks in the driver-attr bug fix (app.state.neo4j, NOT .neo4j_driver) that made
the execution-stage writer dead."""
from __future__ import annotations

import inspect

from src.api.routers import execution as _exec
from src.api.routers import tests as _tests


def test_post_run_hook_uses_correct_driver_attr_and_executed_as():
    src = inspect.getsource(_exec._persist_run_to_db)
    # the bug was getattr(app.state, "neo4j_driver") — must be "neo4j"
    assert 'getattr(_app.state, "neo4j", None)' in src
    assert 'neo4j_driver", None)' not in src   # the dead attr is gone
    assert "upsert_run_results" in src
    assert "upsert_spec_execution_edges" in src
    assert "ARTA_TRACE_FULL_CHAIN_DISABLE" in src


def test_gen_pipeline_writes_full_chain():
    src = inspect.getsource(_tests.generate_tests)
    for fn in ("upsert_requirement_profile", "upsert_recipe_chain",
               "upsert_spec_files", "upsert_scenarios"):
        assert fn in src, f"gen pipeline missing {fn}"
    assert "ARTA_TRACE_FULL_CHAIN_DISABLE" in src
    # profile must be written before recipe (HAS_RECIPE attaches to the profile)
    assert src.index("upsert_requirement_profile") < src.index("upsert_recipe_chain")
