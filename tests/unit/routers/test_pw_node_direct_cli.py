"""Hardened-image compat — Playwright must be invoked via node-direct
(`node node_modules/@playwright/test/cli.js test`) not `npx playwright test`,
because the security-hardened image has NO /bin/sh and npx internally
`spawn('sh')` → `spawn sh ENOENT` → the entire PW/axe run fails to start.
"""
from __future__ import annotations

from pathlib import Path

from src.api.routers.execution import _pw_cli_argv


def test_node_direct_when_cli_present(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTA_PW_NODE_DIRECT_DISABLE", raising=False)
    monkeypatch.chdir(tmp_path)
    cli = tmp_path / "node_modules" / "@playwright" / "test" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// cli")
    argv = _pw_cli_argv()
    assert argv[-1] == "test"
    assert any("cli.js" in a for a in argv)
    assert "npx" not in argv  # no npx → no sh dependency
    assert argv[0].endswith("node") or argv[0] == "node"


def test_killswitch_forces_npx(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTA_PW_NODE_DIRECT_DISABLE", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "node_modules" / "@playwright" / "test").mkdir(parents=True)
    (tmp_path / "node_modules" / "@playwright" / "test" / "cli.js").write_text("// cli")
    assert _pw_cli_argv() == ["npx", "playwright", "test"]


def test_fallback_to_npx_when_cli_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("ARTA_PW_NODE_DIRECT_DISABLE", raising=False)
    monkeypatch.chdir(tmp_path)  # no node_modules
    assert _pw_cli_argv() == ["npx", "playwright", "test"]
