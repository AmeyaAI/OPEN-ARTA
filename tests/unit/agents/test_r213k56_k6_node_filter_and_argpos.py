"""R213.K.5 — chain node filter (drop third-party/static/SPA; skip noise-only
chains) and R213.K.6 — correct k6 http arg position per method.

Both were live-diagnosed: chains were ~91% third-party/static noise that
short-circuited before real reads (K.5), and GET specs put `{headers}` in the
ignored 3rd arg → no Authorization → 401 (K.6). After both, a regenerated cm
chain returns 5/5 checks (100%) against the live SUT.
"""
from __future__ import annotations

import json
import re

from src.agents.chain_aware_k6 import build_chain_aware_k6, _filter_sut_api_nodes, _NOISE_PATH_RE


# ── R213.K.6 — http arg position ────────────────────────────────────────────
def _chain(nodes):
    return {"chain_id": "c", "semantic_hash": "h", "nodes": nodes}


def test_get_puts_params_as_second_arg_with_headers():
    js = build_chain_aware_k6(
        _chain([{"method": "GET", "path_template": "/v1/x", "sequence_index": 0, "provides": {}, "consumes": {}}]),
        requirement_id="REQ-1", project_vars={},
    )
    # GET: http.get(url, { headers: ..., tags: ... })  — NO body arg between
    m = re.search(r"http\.get\(artaApiUrl\(baseUrl, `[^`]*`\),\s*\{ headers: artaAuthHeader\(", js)
    assert m, f"GET must pass params (with headers) as the 2nd arg, got:\n{js[js.find('http.get'):js.find('http.get')+120]}"
    # must NOT be http.get(url, null/body, {...})
    assert "http.get(artaApiUrl(baseUrl, `/v1/x`), null," not in js
    assert "http.get(artaApiUrl(baseUrl, `/v1/x`), JSON.stringify" not in js


def test_post_keeps_body_then_params():
    js = build_chain_aware_k6(
        _chain([{"method": "POST", "path_template": "/v1/y", "sequence_index": 0, "provides": {}, "consumes": {}}]),
        requirement_id="REQ-1", project_vars={},
    )
    assert re.search(r"http\.post\(artaApiUrl\(baseUrl, `[^`]*`\), JSON\.stringify\(\{\}\), \{ headers: artaAuthHeader\(", js)


# ── R213.K.5 — node filter ───────────────────────────────────────────────────
SYNTH_SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/v1/orders/{order_id}": {"get": {"responses": {"200": {}}}},
        "/v1/customers": {"get": {"responses": {"200": {}}}},
    },
}
NOISE = [
    "/google.firestore.v1.Firestore/Listen/channel", "/images/cleardot.gif",
    "/static/js/bundle.js", "/s/inter/v20/x.woff2", "/icon", "/favicon.ico", "/dashboard",
]
REAL = ["/v1/orders/abc123", "/v1/customers"]


def test_noise_regex_flags_third_party_and_static():
    for p in NOISE[:-1]:  # /dashboard is an SPA route, dropped by contract miss not the noise RE
        assert _NOISE_PATH_RE.search(p), p


def test_filter_keeps_only_contract_real(tmp_path, monkeypatch):
    import src.agents.api_discovery as apidisc
    apidisc._R206_MATCHER_CACHE.clear()
    monkeypatch.setattr(apidisc, "_OPENAPI_DIR", tmp_path)
    pid = "synth-sut"
    (tmp_path / f"{pid}.json").write_text(json.dumps(SYNTH_SPEC))
    nodes = [{"method": "GET", "path_template": p} for p in (NOISE + REAL)]
    kept = _filter_sut_api_nodes(nodes, pid)
    kept_paths = {n["path_template"] for n in kept}
    assert kept_paths == set(REAL), kept_paths


def test_noise_only_chain_skips_emission(tmp_path, monkeypatch):
    import src.agents.api_discovery as apidisc
    apidisc._R206_MATCHER_CACHE.clear()
    monkeypatch.setattr(apidisc, "_OPENAPI_DIR", tmp_path)
    pid = "synth-sut2"
    (tmp_path / f"{pid}.json").write_text(json.dumps(SYNTH_SPEC))
    chain = {"chain_id": "c", "semantic_hash": "h", "project_id": pid,
             "nodes": [{"method": "GET", "path_template": p, "sequence_index": i} for i, p in enumerate(NOISE)]}
    js = build_chain_aware_k6(chain, requirement_id="REQ-1", project_vars={})
    assert js == "", "a chain with only noise nodes must skip emission (return '')"


def test_killswitch_keeps_all_nodes(tmp_path, monkeypatch):
    import src.agents.api_discovery as apidisc
    monkeypatch.setattr(apidisc, "_OPENAPI_DIR", tmp_path)
    pid = "synth-sut3"
    (tmp_path / f"{pid}.json").write_text(json.dumps(SYNTH_SPEC))
    monkeypatch.setenv("ARTA_K6_CHAIN_NODE_FILTER_DISABLE", "1")
    nodes = [{"method": "GET", "path_template": p} for p in (NOISE + REAL)]
    assert len(_filter_sut_api_nodes(nodes, pid)) == len(nodes)
