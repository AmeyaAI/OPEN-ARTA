"""R330 P5 — Newman rows resolve to canonical test_cases ids via the AC token
in the item NAME (the data was always there; only PW had the map plumbing)."""
from src.api.routers.execution import _newman_canonical_test_id

CMAP = {("kui_261_api.json", 1): "TC-ABC-261-01",
        ("kui_261_api.json", 2): "TC-ABC-261-02"}


def test_resolves_ac_token():
    assert _newman_canonical_test_id(
        "kui_261_api.json", "AC-1 Happy Path - Get Menus", CMAP, "legacy-id"
    ) == "TC-ABC-261-01"


def test_ac_token_wins_over_trailing_digits():
    # 'AC-2 Get order 123' must resolve seq 2, not 123 (the last-number trap)
    assert _newman_canonical_test_id(
        "kui_261_api.json", "AC-2 Get order 123", CMAP, "legacy-id"
    ) == "TC-ABC-261-02"


def test_zero_padded_and_case_insensitive():
    assert _newman_canonical_test_id(
        "kui_261_api.json", "ac_001 lower-case variant", CMAP, "legacy-id"
    ) == "TC-ABC-261-01"


def test_req_prefixed_ac_token_uses_trailing_seq():
    assert _newman_canonical_test_id(
        "kui_607_api.json", "AC-607-01: Fetch org testcustomer",
        {("kui_607_api.json", 1): "TC-ABC-607-01"}, "legacy-id"
    ) == "TC-ABC-607-01"


def test_falls_back_when_no_token_or_unknown_key():
    assert _newman_canonical_test_id(
        "kui_261_api.json", "no token here", CMAP, "legacy-id") == "legacy-id"
    assert _newman_canonical_test_id(
        "other.json", "AC-1 x", CMAP, "legacy-id") == "legacy-id"
    assert _newman_canonical_test_id(
        "kui_261_api.json", "AC-9 unmapped seq", CMAP, "legacy-id") == "legacy-id"


def test_empty_map_is_safe():
    assert _newman_canonical_test_id("f.json", "AC-1 x", None, "legacy-id") == "legacy-id"
