"""R220.B — repoint hardcoded `storageState: 'auth.json'` (ENOENT) at the real
dispatch path (run-d70e1f: 42 PW fails from a relative auth-state filename)."""
from src.agents.automation_engineer import AutomationEngineerAgent as A


def test_relative_authjson_repointed():
    out, n = A._r220_b_fix_hardcoded_storagestate("      storageState: 'auth.json'")
    assert n == 1 and "process.env.TARGET_AUTH_STATE_PATH" in out and "auth.json" not in out


def test_variants_repointed():
    for src in ("storageState: \"auth-state.json\"", "storageState: './auth.json'"):
        out, n = A._r220_b_fix_hardcoded_storagestate(src)
        assert n == 1 and "TARGET_AUTH_STATE_PATH" in out


def test_env_ref_idempotent():
    src = "storageState: process.env.TARGET_AUTH_STATE_PATH"
    out, n = A._r220_b_fix_hardcoded_storagestate(src)
    assert n == 0 and out == src


def test_undefined_untouched():
    src = "storageState: undefined"
    out, n = A._r220_b_fix_hardcoded_storagestate(src)
    assert n == 0 and out == src


def test_killswitch():
    import os
    os.environ["ARTA_R220_B_STORAGESTATE_DISABLE"] = "1"
    try:
        out, n = A._r220_b_fix_hardcoded_storagestate("storageState: 'auth.json'")
        assert n == 0
    finally:
        del os.environ["ARTA_R220_B_STORAGESTATE_DISABLE"]
