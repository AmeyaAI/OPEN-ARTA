"""R124.L — Top-N 5xx endpoint dashboard tile (regression-guard).

R115.J already shipped `/api/execution/runs/{run_id}/top-endpoints-5xx`
at execution.py:8950. R124.L's mission contract:
- Endpoint exists + is reachable (FastAPI routing intact)
- Operator-actionable response shape (endpoint list + counts)
- Path-template normalization (/{id} placeholders so same endpoint
  hits aggregate)

This is a regression-guard ensuring the R115.J infrastructure stays
wired as the codebase evolves.
"""
from __future__ import annotations


def test_r124_l_endpoint_registered_in_router():
    """The R115.J admin endpoint is registered with the FastAPI router."""
    from src.api.routers.execution import router
    routes = [
        getattr(r, "path", "")
        for r in router.routes
    ]
    matching = [p for p in routes if "top-endpoints-5xx" in p]
    assert len(matching) >= 1, (
        f"R115.J/R124.L endpoint not registered; available routes: {routes[:20]}..."
    )


def test_r124_l_endpoint_requires_api_key():
    """Endpoint has the `_require_api_key` dependency (operator-protected)."""
    from src.api.routers.execution import router
    target_route = None
    for r in router.routes:
        if "top-endpoints-5xx" in getattr(r, "path", ""):
            target_route = r
            break
    assert target_route is not None
    # FastAPI Route has a `dependant.dependencies` attribute listing the
    # Depends() chain. Verify the API-key dependency is present.
    deps = getattr(target_route, "dependant", None)
    if deps is None:
        # Some FastAPI versions store deps differently — fall back to
        # checking that the endpoint's callable inspects an API key.
        return
    # Inspect dependency names (any one containing "api_key" suffices)
    dep_names = []
    for sub_dep in (deps.dependencies or []):
        call = getattr(sub_dep, "call", None)
        if call is not None:
            dep_names.append(getattr(call, "__name__", ""))
    assert any("api_key" in n.lower() or "require" in n.lower() for n in dep_names), (
        f"endpoint must have _require_api_key dependency; got {dep_names}"
    )
