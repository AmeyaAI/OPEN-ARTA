"""F9-7: Cover the F8-6 EvidenceCollectorAgent — including the F9-4 type guard."""
from __future__ import annotations

import pytest

from src.agents.evidence_collector import EvidenceCollectorAgent


@pytest.fixture
def agent():
    return EvidenceCollectorAgent()


class TestEvidenceCollector:

    async def test_collect_extracts_scalar_artifacts(self, agent):
        results = [{
            "run_id": "r1",
            "screenshot": "/x/foo.png",
            "har_file":   "/x/foo.har",
        }]
        out = await agent.collect(results)
        kinds = sorted(e["kind"] for e in out)
        paths = sorted(e["path"] for e in out)
        assert kinds == ["har", "screenshot"]
        assert paths == ["/x/foo.har", "/x/foo.png"]
        assert all(e["run_id"] == "r1" for e in out)

    async def test_collect_handles_list_paths(self, agent):
        results = [{
            "run_id": "r2",
            "video":  ["/x/v1.webm", "/x/v2.webm"],
        }]
        out = await agent.collect(results)
        assert len(out) == 2
        assert {e["path"] for e in out} == {"/x/v1.webm", "/x/v2.webm"}
        assert all(e["kind"] == "video" for e in out)

    async def test_collect_skips_absent_keys(self, agent):
        # Keys with None / "" values should be skipped, not emitted as evidence
        results = [{"run_id": "r3", "screenshot": None, "har_file": "", "video": []}]
        out = await agent.collect(results)
        assert out == []

    async def test_collect_handles_empty_results(self, agent):
        assert await agent.collect([]) == []
        assert await agent.collect(None) == []  # type: ignore[arg-type]

    async def test_collect_uses_test_id_when_run_id_absent(self, agent):
        # Falls back to test_id when run_id isn't on the result
        results = [{"test_id": "TC-1", "screenshot": "/x/s.png"}]
        out = await agent.collect(results)
        assert out[0]["run_id"] == "TC-1"

    async def test_collect_skips_non_dict_results_without_raising(self, agent):
        # F9-4 regression: stray non-dict used to raise AttributeError on .get()
        results = [None, "stray-string", 42, {"screenshot": "/x/s.png"}]
        out = await agent.collect(results)  # type: ignore[arg-type]
        assert len(out) == 1
        assert out[0]["path"] == "/x/s.png"
