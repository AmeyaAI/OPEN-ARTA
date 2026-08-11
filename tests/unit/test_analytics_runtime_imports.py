"""Phase K11 — smoke test that `arta_runtime` is importable as the
generated analytics tests expect.

Generated adversarial pytest tests in
`src/automation/python_tests/analytics/*.py` start with:

    from arta_runtime import analytics_client  # injected by execution router

This works only when `src/automation/python_tests` is on `PYTHONPATH`.
The execution router sets PYTHONPATH for the pytest subprocess; this
test verifies the module is structurally importable AND exposes the
expected surface.

When this test fails in CI, every analytics test in the repo will
ImportError at collection — that's what produced 27% pytest failures
in run-89e80da6 before K11 landed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTEST_PATH = REPO_ROOT / "src" / "automation" / "python_tests"


def test_arta_runtime_importable_with_correct_pythonpath():
    """K11 — `from arta_runtime import analytics_client` must succeed
    when PYTHONPATH includes src/automation/python_tests."""
    env = {**os.environ, "PYTHONPATH": str(PYTEST_PATH)}
    result = subprocess.run(
        [sys.executable, "-c", "from arta_runtime import analytics_client; print(type(analytics_client).__name__)"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"`from arta_runtime import analytics_client` failed.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}\n"
        f"PYTHONPATH used: {env['PYTHONPATH']}"
    )
    assert result.stdout.strip(), "import succeeded but printed nothing — analytics_client is None?"


def test_arta_runtime_exposes_set_analytics_client():
    """K11 — `set_analytics_client` must be importable. Per-project
    conftest.py uses it to wire a real backend client."""
    env = {**os.environ, "PYTHONPATH": str(PYTEST_PATH)}
    result = subprocess.run(
        [sys.executable, "-c", "from arta_runtime import set_analytics_client; print('OK')"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_arta_runtime_default_client_returns_refusal():
    """K11 — when no real backend is wired, `analytics_client.ask(...)`
    must return a refusal response (refused=True). Adversarial tests
    assert refusal is one valid pass condition; a sane default makes
    the system fail-closed rather than confabulate.
    """
    env = {**os.environ, "PYTHONPATH": str(PYTEST_PATH)}
    code = (
        "from arta_runtime import analytics_client\n"
        "r = analytics_client.ask('test query')\n"
        "print('refused:', getattr(r, 'refused', None))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "refused: True" in result.stdout, (
        f"Default analytics_client did not return refused=True. "
        f"stdout: {result.stdout}"
    )
