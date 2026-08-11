"""F20-31: regression tests for the _run_pytest_analytics JSON-report parser.

Five real bugs being protected against — each surfaced by run-b96ce7 where the
Analytics column showed 122 fail rows with completely empty error_message text:

  Bug 1: `if "tests" in json_report:` skipped collection failures because
         pytest-json-report writes `tests: []` (key present, list empty) on
         collection-time errors. Failure detail in `collectors[*].longrepr`
         was silently dropped.
  Bug 2: Only `t.get("call", {}).get("longrepr")` was read — setup/teardown
         longrepr was lost. When `frozen_dataset(missing_path)` raised in
         setup, the FAIL row had empty error_message.
  Bug 3: The fallback path captured only stderr. Pytest with `-q` writes most
         output to stdout (collection summary, --tb=short tracebacks).
  Bug 4: `outcome` defaulted to `"failed"` silently — schema drift would
         silently corrupt counts.
  Bug 5: `nodeid.split('::')[-1]` discarded class names. TestX::test_a and
         TestY::test_a collided on `test_a` after split.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# Import the module to call the runner directly with patched subprocess + report file.
from src.api.routers import execution as exec_mod


@pytest.fixture(autouse=True)
def _isolate_real_results():
    """Each test gets a fresh _REAL_RESULTS dict so they don't bleed."""
    snapshot = dict(exec_mod._REAL_RESULTS)
    exec_mod._REAL_RESULTS.clear()
    yield
    exec_mod._REAL_RESULTS.clear()
    exec_mod._REAL_RESULTS.update(snapshot)


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


async def _invoke_runner(tmp_path: Path, report_payload: dict | None,
                          stdout: bytes = b"", stderr: bytes = b"",
                          returncode: int = 1):
    """Invoke _run_pytest_analytics with a single fake spec file + canned
    subprocess result + canned JSON report. Returns the resulting _REAL_RESULTS
    rows for run_id="r1".

    The runner discovers `*.py` files in pytest_dir; create one stub spec.
    """
    spec = tmp_path / "req_am_001_smoke.py"
    spec.write_text("# stub")

    # Compute the report file path the runner uses for this spec.
    # _run_pytest_analytics writes it to /tmp/arta-pytest-<run_id>-<spec.stem>.json
    report_path = Path(f"/tmp/arta-pytest-r1-{spec.stem}.json")
    if report_payload is not None:
        import json as _json
        report_path.write_text(_json.dumps(report_payload))
    elif report_path.exists():
        report_path.unlink()

    proc = _make_proc(returncode=returncode, stdout=stdout, stderr=stderr)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        await exec_mod._run_pytest_analytics(
            run_id="r1", build_id="b1", pytest_dir=tmp_path,
            test_env={}, project_prefix="",
        )
    return exec_mod._REAL_RESULTS.get("r1", [])


# ── Bug 1: collection failures must surface ────────────────────────────────

