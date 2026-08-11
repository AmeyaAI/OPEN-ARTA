"""R130.F — Dead `load_dom_catalog` removal regression guard.

One case: the orphan function in `grounding_validator.py` is GONE.
The single source of truth is now `api_discovery.load_dom_catalog`
(returns `{}` on miss; 7 production callers).

This test is a REGRESSION guard — if a future refactor reintroduces
the orphan, this test fails and forces the author to instead import
from `api_discovery`.
"""
from __future__ import annotations

from src.agents import grounding_validator
from src.agents import api_discovery


def test_r130f_grounding_validator_has_no_load_dom_catalog():
    """grounding_validator.load_dom_catalog MUST NOT exist after R130.F.
    Single source of truth is api_discovery.load_dom_catalog."""
    assert not hasattr(grounding_validator, "load_dom_catalog"), (
        "R130.F regression: orphan `load_dom_catalog` reintroduced into "
        "grounding_validator.py. Import from api_discovery instead "
        "(single source of truth — returns empty dict on miss, NOT None)."
    )
    # Sanity: the production loader is still present
    assert hasattr(api_discovery, "load_dom_catalog"), (
        "api_discovery.load_dom_catalog (production loader) MUST still exist"
    )
