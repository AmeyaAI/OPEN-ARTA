"""R294 — FUNCTIONAL test of the discovery env-forwarding (the R285 lesson).

R285 shipped DEAD: its tests only asserted on source text (`inspect.getsource`)
and never CALLED the code, so a signature-mismatch TypeError sailed through
green. Several env-forwarding features this session (R265 auth-refresh, R266/R278
read-POST, R272 skip_routes, R292 app-entry/fallback seeds) were likewise pinned
mostly by "does the source contain this string?" — which cannot catch a real
env-building bug.

This test CALLS `_spawn_playwright_discovery`, mocks the subprocess spawn, and
asserts the ACTUAL env dict handed to the probe carries every per-project var.
If any forwarding silently breaks, this fails — where a source-grep would not.
"""
import asyncio

import pytest

import src.agents.discovery_executor as de


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return (b"", b"")


def _run_spawn(tmp_path, monkeypatch, **kwargs) -> dict:
    """Invoke the real spawn function with a mocked subprocess; return the env
    dict it actually built."""
    captured = {}

    async def _fake_exec(*cmd, env=None, **kw):
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(de.asyncio, "create_subprocess_exec", _fake_exec)
    # keep any HAR/ingest side effects cheap + inert
    monkeypatch.setattr(de, "ingest_dom_snapshots", lambda *a, **k: {"routes": {}},
                        raising=False)

    har = tmp_path / "discovery.har"
    coro = de._spawn_playwright_discovery(
        har_path=har,
        spec_dir=tmp_path,
        base_url="https://sut.example.com",
        **kwargs,
    )
    try:
        asyncio.get_event_loop().run_until_complete(coro)
    except Exception:
        # the function may do post-spawn work that needs more context than a
        # unit fixture provides; we only care about the env captured AT spawn.
        pass
    assert "env" in captured, "the spawn was never reached — env not built"
    return captured["env"]


def test_skip_routes_actually_land_in_the_env(tmp_path, monkeypatch):
    """R272 — the union logic, EXERCISED (not source-grepped)."""
    env = _run_spawn(tmp_path, monkeypatch, skip_routes=["/Security", "/admin"])
    assert env.get("ARTA_R150_SKIP_ROUTES")
    parts = env["ARTA_R150_SKIP_ROUTES"].split(",")
    assert "/Security" in parts and "/admin" in parts


def test_skip_routes_union_dedups_against_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTA_R150_SKIP_ROUTES", "/existing")
    env = _run_spawn(tmp_path, monkeypatch, skip_routes=["/existing", "/new"])
    parts = env["ARTA_R150_SKIP_ROUTES"].split(",")
    assert parts.count("/existing") == 1, "union must dedup, not duplicate"
    assert "/new" in parts


def test_app_entry_and_fallback_seeds_land_in_the_env(tmp_path, monkeypatch):
    """R292 — the genericity fix, EXERCISED end to end."""
    env = _run_spawn(
        tmp_path, monkeypatch,
        app_entry_routes=["/ai-apps", "/insights"],
        fallback_route_guesses=["/dashboard", "/projects"])
    assert env.get("ARTA_R182_APP_ENTRY") == "/ai-apps,/insights"
    assert env.get("ARTA_FALLBACK_ROUTE_GUESSES") == "/dashboard,/projects"


def test_no_seeds_means_no_seed_env(tmp_path, monkeypatch):
    """A SUT that configures nothing must not inherit another SUT's routes."""
    env = _run_spawn(tmp_path, monkeypatch)
    assert not env.get("ARTA_R182_APP_ENTRY")
    assert not env.get("ARTA_FALLBACK_ROUTE_GUESSES")


def test_post_read_allowlist_lands_in_the_env(tmp_path, monkeypatch):
    """R266/R278 — the read-POST allowlist, EXERCISED."""
    env = _run_spawn(tmp_path, monkeypatch,
                     post_read_allowlist=["/menumanagement/api/getUserMenus"])
    assert "getUserMenus" in (env.get("TARGET_POST_READ_ALLOWLIST") or "")


def test_auth_refresh_fulfill_lands_in_the_env(tmp_path, monkeypatch):
    """R265 — auth-refresh fulfill config, EXERCISED."""
    env = _run_spawn(tmp_path, monkeypatch, auth_refresh_fulfill={
        "url_contains": "/sso/api/refreshToken",
        "response_template": {"ok": True}})
    assert env.get("TARGET_AUTH_REFRESH_MATCH") == "/sso/api/refreshToken"
    assert env.get("TARGET_AUTH_REFRESH_RESPONSE")


def test_only_absolute_paths_forwarded(tmp_path, monkeypatch):
    """A junk relative entry must be dropped, not forwarded."""
    env = _run_spawn(tmp_path, monkeypatch, skip_routes=["/ok", "notapath"])
    parts = (env.get("ARTA_R150_SKIP_ROUTES") or "").split(",")
    assert "/ok" in parts and "notapath" not in parts
