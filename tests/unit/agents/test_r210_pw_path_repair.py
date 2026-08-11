"""R210 — deterministic Playwright path-repair + fail-fast.

Root cause (run-cf956e/run-773d65): the LLM invents paths that either (a) have the
right OpenAPI SHAPE but a made-up value in a session-id slot (e.g. a media id
where `{account_id}` belongs → 500), or (b) are pure REST-CRUD inventions with no
real template (`/api/organizations` → 404). `_r206_path_is_real` only checks
shape so it can't catch (a). R210 param-value-corrects (a), snaps wrong shapes
(b) when a confident real template exists, and FAIL-FASTs (returns `unresolved`)
otherwise so the caller forces a truthful BLOCK instead of shipping a 404.
"""
from __future__ import annotations

import json

import src.agents.automation_engineer as ae
import src.agents.auth_refresher as ar
import src.agents.auth_chain as ac
from src.agents.automation_engineer import AutomationEngineerAgent

_PID = "r210-test-pid"
_ACC = "0aee6bd7-ed42-4184-9bac-ce0466737ada"
_SUB = "6551f605-39cb-4351-8ea1-b2a7af317985"
_SUBN = "a6a49ce0-1121-455c-91cd-7956eb0891dd"


def _setup(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".arta" / "openapi").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".arta" / "openapi" / f"{_PID}.json").write_text(json.dumps({
        "paths": {
            "/api/media/{account_id}/subscriber/{subscriber_id}/subscription/{subscription_id}/generate-upload-url": {"get": {}},
            "/api/collection/subscriber/{subscriber_id}/subscription/{subscription_id}/collection-modules": {"get": {}},
        }
    }))
    # captured endpoints (R206-cleaned shape) — feed the grounding index
    monkeypatch.setattr(ae, "_load_captured_endpoints", None, raising=False)
    import src.agents.api_discovery as ad
    monkeypatch.setattr(ad, "_load_captured_endpoints", lambda pid: [
        {"method": "GET", "path": f"/api/media/{_ACC}/subscriber/{_SUB}/subscription/{_SUBN}/generate-upload-url"},
    ])
    monkeypatch.setattr(ar, "_find_storage_state_path", lambda env: "ss.json")
    monkeypatch.setattr(ar, "_read_storage_state", lambda p: {"_": True})
    monkeypatch.setattr(ac, "harvest_session_ids_from_storage", lambda ss: {
        "account_id": _ACC, "subscriber_id": _SUB, "subscription_id": _SUBN,
    })


def _r210(content):
    return AutomationEngineerAgent._r210_reground_pw_paths(
        content, {"project_id": _PID, "environment": "staging"}, "media upload organizations")


def test_r210_corrects_made_up_session_id(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    # R207.B sets authHeaderFor to the SAME url token as the request (realistic).
    _url = "`${apiBase}/api/media/MADEUP/subscriber/" + _SUB + "/subscription/" + _SUBN + "/generate-upload-url`"
    spec = ("test('x', async ({ request }) => {\n"
            "  const r = await request.get(" + _url + ", { headers: authHeaderFor(" + _url + ") });\n"
            "  expect(r.status()).toBe(200);\n});\n")
    new, n, unresolved = _r210(spec)
    assert n >= 1 and not unresolved
    # the made-up account_id slot is corrected to the REAL account id; the URL
    # literal is routed via apiUrlFor and the happy-path auth uses authHeaderFor.
    assert f"apiUrlFor('/api/media/{_ACC}/subscriber/{_SUB}/subscription/{_SUBN}/generate-upload-url')" in new
    assert "authHeaderFor(" in new
    assert "MADEUP" not in new
    assert "import { apiUrlFor" in new


def test_r210_failfast_invented_rest_path(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    spec = ("test('x', async ({ request }) => {\n"
            "  const r = await request.get(`${apiBase}/api/organizations`, { headers: authHeaderFor(`${apiBase}/api/organizations`) });\n"
            "  expect(r.status()).toBe(200);\n});\n")
    new, n, unresolved = _r210(spec)
    assert n == 0
    assert unresolved == ["GET /api/organizations"]   # → caller forces unknown_endpoint BLOCK


def test_r210_negative_badtoken_auth_preserved(monkeypatch, tmp_path):
    """A bad-TOKEN negative (asserts 401 via an invalid token) must KEEP the bad
    token — Pass B must not auth-enable it. The path may be corrected (harmless:
    the 401 comes from the token, not the path)."""
    _setup(monkeypatch, tmp_path)
    spec = ("test('neg', async ({ request }) => {\n"
            "  const r = await request.get(`${apiBase}/api/media/MADEUP/subscriber/" + _SUB +
            "/subscription/" + _SUBN + "/generate-upload-url`, { headers: { 'Authorization': 'Bearer invalid' } });\n"
            "  expect(r.status()).toBe(401);\n});\n")
    new, _n, _unresolved = _r210(spec)
    assert "Bearer invalid" in new           # auth NOT enabled — bad token preserved
    assert "authHeaderFor(" not in new       # Pass B left this negative call alone


def test_r210_negative_sentinel_path_preserved(monkeypatch, tmp_path):
    """A bad-VALUE negative (sentinel id in the path) must keep the sentinel."""
    _setup(monkeypatch, tmp_path)
    bad = "00000000-0000-0000-0000-000000000000"
    spec = ("test('neg', async ({ request }) => {\n"
            "  const r = await request.get(`${apiBase}/api/media/" + _ACC + "/subscriber/" + bad +
            "/subscription/" + _SUBN + "/generate-upload-url`);\n"
            "  expect(r.status()).toBe(404);\n});\n")
    new, n, unresolved = _r210(spec)
    assert n == 0 and not unresolved
    assert bad in new                        # sentinel preserved (path not corrected)


def test_r210_already_correct_path_untouched(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    spec = ("test('x', async ({ request }) => {\n"
            "  const r = await request.get(`${apiBase}/api/media/" + _ACC + "/subscriber/" + _SUB +
            "/subscription/" + _SUBN + "/generate-upload-url`, { headers: authHeaderFor('x') });\n"
            "  expect(r.status()).toBe(200);\n});\n")
    new, n, unresolved = _r210(spec)
    assert n == 0 and not unresolved   # real correct path → no rewrite, no block


def test_r210_killswitch(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("ARTA_R210_PW_REGROUND_DISABLE", "1")
    spec = "const r = await request.get(`${apiBase}/api/organizations`);"
    new, n, unresolved = _r210(spec)
    assert new == spec and n == 0 and unresolved == []
