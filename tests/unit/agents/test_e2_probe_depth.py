"""E2 — probe-depth config (read tokens, XHR-replay filter, capture cap).

Secondary lever (runtime enrichment for endpoints the SUT publishes no doc for).
Everything strictly within R154.A: the read tokens are NOUNS only (never
mutation verbs), the XHR replay stays GET-only, and the caps are env-reversible.
"""
import re

import pytest


_PROBE = "src/automation/playwright/discovery_probe.spec.ts"

# The mutation verbs that must NEVER appear as read tokens (a clicked mutation
# button would change SUT state — R154 forbids it).
_MUTATION_VERBS = [
    "create", "generate", "submit", "save", "delete", "update", "run",
    "execute", "add", "remove", "edit", "insert", "post", "put", "destroy",
    "purge", "wipe", "reset", "cancel", "approve", "reject", "send",
]


def _read_tokens() -> list[str]:
    src = open(_PROBE).read()
    m = re.search(r"_R154_A_READ_TOKENS:\s*string\[\]\s*=\s*\[(.*?)\];", src, re.S)
    assert m, "read-token array not found"
    return re.findall(r"'([a-z]+)'", m.group(1))


def test_e2a_adds_business_nouns():
    toks = _read_tokens()
    for noun in ("lease", "manager", "billing", "owner"):
        assert noun in toks, f"{noun} missing from read tokens"


def test_e2a_read_tokens_exclude_every_mutation_verb():
    """The load-bearing safety check: no token is a mutation verb."""
    toks = set(_read_tokens())
    leaked = toks & set(_MUTATION_VERBS)
    assert leaked == set(), f"mutation verbs leaked into read allowlist: {leaked}"


def test_e2a_tokens_gated_by_killswitch():
    src = open(_PROBE).read()
    assert "ARTA_R154_TOKENS_EXTRA_DISABLE" in src


def test_e2c_family_filter_admits_blocked_families():
    # A6 — familyRe is now config-overridable (`_cfgKw.length ? new RegExp(...) :
    # /(...)/i`); the built-in business-family list lives in the FALLBACK regex
    # literal. Its intent (lease/geofence/command still admitted) is unchanged.
    src = open(_PROBE).read()
    m = re.search(r"/\((lease\|geofence\|command[^)]*)\)/i", src)
    assert m, "family fallback regex not found"
    fams = m.group(1).split("|")
    for f in ("lease", "geofence", "command"):
        assert f in fams


def test_a6_replay_filter_has_structural_default_and_config_override():
    # A6 platform/SUT-separation: SUT-agnostic structural rule is the default match,
    # and the family vocabulary is overridable per-SUT via config (no vocab hardcoded
    # as the only path).
    src = open(_PROBE).read()
    assert "_structuralList" in src, "structural default rule missing"
    assert "ARTA_R151B_REPLAY_KEYWORDS" in src, "per-SUT keyword override missing"


def test_e2c_family_filter_still_admits_analytics():
    """Superset — the original analytics regex must remain."""
    src = open(_PROBE).read()
    assert "analyticsRe.test(p) || (broaden && familyRe.test(p))" in src
    assert "insight|pipeline|dashboard" in src


def test_e2c_replay_stays_get_only():
    """R154 defense: the direct-XHR replay must remain GET-only."""
    src = open(_PROBE).read()
    # the loop guards `if (method !== 'GET') continue;`
    assert "method !== 'GET'" in src


def test_e2c_filter_broaden_killswitch():
    src = open(_PROBE).read()
    assert "ARTA_R151B_FILTER_BROADEN_DISABLE" in src


# ── the capture cap (code) ──────────────────────────────────────────────────

def test_e2_capture_cap_env_tunable(tmp_path, monkeypatch):
    import src.agents.api_discovery as ad
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    monkeypatch.setenv("ARTA_CAPTURED_CAP", "3")
    eps = [{"method": "GET", "path": f"/p/{i}", "source": "network"} for i in range(10)]
    ad.save_captured_endpoints("pid-cap", eps)
    import json
    saved = json.loads((tmp_path / "pid-cap.json").read_text())
    assert len(saved) == 3


def test_e2_capture_cap_defaults_to_500(tmp_path, monkeypatch):
    import src.agents.api_discovery as ad
    monkeypatch.setattr(ad, "_CAPTURED_DIR", tmp_path)
    monkeypatch.delenv("ARTA_CAPTURED_CAP", raising=False)
    eps = [{"method": "GET", "path": f"/p/{i}", "source": "network"} for i in range(5)]
    ad.save_captured_endpoints("pid-cap2", eps)
    import json
    saved = json.loads((tmp_path / "pid-cap2.json").read_text())
    assert len(saved) == 5  # under the 500 default, all kept
