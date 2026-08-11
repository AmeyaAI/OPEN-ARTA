"""R164 — exclude/GC derived run-scoped newman artifacts from dispatch.

R159 wrote cookie/auth-injected collection copies into the SOURCE newman dir
and never cleaned them; each run re-globbed prior runs' copies, ballooning the
dispatch denominator (94→128→194) and cross-contaminating results. R164
classifies derived artifacts so they're excluded from the glob AND GC'd.
"""
from __future__ import annotations

from src.api.routers.execution import _r164_is_derived_newman_artifact as derived


def test_source_collections_are_not_derived():
    for n in (
        "req_am_001_api.json",
        "req_am_009_api.json",
        "req_am_001_chain_5.postman_collection.json",
        "req_am_001_chain_1_adv.postman_collection.json",
    ):
        assert derived(n) is False, n


def test_r159_cookie_and_injected_copies_are_derived():
    assert derived("req_am_001_api_run-3dc291_cookie.json") is True
    assert derived("req_am_001_chain_3.postman_collection_run-846363_cookie_r159_run-3dc291_cookie.json") is True
    assert derived("req_am_007_api_r159.json") is True


def test_r29_filtered_sidecars_are_derived():
    assert derived("req_am_001_api_r29_filtered.json") is True


def test_run_stamped_artifacts_are_derived():
    assert derived("anything_run-846363_cookie.json") is True
    assert derived("foo_run-abc123.json") is True
