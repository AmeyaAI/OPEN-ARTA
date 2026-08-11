"""F20-32: regression tests for the analytics-test-agent truncation guard.

Three real bugs being protected against:
  - max_tokens=3000 caused frequent mid-line truncation (~26% overrun for the
    5-layer pytest output). Now 8000 with stop_reason="max_tokens" → RuntimeError
    so @retry on _call_llm fires another attempt.
  - _call_llm previously ignored message.stop_reason. A truncated response
    silently passed through, got light-validated, and was written to disk.
  - _validate_pytest_code only checked for pytest signals (import pytest, etc.)
    A 1897-char file truncated mid-line passes those checks (it has the imports,
    has `def test_...`, has `assert`) but is unimportable Python. The new AST
    check catches that and returns "" so the orchestrator skips file write.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.analytics_test_agent import AnalyticsTestAgent


# Sample of the ACTUAL truncation seen in
# src/automation/pytest/analytics/req_am_012_nl_to_query.py (37 lines, ends
# mid-line at `with mock_db_return`).
TRUNCATED_PYTEST = """\
import pytest
from src.automation.pytest.analytics_helpers import (
    frozen_dataset,
    mock_db_returning,
)

@pytest.mark.tier1
@pytest.mark.analytics
def test_nl_to_sql_query_sum_revenue_by_region():
    fixture_path = "fixtures/analytics/req_am_012_dataset_v1_0_0.parquet"
    with frozen_dataset(fixture_path) as data:
        with mock_db_return"""

VALID_PYTEST = """\
import pytest
from src.automation.pytest.analytics_helpers import frozen_dataset

@pytest.mark.tier1
@pytest.mark.analytics
def test_smoke():
    with frozen_dataset("fixtures/analytics/req_am_001.parquet") as data:
        assert data is not None
"""


# ── _validate_pytest_code AST check (F20-32 secondary safety net) ────────

class TestValidatePytestCode:

    async def test_truncated_pytest_returns_empty_via_ast_check(self):
        # The truncated file has all the pytest signals (import pytest, def test_,
        # assert, @pytest.mark) so the existing has_pytest check passes. The new
        # AST check is what catches it.
        out = AnalyticsTestAgent._validate_pytest_code(TRUNCATED_PYTEST, "nl_to_query")
        assert out == ""

    async def test_valid_pytest_passes_ast_check(self):
        out = AnalyticsTestAgent._validate_pytest_code(VALID_PYTEST, "nl_to_query")
        assert out == VALID_PYTEST

    async def test_too_short_returns_empty(self):
        out = AnalyticsTestAgent._validate_pytest_code("def x(): pass", "nl_to_query")
        # 13 chars < 50 floor → reject
        assert out == ""

    async def test_refusal_returns_empty(self):
        refusal = "I cannot generate this test because " + "x" * 100
        out = AnalyticsTestAgent._validate_pytest_code(refusal, "nl_to_query")
        assert out == ""

    async def test_lacks_pytest_signals_returns_empty(self):
        # Long enough, valid Python, but no pytest signals → reject.
        non_pytest = "def hello():\n    return 'world'\n" * 5
        out = AnalyticsTestAgent._validate_pytest_code(non_pytest, "nl_to_query")
        assert out == ""


# ── _call_llm stop_reason check (F20-32 primary truncation detector) ────

class TestCallLLMTruncation:

    @pytest.fixture
    def agent_with_mock_client(self):
        """Build an AnalyticsTestAgent with a mocked LLM client."""
        client = MagicMock()
        client.provider = "test"
        agent = AnalyticsTestAgent(client)
        return agent, client

    async def test_stop_reason_max_tokens_raises_runtimeerror(self, agent_with_mock_client):
        """Truncation signal from the LLM provider must raise RuntimeError so the
        @retry decorator on _call_llm fires another attempt instead of returning
        a half-written file."""
        agent, client = agent_with_mock_client

        async def _create(*args, **kwargs):
            msg = MagicMock()
            msg.content = [MagicMock(text=TRUNCATED_PYTEST)]
            msg.stop_reason = "max_tokens"
            return msg
        client.messages.create = _create

        # Bypass the @retry's auto-retry by inspecting the underlying coroutine.
        # We want to verify the RuntimeError is raised, not that retry silently
        # masks it. The decorator allows 3 attempts; on the 3rd a RuntimeError
        # propagates as-is (reraise=True).
        with pytest.raises(RuntimeError, match="truncated"):
            await agent._call_llm("any prompt")

    async def test_normal_stop_reason_returns_text(self, agent_with_mock_client):
        agent, client = agent_with_mock_client

        async def _create(*args, **kwargs):
            msg = MagicMock()
            msg.content = [MagicMock(text=VALID_PYTEST)]
            msg.stop_reason = "end_turn"
            return msg
        client.messages.create = _create

        result = await agent._call_llm("any prompt")
        assert result == VALID_PYTEST

    async def test_missing_stop_reason_returns_text(self, agent_with_mock_client):
        """If the provider omits stop_reason entirely (e.g. early Ollama versions),
        we don't raise — just return what we got. The AST check downstream catches
        the case where the output is broken."""
        agent, client = agent_with_mock_client

        async def _create(*args, **kwargs):
            msg = MagicMock()
            msg.content = [MagicMock(text=VALID_PYTEST)]
            # No stop_reason attribute set
            del msg.stop_reason
            return msg
        client.messages.create = _create

        result = await agent._call_llm("any prompt")
        assert result == VALID_PYTEST


# ── max_tokens budget check (F20-32 #1) ──────────────────────────────────

class TestMaxTokensBudget:

    async def test_max_tokens_is_8000_not_3000(self):
        """Read source to verify the bump shipped."""
        import inspect
        from src.agents import analytics_test_agent
        source = inspect.getsource(analytics_test_agent.AnalyticsTestAgent._call_llm)
        assert "max_tokens=8000" in source, (
            "F20-32: max_tokens must be 8000 (was 3000). "
            "5-layer pytest output reliably overran 3000."
        )
        assert "max_tokens=3000" not in source, (
            "F20-32: stale max_tokens=3000 must be removed."
        )
