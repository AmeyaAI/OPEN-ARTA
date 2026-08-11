"""R75.3 — regression test for the AnalyticsResponse + Insight shape.

Pre-R75.3 the `_DefaultAnalyticsClient` stub returned a response with no
`insight` attribute. Generated pytest specs (e.g. req_am_013_e2e.py)
access `response.insight.value`, `response.insight.metric` → AttributeError
on every analytics test, masking the real test correctness issues
underneath.

Post-R75.3 the stub returns `insight=Insight()` so attribute access
works; assertions fail on VALUE comparison (None vs expected), which
is the honest failure mode.

This test locks the shape contract: future contributors can't break it
by removing the `insight` field or changing the dataclass tags.
"""
from __future__ import annotations

import sys
from pathlib import Path

# R75.3 — point sys.path so `arta_runtime` resolves the same way pytest
# does via the project's conftest. Mirrors what
# src/automation/python_tests/conftest.py:14-19 sets up.
_RUNTIME_DIR = Path(__file__).resolve().parents[3] / "src" / "automation" / "python_tests"
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))


def test_default_analytics_client_returns_response_with_insight():
    """The default stub must provide an `.insight` attribute so generated
    tests can access `response.insight.value` without AttributeError."""
    from arta_runtime import analytics_client, Insight, AnalyticsResponse

    response = analytics_client.ask("test query")
    assert isinstance(response, AnalyticsResponse)
    assert response.insight is not None
    assert isinstance(response.insight, Insight)


def test_insight_field_access_does_not_raise():
    """Every attribute the generated tests access on `response.insight`
    must be available (default None) without raising."""
    from arta_runtime import analytics_client

    response = analytics_client.ask("q")
    # All the attributes our generated specs touch
    for attr in ("value", "metric", "schema", "combined",
                 "direction", "magnitude_pct", "source_page",
                 "document_id", "section_id"):
        # Each access must be safe (no AttributeError)
        getattr(response.insight, attr)


def test_insight_lists_default_to_empty():
    """Nested list fields default to empty lists, not None, so
    `verify_from_source(insight, data)` can iterate without crashing."""
    from arta_runtime import Insight

    i = Insight()
    assert i.parsed_claims == []
    assert i.claims == []
    # And the lists are independent across instances
    i2 = Insight()
    i.parsed_claims.append({"x": 1})
    assert i2.parsed_claims == []


def test_stub_default_response_carries_is_stub_marker():
    """Phase L6 — the `_is_stub_default` flag must remain True on stub
    responses so adversarial tests can SKIP cleanly. R75.3 mustn't
    regress this."""
    from arta_runtime import analytics_client

    response = analytics_client.ask("q")
    assert response._is_stub_default is True


def test_insight_is_exported():
    """R75.3 — Insight must be exported so generated specs can
    `from arta_runtime import Insight` for type-annotated fixtures."""
    import arta_runtime
    assert hasattr(arta_runtime, "Insight")
    assert "Insight" in arta_runtime.__all__
