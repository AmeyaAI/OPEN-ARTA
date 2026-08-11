"""R213.K.12 — ground hardcoded tenant-id VALUES (var assignments + inline path
literals) to ${__ENV.<ROLE>} using a {value→role} map learned from captured
endpoint path POSITIONS (leading uuid → account; keyword-preceded → that role).
Complements K.9 (JWT-decode) which misses `const X = '<uuid>'` var assignments.
"""
from __future__ import annotations

from src.agents.chain_aware_k6 import ground_k6_id_values, _r213_k12_value_role_map

CAPS = [
    {"path": "/0aee6bd7-ed42-4184-9bac-ce0466737ada/api/collection/x/user/private/cm/v1/y"},
    {"path": "/acct/sub/subscriber/6551f605-39cb-4351-8ea1-b2a7af317985/blob"},
    {"path": "/acct/organization/424e744f-94a5-4aae-b1ae-f24719f1a426/services"},
]


def test_value_role_map_from_captured_positions():
    m = _r213_k12_value_role_map(CAPS)
    assert m["0aee6bd7-ed42-4184-9bac-ce0466737ada"] == "ACCOUNT_ID"      # leading uuid
    assert m["6551f605-39cb-4351-8ea1-b2a7af317985"] == "SUBSCRIBER_ID"   # after /subscriber/
    assert m["424e744f-94a5-4aae-b1ae-f24719f1a426"] == "ORGANIZATION_ID"  # after /organization/


def test_grounds_var_assignment_bare_no_quotes():
    # the misleading-name case: collectionId holds an ACCOUNT value
    spec = "const collectionId = '0aee6bd7-ed42-4184-9bac-ce0466737ada';"
    out, n = ground_k6_id_values(spec, CAPS)
    assert n == 1
    # bare __ENV (quotes would break: '${__ENV.X}' is a literal, not interpolated)
    assert out == "const collectionId = __ENV.ACCOUNT_ID;"


def test_grounds_inline_path_literal():
    spec = "artaGet(`${baseUrl}/api/x/subscriber/6551f605-39cb-4351-8ea1-b2a7af317985/y`, p);"
    out, n = ground_k6_id_values(spec, CAPS)
    assert n == 1
    assert "/subscriber/${__ENV.SUBSCRIBER_ID}/y`" in out


def test_no_map_no_change():
    spec = "const x = '0aee6bd7-ed42-4184-9bac-ce0466737ada';"
    assert ground_k6_id_values(spec, []) == (spec, 0)


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_K6_PATH_ID_GROUNDING_DISABLE", "1")
    spec = "const collectionId = '0aee6bd7-ed42-4184-9bac-ce0466737ada';"
    assert ground_k6_id_values(spec, CAPS) == (spec, 0)
