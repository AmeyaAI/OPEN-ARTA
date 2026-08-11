"""Recipe-stage failure = loud fail-fast with a structured RootCauseReport.

History: pre-R125.E the recipe stage silently logged a warning + continued
(RECIPE_INVALID stub). R125.E made it a hard re-raise. The graceful Phase-6
default then softened it again (proceed via web_app). The Fail-Fast/
Explain-Clearly directive REVERSES that softening: the recipe stage now drives
the RetryLadder (context → evidence → strategy → escalate) and, on exhaustion,
emits a structured RootCauseReport and FAILS LOUDLY (status=
failed_recipe_ungrounded) — never a silent alternate path. The graceful
web_app downgrade survives only as an explicit, default-OFF opt-in
(ARTA_RECIPE_GRACEFUL=1).

Source-grep style (the recipe block is deep inside the 3700+ line
generate_tests function, not unit-testable in isolation).
"""
from __future__ import annotations

import inspect

from src.api.routers import tests as tests_mod

_SOURCE = inspect.getsource(tests_mod)


class TestR125ERecipeLoudFail:

    def test_recipe_uses_retry_ladder(self):
        """Recipe gen must go through the RetryLadder (not a single attempt)."""
        assert 'RetryLadder(' in _SOURCE
        assert 'stage="dataset_recipe"' in _SOURCE

    def test_recipe_logs_error_loud_fail(self):
        """On ladder exhaustion the recipe stage logs at ERROR (operator-visible)."""
        assert "recipe FAIL-FAST after ladder" in _SOURCE, (
            "recipe-stage exhaustion must log a loud ERROR-level FAIL-FAST line"
        )

    def test_recipe_emits_root_cause_report(self):
        """Failure must emit + persist a structured RootCauseReport (5-level)."""
        idx = _SOURCE.find("recipe FAIL-FAST after ladder")
        assert idx >= 0
        # the recipe block builds + persists an RCA
        assert "build_report(" in _SOURCE and "persist_root_cause(" in _SOURCE
        assert 'failure_id=body.requirement_id, stage="dataset_recipe"' in _SOURCE
        # 5-level deep-dive keys present in the recipe RCA
        for level in ("symptom", "immediate_cause", "upstream_cause",
                      "architectural_cause", "process_cause"):
            assert level in _SOURCE, f"RCA deep-dive level '{level}' missing"
        # still stamps the requirement for the gen-health tile (now carries the RCA)
        assert "_r125_e_recipe_failure" in _SOURCE

    def test_recipe_fails_structured_not_silent_continue(self):
        """Default behaviour: a structured loud failure, NOT a silent alternate
        path. The web_app graceful downgrade is an explicit default-OFF opt-in."""
        assert '"status": "failed_recipe_ungrounded"' in _SOURCE, (
            "recipe exhaustion must return a structured failed status, not "
            "silently continue"
        )
        # graceful is opt-in, default OFF (flipped from the prior default '1')
        assert 'os.environ.get("ARTA_RECIPE_GRACEFUL", "0")' in _SOURCE, (
            "ARTA_RECIPE_GRACEFUL must default to '0' (graceful is explicit opt-in)"
        )

    def test_recipe_block_gated_on_analytics(self):
        """Recipe ladder + loud-fail must live inside the analytics gate.

        R212 narrowed the gate to `project_type == "analytics" and not
        _r212_recipe_skip` (also requires the REQUIREMENT to be analytics), so
        match the `if project_type == "analytics"` prefix without the colon —
        the recipe block is still strictly analytics-gated.
        """
        analytics_idx = _SOURCE.find('if project_type == "analytics"')
        ladder_idx = _SOURCE.find('stage="dataset_recipe"')
        assert analytics_idx > 0 and ladder_idx > analytics_idx, (
            "recipe fail-fast block must be inside the "
            '`if project_type == "analytics"` gate'
        )
        # R212 — the gate must also require the requirement to be analytics
        assert "_r212_recipe_skip" in _SOURCE and "is_analytics_requirement(" in _SOURCE, (
            "recipe gate must also check is_analytics_requirement (R212)"
        )