class TestCollectionFailures:

    async def test_collection_failure_surfaces_with_longrepr(self, tmp_path):
        """When pytest fails at collection (e.g. SyntaxError in test file), the
        JSON report has `tests: []` plus `collectors[*].longrepr`. Previously
        this was silently dropped because the code entered the empty `tests`
        loop and wrote nothing."""
        report = {
            "tests": [],
            "collectors": [{
                "outcome": "failed",
                "nodeid": "req_am_001_smoke.py",
                "longrepr": "SyntaxError: '(' was never closed at line 37",
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert len(rows) >= 1, "Collection failure should produce a FAIL row"
        coll_rows = [r for r in rows if "collect" in r["test_id"]]
        assert len(coll_rows) == 1
        assert coll_rows[0]["status"] == "FAIL"
        assert "SyntaxError" in coll_rows[0]["error_message"]
        assert coll_rows[0]["automation_tool"] == "pytest"

    async def test_multiple_collection_failures_each_surface(self, tmp_path):
        report = {
            "tests": [],
            "collectors": [
                {"outcome": "failed", "nodeid": "f1.py", "longrepr": "ImportError x"},
                {"outcome": "failed", "nodeid": "f2.py", "longrepr": "ImportError y"},
                {"outcome": "passed", "nodeid": "f3.py"},  # ignored
            ],
        }
        rows = await _invoke_runner(tmp_path, report)
        coll_rows = [r for r in rows if "collect" in r["test_id"]]
        assert len(coll_rows) == 2
        assert any("ImportError x" in r["error_message"] for r in coll_rows)
        assert any("ImportError y" in r["error_message"] for r in coll_rows)


# ── Bug 2: longrepr from setup phase must surface ─────────────────────────

class TestPhaseSpecificLongrepr:

    async def test_setup_failure_surfaces_longrepr(self, tmp_path):
        """When a fixture (e.g. frozen_dataset) raises in setup, `call` is
        missing entirely. Previously `t.get("call", {}).get("longrepr")` returned
        None → empty error_message for what's actually a clear FileNotFoundError."""
        report = {
            "tests": [{
                "nodeid": "req_am_001_smoke.py::test_x",
                "outcome": "failed",
                "duration": 0.05,
                "setup": {
                    "outcome": "failed",
                    "longrepr": "FileNotFoundError: fixtures/analytics/req_am_001.parquet",
                },
                # NO "call" key
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert len(rows) == 1
        assert rows[0]["status"] == "FAIL"
        assert "FileNotFoundError" in rows[0]["error_message"]

    async def test_call_failure_still_works(self, tmp_path):
        """Existing behaviour preserved for the common case."""
        report = {
            "tests": [{
                "nodeid": "f.py::test_x",
                "outcome": "failed",
                "duration": 0.1,
                "call": {"outcome": "failed", "longrepr": "AssertionError: 5 != 3"},
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert "AssertionError" in rows[0]["error_message"]

    async def test_teardown_failure_surfaces(self, tmp_path):
        report = {
            "tests": [{
                "nodeid": "f.py::test_x",
                "outcome": "failed",
                "duration": 0.1,
                "teardown": {"outcome": "failed", "longrepr": "TeardownError x"},
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert "TeardownError" in rows[0]["error_message"]

    async def test_passed_test_has_empty_error_message(self, tmp_path):
        report = {
            "tests": [{
                "nodeid": "f.py::test_x", "outcome": "passed", "duration": 0.1,
                "call": {"outcome": "passed"},
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert rows[0]["status"] == "PASS"
        assert rows[0]["error_message"] == ""

    async def test_skipped_test_status(self, tmp_path):
        report = {
            "tests": [{
                "nodeid": "f.py::test_x", "outcome": "skipped", "duration": 0.0,
                "call": {"outcome": "skipped"},
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert rows[0]["status"] == "SKIP"


# ── Bug 3: fallback path captures stdout AND stderr ──────────────────────

class TestFallbackOutputCapture:

    async def test_fallback_uses_combined_output_when_no_json_report(self, tmp_path):
        """When pytest crashes before writing a JSON report (e.g. unknown CLI arg
        in the F20-21 pre-fix world), the fallback path must capture stdout —
        because pytest writes most diagnostic output there with `-q`."""
        rows = await _invoke_runner(
            tmp_path, report_payload=None,
            stdout=b"E   AssertionError: expected 5 got 3\n_______________ test_x _______________\n",
            stderr=b"",  # empty stderr
            returncode=1,
        )
        assert len(rows) == 1
        assert rows[0]["status"] == "FAIL"
        assert "AssertionError" in rows[0]["error_message"]

    async def test_fallback_uses_stderr_when_only_stderr(self, tmp_path):
        rows = await _invoke_runner(
            tmp_path, report_payload=None,
            stdout=b"",
            stderr=b"FATAL: pytest crashed\n",
            returncode=2,
        )
        assert "FATAL: pytest crashed" in rows[0]["error_message"]


# ── Bug 4: missing outcome handled explicitly ────────────────────────────

class TestUnknownOutcome:

    async def test_missing_outcome_marked_as_fail_with_explicit_message(self, tmp_path):
        report = {
            "tests": [{
                "nodeid": "f.py::test_x",
                # no "outcome" key at all
                "duration": 0.05,
                "call": {},
            }],
        }
        rows = await _invoke_runner(tmp_path, report)
        assert rows[0]["status"] == "FAIL"
        assert "outcome" in rows[0]["error_message"].lower()


# ── Bug 5: nodeid keeps last 2 segments ──────────────────────────────────

class TestNodeIdRetention:

    async def test_nodeid_keeps_class_and_test(self, tmp_path):
        report = {
            "tests": [
                {"nodeid": "f.py::TestX::test_a", "outcome": "failed", "duration": 0.1,
                 "call": {"longrepr": "fail X"}},
                {"nodeid": "f.py::TestY::test_a", "outcome": "failed", "duration": 0.1,
                 "call": {"longrepr": "fail Y"}},
            ],
        }
        rows = await _invoke_runner(tmp_path, report)
        ids = [r["test_id"] for r in rows]
        # IDs should be distinguishable — test_a from TestX vs TestY
        assert len(set(ids)) == 2, f"Class names should prevent collision but got {ids}"
        assert any("TestX::test_a" in tid for tid in ids)
        assert any("TestY::test_a" in tid for tid in ids)
