"""R125.G — defensive post-verifier recipe re-validation.

When `recipe_verifier.verify_and_correct` mutates the recipe (closed-loop
correction adjusts trends + expected_outputs in place) AND the except block
stamps verification_strategy/verification_failed directly, the resulting
recipe object can end up in a partial state that Pydantic schema validation
would reject — but no second validation happened, so downstream consumers
(ATDD prompt builder, materialise_fixture) discover the corruption at a
less-traceable point.

R125.G adds a defensive `DatasetRecipe.model_validate(recipe.model_dump())`
AFTER the closed-loop block. If validation fails, stamp a distinct
`verifier_corrupted` strategy + `verification_failed=True` so downstream
consumers see the signal.
"""
from __future__ import annotations

import inspect

from src.agents import dataset_recipe as dr_mod


_SOURCE = inspect.getsource(dr_mod)


class TestR125GPostVerifierRevalidate:

    def test_r125g_marker_present(self):
        """R125.G — marker comment exists for grep + traceability."""
        assert "R125.G" in _SOURCE, "R125.G marker missing from dataset_recipe.py"

    def test_r125g_revalidation_after_closed_loop(self):
        """R125.G must add a defensive model_validate AFTER the closed-loop
        verifier block, BEFORE R55.12 grounding."""
        src = _SOURCE
        # Find the closed-loop block's exit (`verification_failed = True` in
        # the except), then the R125.G block, then the R55.12 grounding section
        closed_loop_end_idx = src.find('recipe.verification_failed = True')
        r125_g_idx = src.find("R125.G")
        r55_12_idx = src.find("R55.12 — open-loop recipe grounding")
        assert closed_loop_end_idx > 0
        assert r125_g_idx > closed_loop_end_idx, (
            "R125.G must come AFTER the closed-loop verifier block"
        )
        assert r125_g_idx < r55_12_idx, (
            "R125.G must come BEFORE R55.12 grounding so corruption is caught "
            "before grounding warnings are stamped on a corrupted recipe"
        )

    def test_r125g_distinct_strategy_value(self):
        """R125.G must use 'verifier_corrupted' as the strategy value so
        operators can distinguish post-verifier corruption from in-verifier
        crash ('verifier_error') in dashboard."""
        assert '"verifier_corrupted"' in _SOURCE, (
            "R125.G: must stamp verification_strategy = 'verifier_corrupted' "
            "(distinct from 'verifier_error' used by closed-loop crash branch)"
        )
        # The pre-existing 'verifier_error' must still be present (closed-loop except)
        assert '"verifier_error"' in _SOURCE, (
            "R125.G must NOT replace the pre-existing 'verifier_error' branch; "
            "verifier_corrupted is a NEW distinct value for post-verifier "
            "corruption detection"
        )

    def test_r125g_validation_failure_is_non_fatal(self):
        """R125.G validation failure must NOT raise — recipe still ships with
        the corruption stamp so downstream can degrade safely."""
        r125_g_idx = _SOURCE.find("R125.G")
        scope = _SOURCE[r125_g_idx:r125_g_idx + 2000]
        # Must have try/except (catches ValidationError gracefully)
        assert "try:" in scope and "except" in scope, (
            "R125.G must wrap model_validate in try/except"
        )
        # Must stamp the corruption marker on failure
        assert "verification_failed = True" in scope, (
            "R125.G failure path must stamp verification_failed = True"
        )
        # Must log at ERROR level (operator-visible) on corruption
        assert "log.error" in scope, (
            "R125.G corruption must log at ERROR (operator dashboard) not WARNING"
        )

    def test_r125g_uses_pydantic_validation_error(self):
        """R125.G catches Pydantic ValidationError specifically (not bare
        Exception). Schema validation errors are distinct from runtime errors;
        catching ValidationError makes the failure mode unambiguous."""
        r125_g_idx = _SOURCE.find("R125.G")
        scope = _SOURCE[r125_g_idx:r125_g_idx + 1500]
        assert "ValidationError" in scope, (
            "R125.G must catch ValidationError specifically — bare Exception "
            "would mask other (genuinely-fatal) issues"
        )
