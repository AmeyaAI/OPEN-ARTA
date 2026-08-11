"""R156.A.3 — Frontend → Backend call-graph linkage tests.

The helper `AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes`
takes FE-route entries (with api_calls extracted by R105.C) + BE-route
entries (with body_schema_source + auth_requirement extracted by
R156.A.1 + R156.A.2) and stamps each FE route with `api_call_links:
list[dict]` that links each call URL to its canonical backend handler.

Mission contract (Pillar 1b): closes Iter 11's PW `waitForResponse`
hallucination class — the LLM sees per FE route which BE handlers it
invokes WITH their request body schema + required token kind, so
`page.waitForResponse` references real SUT URLs and assertions match
real response shapes.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Path normalization ─────────────────────────────────────────────────


def test_r156_a3_normalize_strips_query_and_hash():
    norm = AutomationEngineerAgent._r156_a_3_normalize_path_for_match
    assert norm("/api/v1/datasets?limit=10") == "/api/v1/datasets"
    assert norm("/api/v1/datasets#section") == "/api/v1/datasets"
    assert norm("/api/v1/datasets?limit=10#anchor") == "/api/v1/datasets"


def test_r156_a3_normalize_strips_scheme_and_host():
    norm = AutomationEngineerAgent._r156_a_3_normalize_path_for_match
    assert norm("https://api.example.com/api/v1/datasets") == "/api/v1/datasets"
    assert norm("http://localhost:3000/api/v1/datasets") == "/api/v1/datasets"


def test_r156_a3_normalize_replaces_template_vars_with_wildcard():
    """Express-style `${var}` and Python-style `{var}` placeholders
    normalize to the same canonical wildcard so FE `${apiBase}/${id}`
    matches BE `/datasets/{id}`."""
    norm = AutomationEngineerAgent._r156_a_3_normalize_path_for_match
    assert norm("/api/v1/datasets/${id}") == "/api/v1/datasets/:wild"
    assert norm("/api/v1/datasets/{dataset_id}") == "/api/v1/datasets/:wild"


def test_r156_a3_normalize_empty_and_root():
    norm = AutomationEngineerAgent._r156_a_3_normalize_path_for_match
    assert norm("") == ""
    assert norm("/") == "/"
    # Trailing slash stripped on non-root paths
    assert norm("/api/v1/datasets/") == "/api/v1/datasets"


def test_r156_a3_normalize_handles_non_string_input():
    norm = AutomationEngineerAgent._r156_a_3_normalize_path_for_match
    assert norm(None) == ""  # type: ignore[arg-type]
    assert norm(123) == ""   # type: ignore[arg-type]


# ── Linkage happy path ────────────────────────────────────────────────


def test_r156_a3_links_fe_call_to_be_route_with_body_schema():
    """FE route api_calls=['/api/v1/datasets'] + BE route POST
    /api/v1/datasets with body_schema → FE route gains api_call_links
    carrying method + body_schema_source + auth_requirement."""
    fe_routes = [{
        "route": "/dashboard/datasets",
        "component": "DatasetsPage.tsx",
        "api_calls": ["/api/v1/datasets"],
    }]
    be_routes = [{
        "method": "POST",
        "path": "/api/v1/datasets",
        "body_schema_source": "class DatasetCreate(BaseModel):\n    name: str",
        "auth_requirement": {"token_kind": "agent_token", "scopes": []},
    }]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert "api_call_links" in fe_routes[0]
    links = fe_routes[0]["api_call_links"]
    assert len(links) == 1
    link = links[0]
    assert link["url"] == "/api/v1/datasets"
    assert link["method"] == "POST"
    assert "body_schema_source" in link
    assert "auth_requirement" in link
    assert link["auth_requirement"]["token_kind"] == "agent_token"


def test_r156_a3_links_multiple_methods_at_same_path():
    """When BE serves both GET and POST on the same path, BOTH show
    up as separate links (LLM can then choose which to assert against
    based on scenario)."""
    fe_routes = [{
        "route": "/dashboard",
        "component": "Dashboard.tsx",
        "api_calls": ["/api/v1/datasets"],
    }]
    be_routes = [
        {"method": "GET", "path": "/api/v1/datasets"},
        {"method": "POST", "path": "/api/v1/datasets",
         "body_schema_source": "class X: ..."},
    ]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    links = fe_routes[0]["api_call_links"]
    methods = {lk["method"] for lk in links}
    assert "GET" in methods
    assert "POST" in methods


def test_r156_a3_links_template_var_to_canonical_path():
    """FE call `${apiBase}/api/v1/datasets/${id}` normalizes to
    `/api/v1/datasets/:wild` and matches BE `/api/v1/datasets/{id}`."""
    fe_routes = [{
        "route": "/dashboard/datasets/[id]",
        "component": "DatasetDetail.tsx",
        "api_calls": ["/api/v1/datasets/${id}"],
    }]
    be_routes = [{
        "method": "GET",
        "path": "/api/v1/datasets/{dataset_id}",
        "auth_requirement": {"token_kind": "agent_token"},
    }]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert "api_call_links" in fe_routes[0]
    assert fe_routes[0]["api_call_links"][0]["method"] == "GET"


def test_r156_a3_links_api_prefix_variant():
    """When FE references `/v1/X` but BE is declared `/api/v1/X`
    (mount-prefix gap R107.C / R143.B), the helper tries the
    `/api`-prefix variant and links them anyway."""
    fe_routes = [{
        "route": "/dashboard",
        "component": "Dashboard.tsx",
        "api_calls": ["/v1/datasets"],
    }]
    be_routes = [{
        "method": "POST",
        "path": "/api/v1/datasets",
        "body_schema_source": "class X: ...",
    }]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert "api_call_links" in fe_routes[0]
    assert fe_routes[0]["api_call_links"][0]["url"] == "/v1/datasets"


def test_r156_a3_strips_api_prefix_variant():
    """Reverse direction: FE references `/api/v1/X` but BE declared
    without `/api` prefix → still link via prefix-strip."""
    fe_routes = [{
        "route": "/dashboard",
        "component": "Dashboard.tsx",
        "api_calls": ["/api/v1/datasets"],
    }]
    be_routes = [{
        "method": "GET",
        "path": "/v1/datasets",
    }]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert "api_call_links" in fe_routes[0]


def test_r156_a3_caps_links_at_eight_per_route():
    """A FE component that invokes >8 backend routes — helper caps
    at 8 links to keep prompt budget bounded."""
    fe_routes = [{
        "route": "/dashboard",
        "component": "BigComponent.tsx",
        "api_calls": [f"/api/v1/x{i}" for i in range(15)],
    }]
    be_routes = [
        {"method": "GET", "path": f"/api/v1/x{i}"} for i in range(15)
    ]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert len(fe_routes[0]["api_call_links"]) <= 8


# ── No-link cases ─────────────────────────────────────────────────────


def test_r156_a3_no_links_when_no_api_calls():
    """FE route without api_calls field → no api_call_links added."""
    fe_routes = [{
        "route": "/login",
        "component": "Login.tsx",
        # no api_calls field
    }]
    be_routes = [{"method": "POST", "path": "/api/auth/login"}]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert "api_call_links" not in fe_routes[0]


def test_r156_a3_no_links_when_no_matching_backend():
    """FE api_calls reference URLs the BE doesn't expose → no links
    field added (LLM can still see the raw api_calls list)."""
    fe_routes = [{
        "route": "/dashboard",
        "component": "Dashboard.tsx",
        "api_calls": ["/api/v1/nonexistent"],
    }]
    be_routes = [{"method": "GET", "path": "/api/v1/datasets"}]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    assert "api_call_links" not in fe_routes[0]


def test_r156_a3_returns_input_unchanged_on_empty_inputs():
    """Empty fe_routes OR empty be_routes → helper returns the input
    list unchanged (no exception)."""
    assert AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes([], []) == []
    assert AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(
        [{"route": "/x"}], [],
    ) == [{"route": "/x"}]


def test_r156_a3_ignores_non_dict_entries():
    """Mixed valid + invalid entries → helper skips non-dicts without
    raising, processes dicts normally."""
    fe_routes: list = [
        None,  # type: ignore[list-item]
        {"route": "/dashboard", "api_calls": ["/api/v1/x"]},
        "not-a-dict",  # type: ignore[list-item]
    ]
    be_routes: list = [
        {"method": "GET", "path": "/api/v1/x"},
        None,  # type: ignore[list-item]
    ]
    result = AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    # The valid dict gets linked, invalid entries pass through unchanged
    assert "api_call_links" in result[1]


# ── Link shape ────────────────────────────────────────────────────────


def test_r156_a3_link_omits_empty_optional_fields():
    """When BE route lacks body_schema_source / auth_requirement,
    those fields are NOT included in the link (cleaner prompt)."""
    fe_routes = [{
        "route": "/dashboard",
        "component": "Dashboard.tsx",
        "api_calls": ["/api/v1/health"],
    }]
    be_routes = [{
        "method": "GET",
        "path": "/api/v1/health",
        # no body_schema_source, no auth_requirement
    }]
    AutomationEngineerAgent._r156_a_3_link_fe_to_be_routes(fe_routes, be_routes)
    link = fe_routes[0]["api_call_links"][0]
    assert link["url"] == "/api/v1/health"
    assert link["method"] == "GET"
    assert "body_schema_source" not in link
    assert "auth_requirement" not in link
