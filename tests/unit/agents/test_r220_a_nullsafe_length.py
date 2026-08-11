"""R220.A — null-safe `.length` / `toHaveLength()` rewriter.

Guards against the run-c3d56e crash cluster (63 PW fails:
`TypeError: Cannot read properties of undefined (reading 'length')`) where the
LLM reads `.length` on an invented response-envelope key the SUT omits.
"""
from src.agents.automation_engineer import AutomationEngineerAgent as A


def test_tohavelength_on_invented_key_is_guarded():
    out, n = A._r220_a_nullsafe_length(
        "expect(body.data.subscriptions).toHaveLength(0);")
    assert n == 1
    assert out == "expect((body.data.subscriptions) ?? []).toHaveLength(0);"


def test_bare_member_chain_length_is_guarded():
    out, n = A._r220_a_nullsafe_length(
        "expect(body.menuItems.length).toBeGreaterThanOrEqual(1);")
    assert n == 1
    assert "(body.menuItems ?? []).length" in out


def test_idempotent_already_guarded_untouched():
    src = "expect((body.x) ?? []).toHaveLength(2);"
    out, n = A._r220_a_nullsafe_length(src)
    assert n == 0 and out == src

    src2 = "const rows = (body?.items ?? []).length;"
    out2, n2 = A._r220_a_nullsafe_length(src2)
    assert n2 == 0 and out2 == src2


def test_single_member_length_left_alone():
    # `arr.length` (no invented envelope chain) is not the crash pattern; leaving
    # it avoids wrapping every safe array access.
    src = "const n = arr.length;"
    out, n = A._r220_a_nullsafe_length(src)
    assert n == 0 and out == src


def test_killswitch_disables():
    import os
    os.environ["ARTA_R220_A_NULLSAFE_LENGTH_DISABLE"] = "1"
    try:
        src = "expect(body.geofences).toHaveLength(3);"
        out, n = A._r220_a_nullsafe_length(src)
        assert n == 0 and out == src
    finally:
        del os.environ["ARTA_R220_A_NULLSAFE_LENGTH_DISABLE"]
