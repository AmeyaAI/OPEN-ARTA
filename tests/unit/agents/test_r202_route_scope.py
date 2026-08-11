"""R202 — route-SCOPED catalog grounding.

Root cause it fixes (run-d21eb3, ~155 selector FAILs): the catalog grounding
helpers FLATTEN selectors across ALL routes (drop the route key), so a feature
spec (e.g. /analytics) is grounded against the UNION of nav-page selectors;
the "use ONLY catalog selectors" constraint then forces nav-page selectors
onto feature pages → gen-validation passes but the selector is absent at
runtime. R202 scopes the grounding to the spec's target route(s), falling back
to the full set (never ground against nothing) when no catalog route matches.
"""
from __future__ import annotations

import json
from pathlib import Path

import src.agents.api_discovery as ad
from src.agents.api_discovery import (
    project_testids,
    project_stable_selectors,
    format_dom_catalog_for_prompt,
    r202_select_routes,
)

_PID = "r202-test-pid"


def _setup(monkeypatch, tmp_path):
    """Write a catalog with nav-page selectors A,B on /nav-home and a feature
    selector C on /analytics-home."""
    monkeypatch.setattr(ad, "_DOM_CATALOG_DIR", tmp_path)
    d = tmp_path / _PID
    d.mkdir(parents=True, exist_ok=True)
    catalog = {
        "routes": {
            "/nav-home": [
                {"testid": "navA", "tag": "button", "text": "A"},
                {"testid": "navB", "tag": "button", "text": "B"},
            ],
            "/analytics-home": [
                {"testid": "analyticsC", "tag": "button", "text": "C"},
            ],
        },
        "testid_count": 3,
        "role_name_count": 0,
    }
    (d / "dom_catalog.json").write_text(json.dumps(catalog))


def test_r202_scopes_testids_to_target_route(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    # gherkin route /analytics matches catalog /analytics-home (first-segment)
    assert project_testids(_PID, routes=["/analytics"]) == {"analyticsC"}


def test_r202_no_match_falls_back_to_full(monkeypatch, tmp_path):
    """No catalog route matches → fall back to the FULL set (never empty —
    that would lock the R57.1 retry loop)."""
    _setup(monkeypatch, tmp_path)
    assert project_testids(_PID, routes=["/totally-unrelated"]) == {"navA", "navB", "analyticsC"}


def test_r202_routes_none_is_legacy_flatten(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert project_testids(_PID, routes=None) == {"navA", "navB", "analyticsC"}


def test_r202_stable_selectors_scoped(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    scoped = project_stable_selectors(_PID, routes=["/analytics-home"])
    assert scoped["testids"] == {"analyticsC"}


def test_r202_catalog_prompt_scoped_to_route(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    block = format_dom_catalog_for_prompt(_PID, routes=["/analytics-home"])
    assert "analyticsC" in block
    # nav-page testids must NOT leak into a feature-page spec's catalog block
    assert "navA" not in block and "navB" not in block


def test_r202_select_routes_match_flag():
    routes = {"/nav-home": [1], "/analytics-home": [1]}
    scoped, matched = r202_select_routes(routes, ["/analytics"])
    assert matched is True and set(scoped) == {"/analytics-home"}
    scoped2, matched2 = r202_select_routes(routes, ["/missing"])
    assert matched2 is False and scoped2 == {}


def test_r202_param_route_normalization():
    """Inconsistent :param names + nesting still match by first segment."""
    routes = {"/workspace/:space_id/project/:project_id/collections": [1]}
    scoped, matched = r202_select_routes(routes, ["/workspace/:orgId/project/:pid/collections"])
    assert matched is True
