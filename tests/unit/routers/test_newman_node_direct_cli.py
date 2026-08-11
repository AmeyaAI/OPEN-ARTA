"""Hardened-image compat — Newman must be invoked without npx (npx needs
/bin/sh, absent in the hardened image → `spawn sh ENOENT` → every Newman run
fails). Prefer node-direct cli.js, then the global node-shebang `newman`
binary, then npx; honor the ARTA_NEWMAN_BIN operator override.
"""
from __future__ import annotations

from src.api.routers.execution import _newman_argv


def test_node_direct_cli_when_present(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTA_NEWMAN_NODE_DIRECT_DISABLE", raising=False)
    monkeypatch.delenv("ARTA_NEWMAN_BIN", raising=False)
    monkeypatch.chdir(tmp_path)
    cli = tmp_path / "node_modules" / "newman" / "bin" / "newman.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// newman cli")
    argv = _newman_argv()
    assert argv[-1] == "run"
    assert any("newman.js" in a for a in argv)
    assert "npx" not in argv


def test_global_newman_binary_when_no_cli(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTA_NEWMAN_NODE_DIRECT_DISABLE", raising=False)
    monkeypatch.delenv("ARTA_NEWMAN_BIN", raising=False)
    monkeypatch.chdir(tmp_path)  # no node_modules
    monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/newman" if b == "newman" else None)
    assert _newman_argv() == ["/usr/bin/newman", "run"]  # node-shebang binary, no sh


def test_killswitch_forces_npx(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTA_NEWMAN_NODE_DIRECT_DISABLE", "1")
    monkeypatch.delenv("ARTA_NEWMAN_BIN", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _newman_argv() == ["npx", "newman", "run"]


def test_operator_override_wins(monkeypatch):
    monkeypatch.setenv("ARTA_NEWMAN_BIN", "/opt/newman-4.6/newman")
    assert _newman_argv() == ["/opt/newman-4.6/newman", "run"]
    monkeypatch.setenv("ARTA_NEWMAN_BIN", "npx")
    assert _newman_argv() == ["npx", "newman", "run"]
