"""R213.K.9 — ground HARDCODED stale tenant ids in LLM k6 paths to ${__ENV.<CLAIM>}
so dispatch fills LIVE ids. The {value→claim} map is learned from the spec's OWN
baked JWT (generic — no SUT keyword table). The base64 JWT itself is untouched.
"""
from __future__ import annotations

import base64
import json

from src.agents.chain_aware_k6 import ground_k6_path_ids


def _jwt(claims: dict) -> str:
    p = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{p}.sig"


CLAIMS = {
    "account_id": "0aee6bd7-ed42-4184-9bac-ce0466737ada",
    "subscriber_id": "6551f605-39cb-4351-8ea1-b2a7af317985",
    "subscription_id": "a6a49ce0-1121-455c-91cd-7956eb0891dd",
    "organization_id": "424e744f-94a5-4aae-b1ae-f24719f1a426",
}
JWT = _jwt(CLAIMS)
SPEC = (
    f"const AUTH_TOKEN = __ENV.AUTH_TOKEN || '{JWT}';\n"
    "const u = artaPost(`${BASE_URL}/api/media/0aee6bd7-ed42-4184-9bac-ce0466737ada"
    "/subscriber/6551f605-39cb-4351-8ea1-b2a7af317985"
    "/subscription/a6a49ce0-1121-455c-91cd-7956eb0891dd/file`, b, p);\n"
    "const o = artaGet(`${BASE_URL}/organization/424e744f-94a5-4aae-b1ae-f24719f1a426/x`, p);\n"
)


def test_grounds_path_ids_from_baked_jwt():
    out, n = ground_k6_path_ids(SPEC)
    assert n == 4
    assert "/api/media/${__ENV.ACCOUNT_ID}/" in out
    assert "/subscriber/${__ENV.SUBSCRIBER_ID}/" in out
    assert "/subscription/${__ENV.SUBSCRIPTION_ID}/" in out
    assert "/organization/${__ENV.ORGANIZATION_ID}/" in out


def test_jwt_base64_left_intact():
    out, _ = ground_k6_path_ids(SPEC)
    assert JWT in out, "the baked JWT (base64) must not be mangled — only path segments"


def test_noop_without_jwt():
    assert ground_k6_path_ids("const u = artaGet(`${BASE_URL}/api/x`, p);") == (
        "const u = artaGet(`${BASE_URL}/api/x`, p);", 0,
    )


def test_killswitch(monkeypatch):
    monkeypatch.setenv("ARTA_K6_PATH_ID_GROUNDING_DISABLE", "1")
    assert ground_k6_path_ids(SPEC) == (SPEC, 0)


def test_non_id_uuid_values_not_substituted():
    # a JWT claim that is NOT an id-role (e.g. a random nonce) is ignored
    out, n = ground_k6_path_ids(
        f"const t = '{_jwt({'nonce': 'deadbeef-0000-0000-0000-000000000000'})}';\n"
        "artaGet(`${BASE_URL}/x/deadbeef-0000-0000-0000-000000000000`, p);"
    )
    assert n == 0  # nonce isn't in _R213_K9_ID_CLAIMS
