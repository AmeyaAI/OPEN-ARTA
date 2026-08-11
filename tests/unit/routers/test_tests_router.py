"""F7-2 regression: pin URL contract after the tests.py split.

These tests would have caught the route-ordering bug where adding
include_router LATER than `GET /{test_id}` made `/pending-reviews` match
the catch-all and 404. They also verify the extracted sub-routers
(fixtures, versions, review) are reachable.
"""
from __future__ import annotations

import pytest

# F9-6: Requires live Postgres — auto-skipped when DB unreachable (see tests/conftest.py).
pytestmark = pytest.mark.integration


class TestTestsRouterAfterSplit:

    async def test_list_tests(self, test_app):
        resp = await test_app.get("/api/tests")
        assert resp.status_code == 200
        body = resp.json()
        assert "tests" in body
        assert "total" in body

    async def test_get_specific_test_by_id(self, test_app):
        # TC-124 is in the seed extended into tests_state.GENERATED_TESTS.
        # When a real DB is available the endpoint returns the DB row shape
        # (id=row UUID, test_id=TC-124); without DB it returns the seed shape
        # (id=TC-124). Either way the response must include the test_id.
        resp = await test_app.get("/api/tests/TC-124")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("test_id") == "TC-124" or body.get("id") == "TC-124", \
            f"response should identify TC-124, got: {body!r}"

    async def test_pending_reviews_route_is_specific_not_catchall(self, test_app):
        """F7-2 regression: this URL must hit tests_review.py's
        list_pending_reviews, not tests.py's `GET /{test_id}` catch-all."""
        resp = await test_app.get("/api/tests/pending-reviews")
        assert resp.status_code == 200
        body = resp.json()
        # Sub-router returns a list of review dicts (or empty), NOT a single
        # test (which would be the catch-all 404 / 200 test object shape).
        assert isinstance(body, list), \
            f"pending-reviews must return a list, got {type(body).__name__}: {body}"

    async def test_extracted_fixtures_endpoint_reachable(self, test_app):
        """tests_fixtures.py mounted via include_router."""
        resp = await test_app.get("/api/tests/TC-124/data")
        assert resp.status_code == 200

    async def test_extracted_versions_endpoint_reachable(self, test_app):
        """tests_versions.py mounted via include_router."""
        resp = await test_app.get("/api/tests/TC-124/versions")
        assert resp.status_code == 200
        body = resp.json()
        assert "versions" in body
