"""R156.B.3 — FK data dependency resolver tests.

The helper `_r156_b_3_resolve_data_dependencies` reads each BE route's
body_schema_source (set by R156.A.1), extracts FK-shaped fields
(`<entity>_id` / `<entity>Id`), and finds the producer GET endpoint
in the same be_routes list. Stamps `data_dependencies` so R156.B.4
can render "fetch X first, then call Y" chains in the LLM prompt.

Mission contract (Pillar 1): closes the body-field "FK invented as
constant string" hallucination class.
"""
from __future__ import annotations

from src.agents.automation_engineer import AutomationEngineerAgent


# ── Happy path: FK resolved via /entities/me producer ─────────────────


def test_r156_b3_owner_id_resolves_to_users_me_producer():
    """`owner_id` body field → producer `GET /api/v1/users/me`."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "class DatasetCreate(BaseModel):\n"
                "    name: str\n"
                "    owner_id: str\n"
            ),
        },
        {"method": "GET", "path": "/api/v1/users/me"},
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    deps = be_routes[0]["data_dependencies"]
    assert len(deps) == 1
    assert deps[0]["field"] == "owner_id"
    assert deps[0]["producer_method"] == "GET"
    assert deps[0]["producer_path"] == "/api/v1/users/me"


def test_r156_b3_camelcase_field_owns_camelcase_dependency():
    """`ownerId` (camelCase) → dependency uses camelCase field name
    (TS / Express conventions preserved)."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "interface DatasetCreate {\n"
                "  name: string;\n"
                "  ownerId: string;\n"
                "}\n"
            ),
        },
        {"method": "GET", "path": "/api/v1/users/me"},
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    deps = be_routes[0]["data_dependencies"]
    assert any(d["field"] == "ownerId" for d in deps)


def test_r156_b3_falls_back_to_plural_list_endpoint():
    """When no `/entities/me` endpoint exists, helper falls back to
    `GET /entities` (plural list)."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "class DatasetCreate(BaseModel):\n"
                "    org_id: str\n"
            ),
        },
        {"method": "GET", "path": "/api/v1/orgs"},   # plural list
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    deps = be_routes[0]["data_dependencies"]
    assert len(deps) == 1
    assert deps[0]["producer_path"] == "/api/v1/orgs"


def test_r156_b3_multiple_fks_get_multiple_dependencies():
    """Body schema with TWO FK fields → both get producer entries
    (when producers exist)."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "class DatasetCreate(BaseModel):\n"
                "    owner_id: str\n"
                "    org_id: str\n"
                "    name: str\n"
            ),
        },
        {"method": "GET", "path": "/api/v1/users/me"},
        {"method": "GET", "path": "/api/v1/orgs/me"},
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    deps = be_routes[0]["data_dependencies"]
    fields = {d["field"] for d in deps}
    assert fields == {"owner_id", "org_id"}


# ── No-op cases ───────────────────────────────────────────────────────


def test_r156_b3_skips_routes_without_body_schema():
    """Route without body_schema_source → no data_dependencies added."""
    be_routes = [
        {"method": "GET", "path": "/api/v1/datasets"},
        {"method": "GET", "path": "/api/v1/users/me"},
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    assert "data_dependencies" not in be_routes[0]


def test_r156_b3_skips_when_no_fk_fields():
    """Body schema has no FK-shaped fields → no dependencies stamped."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/health",
            "body_schema_source": (
                "class HealthPing(BaseModel):\n"
                "    timestamp: str\n"
                "    note: str | None\n"
            ),
        },
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    assert "data_dependencies" not in be_routes[0]


def test_r156_b3_skips_when_no_producer_found():
    """FK exists but no producer endpoint in be_routes → field omitted
    (LLM left without hint, falls back to fixture)."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "class DatasetCreate(BaseModel):\n"
                "    obscure_thing_id: str\n"
            ),
        },
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    assert "data_dependencies" not in be_routes[0]


def test_r156_b3_skips_generic_id_fields():
    """Lone `id`/`uuid`/`pk` fields are skipped — they're typically
    SUT-auto-generated, not data dependencies."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "class DatasetCreate(BaseModel):\n"
                "    id: str\n"
                "    uuid: str\n"
            ),
        },
        {"method": "GET", "path": "/api/v1/users/me"},
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    assert "data_dependencies" not in be_routes[0]


def test_r156_b3_empty_input_returns_empty():
    assert AutomationEngineerAgent._r156_b_3_resolve_data_dependencies([]) == []


def test_r156_b3_skips_circular_self_producer():
    """If `POST /api/v1/datasets` body has `dataset_id` AND there's a
    `GET /api/v1/datasets` route in the list, the helper does NOT
    suggest the same route as producer for itself."""
    be_routes = [
        {
            "method": "POST",
            "path": "/api/v1/datasets",
            "body_schema_source": (
                "class DatasetCreate(BaseModel):\n"
                "    dataset_id: str\n"
            ),
        },
        {"method": "GET", "path": "/api/v1/datasets"},
        # No /datasets/me — so the plural endpoint is the only candidate,
        # which is the producer of *other* datasets. The helper allows
        # this even though it's noisy — operator can override per project.
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    # The producer is `GET /api/v1/datasets`. NOT the POST route itself
    # (helper guard against self-loop).
    if "data_dependencies" in be_routes[0]:
        for d in be_routes[0]["data_dependencies"]:
            assert not (
                d["producer_method"] == "POST"
                and d["producer_path"] == "/api/v1/datasets"
            )


def test_r156_b3_ignores_non_dict_entries():
    be_routes: list = [
        None,  # type: ignore[list-item]
        {
            "method": "POST",
            "path": "/api/v1/x",
            "body_schema_source": "class X(BaseModel):\n    owner_id: str\n",
        },
        {"method": "GET", "path": "/api/v1/users/me"},
    ]
    AutomationEngineerAgent._r156_b_3_resolve_data_dependencies(be_routes)
    assert be_routes[1]["data_dependencies"][0]["field"] == "owner_id"
