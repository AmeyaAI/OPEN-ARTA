"""Deterministic .proto tree-walk fetch — the gRPC-discovery source step.
GitHub code-search does not index every private repo, so an org-scoped
`search_code` misses `.proto` in a private SUT auth service; a git-tree walk of
the configured repos surfaces them reliably."""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.agents import github_context as gc

_PROTO = (
    'syntax = "proto3";\n'
    "service AuthorizationService {\n"
    "  rpc Authenticate (AuthRequest) returns (AuthResponse);\n"
    "}\n"
    "message AuthRequest { string token = 1; }\n"
)


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _mock_client(calls):
    async def _fake_get(url, params=None):
        calls.append(url)
        if "/git/trees/" in url:
            return _Resp(200, {"tree": [
                {"type": "blob", "path": "src/server2/app/authorization.proto"},
                {"type": "blob", "path": "README.md"},          # excluded (ext)
                {"type": "tree", "path": "src"},                # excluded (not blob)
            ]})
        if "/contents/" in url:
            return _Resp(200, {"content": _b64(_PROTO), "encoding": "base64"})
        return _Resp(404, {})

    client = AsyncMock()
    client.get = _fake_get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    gc._CACHE.clear()
    monkeypatch.delenv("ARTA_GH_TREE_WALK_DISABLE", raising=False)
    yield
    gc._CACHE.clear()


@pytest.mark.asyncio
async def test_fetch_proto_walks_tree_on_branch():
    calls: list = []
    project = {"integrations": {"github_token": "tok", "repositories": [
        {"repo": "Org/auth-server", "branch": "main"}]}}
    with patch("httpx.AsyncClient", return_value=_mock_client(calls)):
        files = await gc.fetch_files_by_extension(project, ".proto", cap=10)
    assert len(files) == 1                                   # README/tree filtered
    assert files[0]["path"] == "src/server2/app/authorization.proto"
    assert "AuthorizationService" in files[0]["text"]
    assert any("/git/trees/main" in c for c in calls)         # configured branch used


@pytest.mark.asyncio
async def test_fetch_proto_no_token_or_repos():
    assert await gc.fetch_files_by_extension({"integrations": {}}, ".proto") == []
    proj = {"integrations": {"github_token": "t"}}            # token but no repos
    assert await gc.fetch_files_by_extension(proj, ".proto") == []


@pytest.mark.asyncio
async def test_fetch_proto_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_GH_TREE_WALK_DISABLE", "1")
    proj = {"integrations": {"github_token": "t", "repositories": [{"repo": "O/r"}]}}
    assert await gc.fetch_files_by_extension(proj, ".proto") == []


def test_project_repo_entries_defaults_head():
    proj = {"integrations": {"repositories": [
        {"repo": "O/a", "branch": "dev"}, {"repo": "O/b"}, "O/c"],
        "github_repo": "O/single"}}
    entries = gc._project_repo_entries(proj)
    assert ("O/a", "dev") in entries
    assert ("O/b", "HEAD") in entries and ("O/c", "HEAD") in entries
    assert ("O/single", "HEAD") in entries
