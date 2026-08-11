"""R295 — wire build_request_bodies into read-POST gen (the body-hint seam).

Critical-review finding: `build_request_bodies` (R211) synthesizes request
bodies but was wired ONLY into the destructive-action chain path (tests.py,
gated on the R154 opt-in). A read-POST like `getDataForCommandCenter` never got
a body, so the LLM emitted `{ data: {} }` and the endpoint 500'd.

R295 injects the synthesized body into the API-contract gen prompt. It is also
the integration seam for the planned DTO-from-source extractor: that adds a 4th
body source and flows through this block with no new injection path.

Two correctness properties this pins:
  - EMPTY bodies (`{}`/`[]`/`[{}]`) are NOT injected — a `{}` hint would
    authoritatively tell the LLM to send an empty body, the exact 500 we fix.
  - `known_ids` is a DICT (known_ids_for_chain), not a set (all_real_id_values)
    — the wrong-type-slips-through bug R285 shipped.
"""
from src.agents.automation_engineer import AutomationEngineerAgent as A


def _block(monkeypatch, bodies):
    """Render the R295 block with build_request_bodies mocked to `bodies`."""
    import src.agents.test_data as td
    monkeypatch.setattr(td, "build_request_bodies", lambda **k: bodies)
    import src.agents.api_discovery as ad
    monkeypatch.setattr(ad, "_load_captured_endpoints", lambda p: [])
    a = A.__new__(A)
    return A._r295_request_body_block(
        a, "p", "reefer command center report account training file")


def test_non_empty_body_is_injected(monkeypatch):
    out = _block(monkeypatch, {"POST /training/api/addFile": {"file": "x"}})
    assert "[HARD CONSTRAINT — REQUEST BODY]" in out
    assert '"file"' in out
    assert "/training/api/addFile" in out


def test_empty_body_is_not_injected(monkeypatch):
    """A `{}` hint is worse than none — it tells the LLM to send the 500-body."""
    out = _block(monkeypatch, {"POST /training/api/saveOrder": {}})
    assert out == "", "empty synthesized body must produce no hint"


def test_empty_list_and_list_of_empty_not_injected(monkeypatch):
    out = _block(monkeypatch, {"PUT /a/b": [], "PUT /a/c": [{}]})
    assert out == ""


def test_mixed_keeps_only_the_real_body(monkeypatch):
    out = _block(monkeypatch, {
        "POST /training/api/addFile": {"file": "x"},   # real -> kept
        "POST /training/api/saveOrder": {},            # empty -> dropped
    })
    lines = [l for l in out.splitlines() if "->" in l]
    assert len(lines) == 1 and "addFile" in lines[0]


def test_no_bodies_is_empty_block(monkeypatch):
    assert _block(monkeypatch, {}) == ""


def test_known_ids_uses_the_dict_accessor_not_the_set():
    """Pin the R285-class fix: the source must call the dict-returning
    known_ids_for_chain, never the set-returning all_real_id_values."""
    import inspect
    src = inspect.getsource(A._r295_request_body_block)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "known_ids_for_chain" in code
    assert "all_real_id_values" not in code


def test_wired_into_the_gen_prompt():
    import inspect
    src = inspect.getsource(A)
    assert "self._r295_request_body_block(" in src
    assert 'ARTA_R295_BODY_HINT_DISABLE' in src
