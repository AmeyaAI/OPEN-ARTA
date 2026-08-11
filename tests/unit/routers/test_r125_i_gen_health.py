"""R125.I — gen-health dashboard endpoint per project.

Per the user directive: "ARTA performance and accuracy should be same
irrespective whether I choose claude code cli or ollama."

R125.I is THE comparison surface. Operator can verify quality side-by-side
across providers before/after R125.H provider switch. Reads R125.K
`_gen_metrics.llm` stamps from every test row + aggregates per-provider
counts, models, strategies, gen_source distribution, failed-req drill-down,
and R125.M strategy_divergence counts.

Unit tests directly call `project_gen_health()` with a mocked
GENERATED_TESTS list — no FastAPI integration overhead.
"""
from __future__ import annotations

import pytest

from src.api.routers.projects import project_gen_health


def _make_test_row(
    project_id: str,
    req_id: str,
    tool: str = "playwright",
    gen_source: str = "llm",
    provider: str = "claude_code",
    model: str = "claude-sonnet-4-6",
    strategy: str = "batch",
    generation_failure: str | None = None,
) -> dict:
    """Construct a GENERATED_TESTS row matching the shape from tests.py."""
    return {
        "id": f"TC-{req_id.replace('REQ-', '')}-01",
        "project_id": project_id,
        "requirement_id": req_id,
        "tool": tool,
        "automation_tool": tool,
        "generation_source": gen_source,
        "generation_failure": generation_failure,
        "_gen_metrics": {
            "llm": {"provider": provider, "model": model, "strategy": strategy},
            "gen_source": gen_source,
        },
    }


@pytest.fixture
def patched_generated_tests(monkeypatch):
    """Patch GENERATED_TESTS module-level list with a known set."""
    from src.api.routers import tests as tests_mod
    # Save original + clear so we can set our own
    original = list(tests_mod.GENERATED_TESTS)
    tests_mod.GENERATED_TESTS.clear()
    yield tests_mod.GENERATED_TESTS
    # Restore
    tests_mod.GENERATED_TESTS.clear()
    tests_mod.GENERATED_TESTS.extend(original)


@pytest.fixture
def stub_resolve_project(monkeypatch):
    """Stub `_resolve_project` to return a known project."""
    async def _stub(project_id: str):
        return {
            "name": f"Test project {project_id}",
            "coverage_pct": 0,
            "open_defects": 0,
            "test_count": 0,
            "last_run_status": "unknown",
        }
    from src.api.routers import projects as projects_mod
    monkeypatch.setattr(projects_mod, "_resolve_project", _stub)


@pytest.mark.asyncio
async def test_r125i_returns_per_provider_breakdown(patched_generated_tests, stub_resolve_project):
    """Tests on Claude vs Ollama → per-provider counts visible."""
    pid = "test-project-001"
    # 3 Claude tests + 2 Ollama tests, all LLM-generated
    patched_generated_tests.extend([
        _make_test_row(pid, "REQ-001", provider="claude_code", model="claude-sonnet-4-6"),
        _make_test_row(pid, "REQ-002", provider="claude_code", model="claude-sonnet-4-6"),
        _make_test_row(pid, "REQ-003", provider="claude_code", model="claude-haiku-4-5-20251001"),
        _make_test_row(pid, "REQ-004", provider="ollama", model="arta-qwen-pro:latest", strategy="sequential"),
        _make_test_row(pid, "REQ-005", provider="ollama", model="arta-qwen-pro:latest", strategy="sequential"),
    ])
    result = await project_gen_health(project_id=pid)
    assert result["total_tests"] == 5
    assert "claude_code" in result["by_provider"]
    assert "ollama" in result["by_provider"]
    assert result["by_provider"]["claude_code"]["count"] == 3
    assert result["by_provider"]["ollama"]["count"] == 2
    # Models surface for each provider
    assert "claude-sonnet-4-6" in result["by_provider"]["claude_code"]["models"]
    assert "claude-haiku-4-5-20251001" in result["by_provider"]["claude_code"]["models"]
    assert "arta-qwen-pro:latest" in result["by_provider"]["ollama"]["models"]
    # Strategy surfaces
    assert "batch" in result["by_provider"]["claude_code"]["strategies"]
    assert "sequential" in result["by_provider"]["ollama"]["strategies"]


@pytest.mark.asyncio
async def test_r125i_failed_reqs_surfaced(patched_generated_tests, stub_resolve_project):
    """Tests with gen_source=failed OR generation_failure stamped surface in
    failed_reqs drilldown."""
    pid = "test-project-002"
    patched_generated_tests.extend([
        _make_test_row(pid, "REQ-OK", gen_source="llm"),
        _make_test_row(pid, "REQ-FAIL-A", gen_source="failed", generation_failure="LLM timeout"),
        _make_test_row(pid, "REQ-FAIL-B", gen_source="llm",
                       generation_failure="r125_m_strategy_divergence: sequential strategy didn't produce k6"),
    ])
    result = await project_gen_health(project_id=pid)
    assert result["failed_req_count"] == 2
    failed_ids = {r["requirement_id"] for r in result["failed_reqs"]}
    assert "REQ-FAIL-A" in failed_ids
    assert "REQ-FAIL-B" in failed_ids
    assert "REQ-OK" not in failed_ids


@pytest.mark.asyncio
async def test_r125i_strategy_divergence_count(patched_generated_tests, stub_resolve_project):
    """R125.M divergence stamps surface as a counter on the gen-health tile."""
    pid = "test-project-003"
    patched_generated_tests.extend([
        _make_test_row(pid, "REQ-DIV-1", generation_failure="r125_m_strategy_divergence: missing k6"),
        _make_test_row(pid, "REQ-DIV-2", generation_failure="r125_m_strategy_divergence: missing newman"),
        _make_test_row(pid, "REQ-OK"),  # not a divergence
    ])
    result = await project_gen_health(project_id=pid)
    assert result["strategy_divergence_count"] == 2


@pytest.mark.asyncio
async def test_r125i_other_projects_excluded(patched_generated_tests, stub_resolve_project):
    """gen-health for project A must not include rows from project B."""
    patched_generated_tests.extend([
        _make_test_row("project-a", "REQ-001"),
        _make_test_row("project-a", "REQ-002"),
        _make_test_row("project-b", "REQ-100"),
        _make_test_row("project-c", "REQ-200"),
    ])
    result = await project_gen_health(project_id="project-a")
    assert result["total_tests"] == 2


@pytest.mark.asyncio
async def test_r125i_empty_project_returns_zeros(patched_generated_tests, stub_resolve_project):
    """Project with no tests yet → zero counters, no errors."""
    result = await project_gen_health(project_id="empty-project")
    assert result["total_tests"] == 0
    assert result["by_provider"] == {}
    assert result["by_gen_source"] == {}
    assert result["failed_req_count"] == 0


@pytest.mark.asyncio
async def test_r125i_unknown_provider_handled_gracefully(patched_generated_tests, stub_resolve_project):
    """Tests missing _gen_metrics (legacy pre-R125.K rows) bucket as 'unknown'."""
    pid = "test-project-legacy"
    patched_generated_tests.extend([
        # Row without _gen_metrics — legacy pre-R125.K
        {
            "id": "TC-LEG-01",
            "project_id": pid,
            "requirement_id": "REQ-LEG",
            "tool": "playwright",
            "automation_tool": "playwright",
            "generation_source": "llm",
        },
    ])
    result = await project_gen_health(project_id=pid)
    assert "unknown" in result["by_provider"]
    assert result["by_provider"]["unknown"]["count"] == 1
