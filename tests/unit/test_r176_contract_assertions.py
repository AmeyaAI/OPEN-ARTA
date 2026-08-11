"""R176 — contract tests assert CORRECTNESS (documented status), not latency.

Pre-R176 the contract generator emitted one item per declared response code,
each asserting that EXACT code + a hardcoded `responseTime < 5000ms`. So
assert-401/assert-404 items (sent with a happy-path request) failed on a 200,
and a correct-but-slow 200-in-9s failed the functional test. R176: one item per
operation asserting status ∈ documented-codes, with NO functional latency gate.
"""
from __future__ import annotations

import asyncio

import src.agents.contract_test_generator as ctg
from src.agents.contract_test_generator import _build_test_script, generate_contract_collection


def test_status_assertion_accepts_any_documented_code():
    js = "\n".join(_build_test_script("200", {}, documented_codes=["200", "401", "404"]))
    assert "to.include(pm.response.code)" in js
    assert "[200, 401, 404]" in js
    # exact-code assertion (the guaranteed-fail pattern) is gone
    assert "to.have.status(401)" not in js


def test_no_hardcoded_latency_fail():
    js = "\n".join(_build_test_script("200", {}, documented_codes=["200"]))
    assert "responseTime" not in js
    assert "5000" not in js


def test_one_item_per_operation_not_per_code(monkeypatch):
    spec = {
        "openapi": "3.0.0", "info": {"title": "API"},
        "paths": {
            "/widgets": {
                "get": {
                    "summary": "List widgets",
                    "responses": {"200": {"description": "ok"}, "401": {"description": "no"},
                                  "404": {"description": "missing"}},
                }
            }
        },
    }

    async def _fake_fetch(url, timeout=30.0):
        return spec
    monkeypatch.setattr(ctg, "_fetch_spec", _fake_fetch)
    coll = asyncio.get_event_loop().run_until_complete(
        generate_contract_collection("http://x/openapi.json", requirement_id="REQ-X"))
    items = coll.get("item") or []
    # 3 documented codes → pre-R176 would be 3 items; R176 = 1 item for the operation
    get_items = [it for it in items if (it.get("request") or {}).get("method", "").upper() == "GET"]
    assert len(get_items) == 1
    exec_js = "\n".join(get_items[0]["event"][0]["script"]["exec"])
    assert "to.include(pm.response.code)" in exec_js
