"""R123.A parity for the pytest/analytics lane — the undefined-symbol validator.
Catches a mangled/typo'd bare call (NameError) and a bad arta_runtime import
(ImportError) at GEN time, instead of letting it survive to a runtime crash."""
import os

from src.agents.grounding_validator import validate_pytest_undefined_symbols as V


def test_flags_bare_undefined_name():
    code = ("from arta_runtime import assert_well_formed\n"
            "def test_x():\n    assert_intNone_consistent(1)\n")
    v = V(code)
    assert any(x.kind == "undefined_symbol" and x.symbol == "assert_intNone_consistent" for x in v)


def test_flags_bad_arta_runtime_import():
    code = "from arta_runtime import totally_fake_helper\ndef test_x():\n    totally_fake_helper()\n"
    kinds = {(x.kind, x.symbol) for x in V(code)}
    assert ("undefined_import", "totally_fake_helper") in kinds


def test_clean_code_no_false_positives():
    # proper imports, builtins (len/range), scoped for-loop var, fixture param
    code = ("from arta_runtime import assert_well_formed\nimport pytest\n"
            "def test_ok(tmp_path):\n"
            "    rows = [1, 2, 3]\n"
            "    for r in rows:\n        assert_well_formed(r)\n"
            "    assert len(rows) == 3\n")
    assert V(code) == []


def test_syntax_error_is_not_our_job():
    assert V("def test(:\n  pass") == []   # syntax caught upstream, we return clean


def test_killswitch():
    os.environ["ARTA_PYTEST_UNDEF_DISABLE"] = "1"
    try:
        assert V("def t():\n    undefined_thing()\n") == []
    finally:
        del os.environ["ARTA_PYTEST_UNDEF_DISABLE"]
