"""pytest configuration for ARTA-generated analytics tests.

Adds this directory to sys.path so generated tests can resolve
`from arta_runtime import analytics_client` and
`from src.automation.python_tests.analytics_helpers import ...` without per-spec
PYTHONPATH gymnastics. The execution router invokes pytest per-file from the
repo root, which would otherwise leave arta_runtime/ unresolvable.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[2]

for _p in (str(_HERE), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def pytest_configure(config):
    """R17b — register markers locally so pytest doesn't warn even when
    invoked without `-c /app/pytest.ini`. Mirrors the markers section
    in the repo-root pytest.ini for portability — when ARTA's runner
    misses the -c flag (older code path, ad-hoc operator invocation),
    tests still recognize @pytest.mark.tier1/tier2/tier3/analytics/etc.
    instead of raising PytestUnknownMarkWarning (which Python 3.12 +
    recent pytest can treat as a test failure)."""
    for _line in (
        "tier1: F5-7 analytics tier 1 — fast, every commit (≤30s, no LLM)",
        "tier2: F5-7 analytics tier 2 — PR gate (≤5min, mocked LLM)",
        "tier3: F5-7 analytics tier 3 — nightly (~60min, full LLM judge)",
        "analytics: F5-7 analytics suite — generated tests for analytics agents",
        "adversarial: F5-7 analytics adversarial suite — robustness probes",
        "unit: unit tests",
        "integration: integration tests (require services)",
    ):
        config.addinivalue_line("markers", _line)


def pytest_collection_modifyitems(config, items):
    """R72.6 safety net — inject `analytics_client` + `assert_adversarial_handled`
    + `tolerant_assert` into every test module's namespace so existing specs
    (generated before the R72.6 prompt fix added explicit imports) don't
    raise NameError at runtime. Going forward the LLM emits the imports
    directly; this conftest hook keeps the 224 already-on-disk specs
    running without a full regen sweep.

    Idempotent: only injects when the name is absent from the module
    globals. Modules with explicit imports are unaffected.
    """
    try:
        from arta_runtime import (  # noqa: F401
            analytics_client as _ac,
            tolerant_assert as _ta,
            assert_adversarial_handled as _aah,
            assert_well_formed as _awf,
            assert_grounded as _agr,
            assert_internally_consistent as _aic,
            assert_analytics_correct as _aacr,
            verify_insight_properties as _vip,
        )
    except Exception:
        return  # arta_runtime not importable — leave items alone
    _injectable = {
        "analytics_client": _ac,
        "tolerant_assert": _ta,
        "assert_adversarial_handled": _aah,
        # G2 (R218) — invariant assertions for ungrounded recipes.
        "assert_well_formed": _awf,
        "assert_grounded": _agr,
        "assert_internally_consistent": _aic,
        # AN4 (R218) — analytics correctness on controlled data.
        "assert_analytics_correct": _aacr,
        "verify_insight_properties": _vip,
    }
    seen_modules: set = set()
    for item in items:
        mod = getattr(item, "module", None)
        if mod is None or id(mod) in seen_modules:
            continue
        seen_modules.add(id(mod))
        for name, value in _injectable.items():
            if not hasattr(mod, name):
                setattr(mod, name, value)
