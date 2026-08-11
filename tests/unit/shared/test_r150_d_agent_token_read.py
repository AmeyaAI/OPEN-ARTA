"""R150.D — verify auto_detect_auth_bypass reads from env_block.variables
correctly when project_meta is passed end-to-end from tests.py:2671.

Pre-R150.D bug: callers passed None as project_meta → auto_detect returned
False unconditionally → R145.B login-flow AC filter never fired.

Post-R150.D: tests.py:2671 hoists `project: dict | None = None` initializer
above the `if project_id:` block, then passes `project_meta=project` to
atdd_agent.generate(). The check at auth_route_patterns.py:130-133 reads
env_block.variables.agent_token correctly.

These tests verify the auto-detect contract end-to-end:
  - When R45.2 paste has fired (meta sidecar exists + agent_token populated),
    auto-detect returns True.
  - When meta sidecar missing OR agent_token sparse, returns False.
"""
from __future__ import annotations

from pathlib import Path

from src.shared.auth_route_patterns import auto_detect_auth_bypass


def _make_project(env_name: str = "staging", agent_token: str = "") -> dict:
    return {
        "id": "test-project-r150d",
        "environments": {
            env_name: {
                "variables": {"agent_token": agent_token},
            },
        },
    }


def _make_meta_sidecar(tmp_path: Path, env_name: str = "staging") -> Path:
    sidecar = tmp_path / f"{env_name}-storage.json.r112a.meta.json"
    sidecar.write_text('{"source": "r45_2_paste"}')
    return sidecar


def test_r150_d_meta_present_token_populated_returns_true(tmp_path):
    """Happy path: meta sidecar exists + agent_token >= 50 chars → True."""
    _make_meta_sidecar(tmp_path, "staging")
    project = _make_project("staging", agent_token="a" * 831)  # R96.1 mint length
    assert auto_detect_auth_bypass(project, "staging", envs_dir=tmp_path) is True


def test_r150_d_meta_missing_returns_false(tmp_path):
    """No paste-trust meta sidecar → False (operator hasn't pasted)."""
    project = _make_project("staging", agent_token="a" * 831)
    assert auto_detect_auth_bypass(project, "staging", envs_dir=tmp_path) is False


def test_r150_d_token_empty_returns_false(tmp_path):
    """Meta exists but agent_token empty (R96.1 exchange never ran) → False."""
    _make_meta_sidecar(tmp_path, "staging")
    project = _make_project("staging", agent_token="")
    assert auto_detect_auth_bypass(project, "staging", envs_dir=tmp_path) is False


def test_r150_d_project_none_returns_false(tmp_path):
    """project_meta=None (pre-R150.D bug scenario) → False (early return)."""
    _make_meta_sidecar(tmp_path, "staging")
    assert auto_detect_auth_bypass(None, "staging", envs_dir=tmp_path) is False


def test_r150_d_explicit_override_true_wins(tmp_path):
    """project.auth_bypass=True overrides auto-detect (even without meta)."""
    project = _make_project("staging", agent_token="")
    project["auth_bypass"] = True
    assert auto_detect_auth_bypass(project, "staging", envs_dir=tmp_path) is True


def test_r150_d_explicit_override_false_wins(tmp_path):
    """project.auth_bypass=False overrides auto-detect (even with valid paste)."""
    _make_meta_sidecar(tmp_path, "staging")
    project = _make_project("staging", agent_token="a" * 831)
    project["auth_bypass"] = False
    assert auto_detect_auth_bypass(project, "staging", envs_dir=tmp_path) is False
