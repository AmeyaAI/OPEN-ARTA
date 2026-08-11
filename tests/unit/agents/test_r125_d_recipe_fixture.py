"""R125.D — DatasetRecipeAgent.design() materialises the fixture parquet at recipe-stage.

Pre-R125.D: `materialise_fixture()` was called only at the test-gen endpoint
(`tests.py:~3506`). If anything between recipe-stage and gen-stage failed
(LLM auth lapse, R49 budget timeout, network blip), pytest at dispatch hit
`FileNotFoundError` on the canonical_path because the parquet was never written.

R125.D moves materialisation UPSTREAM into `DatasetRecipeAgent.design()`,
after the verifier completes + sidecar persisted. The downstream call at
tests.py becomes idempotent (already-exists short-circuit). On materialise
failure, the design path continues (NON-BLOCKING) — the tests.py call
remains as the safety net.
"""
from __future__ import annotations

import inspect

from src.agents import dataset_recipe as dr_mod


class TestR125DRecipeStageMaterialisation:
    """R125.D source-grep tests: the materialise_fixture call must live INSIDE
    DatasetRecipeAgent.design() (after verifier + grounding + sidecar persist).
    Source-grep style mirrors test_analytics_fixture_required.py.
    """

    _SOURCE = inspect.getsource(dr_mod)

    def test_r125d_materialise_call_present_in_design(self):
        """R125.D — DatasetRecipeAgent.design must invoke materialise_fixture."""
        assert "materialise_fixture" in self._SOURCE, (
            "R125.D: materialise_fixture import/call missing from dataset_recipe.py — "
            "recipe-stage fixture write didn't ship"
        )
        # Must reference R125.D marker for traceability + grep
        assert "R125.D" in self._SOURCE, (
            "R125.D: marker comment missing from dataset_recipe.py"
        )

    def test_r125d_materialise_after_persist_sidecar(self):
        """R125.D must run AFTER _persist_sidecar (verifier + grounding warnings
        + sidecar all stamped first; THEN fixture written)."""
        src = self._SOURCE
        persist_idx = src.find("self._persist_sidecar(recipe)")
        materialise_idx = src.find("materialise_fixture(")
        assert persist_idx >= 0, "persist_sidecar call missing"
        assert materialise_idx > persist_idx, (
            "R125.D: materialise_fixture must come AFTER _persist_sidecar — "
            "sidecar is the authoritative recipe contract; parquet generation "
            "should follow"
        )

    def test_r125d_failure_is_non_blocking(self):
        """R125.D materialise failure must NOT block recipe ship.
        Pre-R125.D the tests.py call was the only materialise path; if R125.D
        raises, the safety net at tests.py kicks in. Loud warn, no re-raise.
        """
        src = self._SOURCE
        # Find the R125.D block + assert it has try/except (non-blocking shape)
        r125_d_block_idx = src.find("R125.D")
        assert r125_d_block_idx >= 0
        # Look for try/except within ~30 lines after the R125.D marker
        scope = src[r125_d_block_idx:r125_d_block_idx + 2000]
        assert "try:" in scope and "except" in scope, (
            "R125.D: must wrap materialise_fixture in try/except — "
            "failure must not block recipe return"
        )
        # Must log on failure (operator-visible)
        assert "R125.D: recipe fixture materialise failed" in scope, (
            "R125.D: must surface materialise failures via log.warning so "
            "operator sees the issue but recipe still ships"
        )

    def test_r125d_uses_recipe_model_dump_argument(self):
        """R125.D passes the verified recipe via model_dump() to
        materialise_fixture(recipe=...) — NOT the columns/row_count direct args.
        Ensures the recipe IS the contract (canonical_path, expected_outputs,
        version, row_count all flow from one source of truth).
        """
        src = self._SOURCE
        r125_d_block_idx = src.find("R125.D")
        scope = src[r125_d_block_idx:r125_d_block_idx + 2000]
        assert "recipe.model_dump()" in scope, (
            "R125.D: must call materialise_fixture(recipe=recipe.model_dump()) "
            "so the recipe is the single source of truth for path/columns/rows"
        )
        assert "recipe.requirement_id" in scope, (
            "R125.D: must pass req_id from the recipe (not from caller)"
        )


class TestR125DMaterialiseIsIdempotent:
    """Real call against materialise_fixture to confirm the downstream tests.py
    invocation still works after recipe-stage already ran it. The R125.D plan
    documented that the downstream call becomes idempotent (already-exists
    short-circuit handled by materialise_fixture overwrite semantics)."""

    def test_r125d_materialise_overwrites_cleanly(self, tmp_path, monkeypatch):
        """Second materialise call on the same recipe should not raise."""
        from src.fixtures import generator as gen_mod
        monkeypatch.setattr(gen_mod, "FIXTURES_DIR", tmp_path, raising=False)
        monkeypatch.setattr(gen_mod, "REPO_ROOT", tmp_path, raising=False)

        canonical = "test_recipe_v1_0_0.parquet"
        recipe_dict = {
            "requirement_id": "REQ-TEST-001",
            "version": "1.0.0",
            "canonical_path": canonical,
            "row_count": 50,
            "columns": [
                {"name": "date", "dtype": "string", "distribution": {"shape": "monotonic"}},
                {"name": "revenue", "dtype": "float", "distribution": {"shape": "normal", "mu": 100, "sigma": 10}},
            ],
        }
        # First call — clean materialise
        p1 = gen_mod.materialise_fixture(req_id="REQ-TEST-001", recipe=recipe_dict)
        assert p1.exists(), f"first materialise must produce a file at {p1}"
        # Second call — idempotent overwrite (no exception)
        p2 = gen_mod.materialise_fixture(req_id="REQ-TEST-001", recipe=recipe_dict)
        assert p2.exists()
        assert p1 == p2, "same recipe → same canonical path"
