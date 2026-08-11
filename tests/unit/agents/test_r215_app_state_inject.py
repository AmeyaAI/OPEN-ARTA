"""R215 Item-0 — SPA app-state injection (E1 keystone of the a11y mission).

Builds `selectedOrganization`/`selectedProject`/`selectedWorkspace` (camelCase,
FULL cm items) from the live cm hierarchy so a fresh-cookie SPA renders DATA
VIEWS instead of the org/project selection wall — the goal-achieving arm that
lets axe / PW-UI / discovery actually scan the real authenticated SUT.
"""
from __future__ import annotations

import json

from src.agents.automation_engineer import _r215_norm_id, _r215_build_app_state


def _org(i, n="Org"):
    return {"payload": {"id": i, "name": n, "__auto_id__": "a-" + i}}


def _ws(i, n="WS"):
    return {"payload": {"id": i, "name": n, "__auto_id__": "a-" + i}}


def _proj(i, org_id, ws_id, n="Proj"):
    # organization_id + workspace can be str OR list (R215 norm handles both)
    return {"payload": {"id": i, "name": n, "organization_id": org_id, "workspace": ws_id}}


def test_norm_id_handles_str_list_dict():
    assert _r215_norm_id("abc") == "abc"
    assert _r215_norm_id(["abc", "def"]) == "abc"
    assert _r215_norm_id({"id": "xyz"}) == "xyz"
    assert _r215_norm_id({"__auto_id__": "auto"}) == "auto"
    assert _r215_norm_id([]) is None
    assert _r215_norm_id(None) is None


def test_consistent_triple_full_items():
    orgs = [_org("o1"), _org("o2")]
    wss = [_ws("w1"), _ws("w2")]
    # project p2 links org o2 + ws w2 (both resolvable)
    projs = [_proj("p2", "o2", "w2", "Bank Statement")]
    state = _r215_build_app_state(orgs, wss, projs)
    assert set(state.keys()) == {"selectedOrganization", "selectedWorkspace", "selectedProject"}
    # values are FULL cm items (not minimal stubs) — SPA reads .payload.id + siblings
    proj = json.loads(state["selectedProject"])
    assert proj["payload"]["id"] == "p2" and proj["payload"]["name"] == "Bank Statement"
    org = json.loads(state["selectedOrganization"])
    assert org["payload"]["id"] == "o2"          # the project's OWN org, not just orgs[0]
    ws = json.loads(state["selectedWorkspace"])
    assert ws["payload"]["id"] == "w2"


def test_list_shaped_linkage_resolves():
    orgs = [_org("o1")]
    wss = [_ws("w1")]
    projs = [_proj("p1", ["o1"], [{"id": "w1"}])]   # org as [id], workspace as [{id}]
    state = _r215_build_app_state(orgs, wss, projs)
    assert json.loads(state["selectedOrganization"])["payload"]["id"] == "o1"
    assert json.loads(state["selectedWorkspace"])["payload"]["id"] == "w1"


def test_fallback_when_no_consistent_triple():
    """A project whose org/ws don't resolve → fall back to first project + orgs[0]."""
    orgs = [_org("o1")]
    wss = [_ws("w1")]
    projs = [_proj("p9", "UNKNOWN-ORG", "UNKNOWN-WS")]
    state = _r215_build_app_state(orgs, wss, projs)
    assert json.loads(state["selectedProject"])["payload"]["id"] == "p9"
    assert json.loads(state["selectedOrganization"])["payload"]["id"] == "o1"  # fallback org
    assert json.loads(state["selectedWorkspace"])["payload"]["id"] == "w1"     # fallback ws


def test_empty_when_no_orgs_or_projects():
    assert _r215_build_app_state([], [_ws("w1")], [_proj("p1", "o1", "w1")]) == {}
    assert _r215_build_app_state([_org("o1")], [_ws("w1")], []) == {}
    # no workspace at all + project ws unresolvable → can't build → {}
    assert _r215_build_app_state([_org("o1")], [], [_proj("p1", "o1", "w1")]) == {}
