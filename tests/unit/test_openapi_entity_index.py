"""Data-Object entity index — the domain entity (OpenAPI component-schema $ref)
per operation, feeding the Req→…→API→Data traceability spine."""
from src.agents.sut_topology import openapi_entity_index

ENTITY_SPEC = {"paths": {
    "/widgets": {
        # write → prefer requestBody entity
        "post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/WidgetDto"}}}},
            "responses": {"200": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/WidgetResult"}}}}}},
        # read → response entity, unwrapping a top-level array
        "get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Widget"}}}}}}},
    },
    # Swagger-2 body param + responses.200.schema
    "/orgs": {"put": {
        "parameters": [{"in": "body", "schema": {"$ref": "#/definitions/Organization"}}],
        "responses": {"200": {"schema": {"$ref": "#/definitions/OrgResult"}}}}},
    "/health": {"get": {"responses": {"200": {"description": "ok"}}}},  # no entity
}}


def test_openapi_entity_index():
    idx = openapi_entity_index(ENTITY_SPEC)
    assert idx["POST:/widgets"] == "WidgetDto"      # write prefers requestBody
    assert idx["GET:/widgets"] == "Widget"          # read unwraps array items
    assert idx["PUT:/orgs"] == "Organization"       # swagger-2 body param
    assert "GET:/health" not in idx                 # no schema ref → absent


def test_openapi_entity_index_empty():
    assert openapi_entity_index({}) == {}
    assert openapi_entity_index({"paths": {"/x": {"get": {}}}}) == {}
