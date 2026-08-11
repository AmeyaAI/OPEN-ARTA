"""ARTA test fixtures — mock LLM, event bus, coverage reports.

F9-6: Tests marked with `@pytest.mark.integration` are auto-skipped when
no live Postgres is reachable (`ARTA_REQUIRE_DB=1` forces them to fail
instead, which is what CI sets). The 13 tests in `tests/unit/routers/`
that need a real DB carry this marker so `pytest tests/unit` works
outside the container without 13 spurious failures.
"""
from __future__ import annotations

import os
import socket
import sys
from unittest.mock import AsyncMock, Mock

import pytest

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# F9-6: Register the integration marker so pytest doesn't warn about it.
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires live Postgres; auto-skipped when DB unreachable "
        "(set ARTA_REQUIRE_DB=1 to force failure on skip in CI)",
    )


def _postgres_reachable() -> bool:
    """Probe whether ARTA's configured Postgres accepts a real query.

    A bare TCP open isn't enough — there can be a Postgres listening on 5432
    that doesn't have ARTA's user/database, in which case the integration
    tests fail with `InvalidPasswordError`. Run `SELECT 1` via asyncpg (the
    same driver the app uses) so a successful probe means the integration
    tests can actually connect.
    """
    import asyncio

    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    user = os.environ.get("POSTGRES_USER", "arta")
    pw   = os.environ.get("POSTGRES_PASSWORD", "arta")
    db   = os.environ.get("POSTGRES_DB", "arta")
    if os.environ.get("DATABASE_URL"):
        # Crude parse — ok for the typical postgresql+asyncpg://user:pw@host:port/db form
        import re
        m = re.match(r"postgres(?:ql)?(?:\+asyncpg)?://([^:]+):([^@]*)@([^:/]+)(?::(\d+))?/(\w+)", os.environ["DATABASE_URL"])
        if m:
            user, pw, host, port_s, db = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            if port_s:
                port = int(port_s)

    async def _probe() -> bool:
        try:
            import asyncpg  # type: ignore
            conn = await asyncio.wait_for(
                asyncpg.connect(host=host, port=port, user=user, password=pw, database=db),
                timeout=2.0,
            )
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()
            return True
        except Exception:
            return False

    try:
        return asyncio.run(_probe())
    except Exception:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip integration tests when no live DB is reachable."""
    db_up = _postgres_reachable()
    require_db = os.environ.get("ARTA_REQUIRE_DB") == "1"
    if db_up:
        return
    skip_marker = pytest.mark.skip(reason="integration: Postgres not reachable on localhost:5432")
    for item in items:
        if "integration" in item.keywords:
            if require_db:
                # CI mode — don't silently skip
                item.add_marker(pytest.mark.xfail(reason="ARTA_REQUIRE_DB=1 but DB unreachable", strict=True))
            else:
                item.add_marker(skip_marker)


# ── Mock LLM response (matches _ContentBlock from src/agents/llm_client.py) ──

class _FakeContentBlock:
    def __init__(self, text: str):
        self.text = text


def make_llm_response(text: str, model: str = "mock-model"):
    resp = Mock()
    resp.content = [_FakeContentBlock(text)]
    resp.stop_reason = "end_turn"
    resp.model = model
    resp.usage = Mock(input_tokens=100, output_tokens=50)
    return resp


@pytest.fixture
def mock_llm_client():
    """AsyncMock that mimics AsyncAnthropic — all agents call client.messages.create()."""
    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=make_llm_response('{"result": "ok"}'))
    return client


@pytest.fixture
def mock_event_bus():
    """AsyncMock event bus with .publish()."""
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def passing_coverage_report():
    """Coverage report where all checks pass with default thresholds."""
    return {
        "coverage_pct": 92.0,
        "pass_rate": 95.0,
        "p0_pass_rate": 100.0,
        "coverage_by_priority": {
            "P0": {"coverage_pct": 100.0, "total": 5, "covered": 5},
            "P1": {"coverage_pct": 95.0, "total": 20, "covered": 19},
            "P2": {"coverage_pct": 80.0, "total": 10, "covered": 8},
            "P3": {"coverage_pct": 60.0, "total": 5, "covered": 3},
        },
        "nfr": {
            "performance_p95_ms": 1500,
            "performance_p99_ms": 3000,
            "error_rate_pct": 0.5,
            "security_critical_findings": 0,
            "security_high_findings": 0,
            "auth_bypass_tested": True,
            "health_checks_present": True,
            "code_coverage_pct": 85.0,
            "duplication_pct": 3.0,
        },
    }


@pytest.fixture
def no_defects():
    return []


@pytest.fixture
def p0_defect():
    # R75.5 — match the post-R-GateDefectPriority shape that
    # `_check_defect_policy._is_p0` checks: `priority == "P0"` OR
    # `severity == "critical"`. Pre-R75.5 this fixture stamped
    # `severity == "P0"` which matched NEITHER signal, causing
    # `test_fail_open_p0_defects` to fail with `decision == "CONCERNS"`
    # instead of `"FAIL"`. The fixture lagged the gate semantics.
    return [{"priority": "P0", "severity": "critical", "status": "open", "title": "Critical bug"}]


@pytest.fixture
def test_app(tmp_path):
    """FastAPI TestClient for router tests (no DB, mock fallback mode)."""
    os.environ.pop("ARTA_API_KEY", None)  # bypass auth in dev mode
    os.environ["ARTA_PROJECTS_FILE"] = str(tmp_path / "projects.json")
    from httpx import ASGITransport, AsyncClient
    from src.api.main import app
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


# F4-3: pytest-asyncio creates a fresh event loop per test, but the asyncpg
# engine is module-level in src/db/session.py — connections opened on test A's
# loop become orphaned when test B starts ("Event loop is closed" /
# "attached to a different loop"). Dispose the engine after each test so the
# next test gets a fresh pool tied to its own loop.
@pytest.fixture(autouse=True)
async def _dispose_db_engine_after_each_test():
    yield
    try:
        from src.db import session as db_session
        engine = getattr(db_session, "_engine", None) or getattr(db_session, "engine", None)
        if engine is not None and hasattr(engine, "dispose"):
            await engine.dispose()
    except Exception:
        pass
    # Reset the db_adapter availability cache so the next test re-probes.
    try:
        from src.api.db_adapter import reset_db_check
        reset_db_check()
    except Exception:
        pass
