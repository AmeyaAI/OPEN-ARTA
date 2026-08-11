"""R145.E — chromium belt-and-suspenders defensive fix regression tests.

Pre-R145.E: chromium hit ERR_TIMED_OUT in Iter 4 (run-863889) for all
54 PW FAILs despite arta-api proving the SUT was reachable. R143.D
preflight returned `should_bridge=true` when invoked manually, but
without log evidence we cannot tell whether the env var was dropped
between dispatcher and subprocess OR chromium dropped the flag.

R145.E ships three independent defense layers:
  - Layer 1 (R143.D.2 belt): dispatcher-side env var TARGET_CHROMIUM_HOST_RESOLVER_RULES
  - Layer 2 (R145.E suspenders): TS-side dns.lookup in auth-setup.ts globalSetup;
    re-derives the rule when env var is absent
  - Layer 3 (R145.E resilience flags): chromium launch-args list extended
    with --disable-features=AsyncDns,EnableHTTP3,DnsOverHttps +
    --dns-prefetch-disable + --no-pings to bypass known chromium-side
    failure modes that --host-resolver-rules alone cannot heal

Killswitches:
  ARTA_R145_E_LAYER2_DNS_DISABLE=1   → skip Layer 2 dns.lookup
  ARTA_R145_E_RESILIENCE_FLAGS_DISABLE=1 → skip Layer 3 resilience flags

These tests verify SOURCE-FILE CONTENT (regression guards): if a future
refactor removes the layers OR breaks the killswitch wiring, the test
fails. Full integration testing requires Playwright chromium subprocess
spawn — out of scope for unit tests.
"""
from __future__ import annotations

from pathlib import Path

_PW_CONFIG = Path("src/automation/common/playwright.base.config.ts")
_AUTH_SETUP = Path("src/automation/common/auth-setup.ts")


def test_r145_e_layer1_resolver_rules_env_var_read():
    """R145.E Layer 1 — playwright.base.config.ts reads
    TARGET_CHROMIUM_HOST_RESOLVER_RULES at module load."""
    content = _PW_CONFIG.read_text()
    assert "TARGET_CHROMIUM_HOST_RESOLVER_RULES" in content
    assert "--host-resolver-rules=" in content


def test_r145_e_layer3_resilience_flags_concatenated_when_bridge_armed():
    """R145.E Layer 3 — when R143_D2_RESOLVER_RULES is set, launch-args
    MUST include the three resilience flags. Source-content regression
    check (full integration requires chromium subprocess)."""
    content = _PW_CONFIG.read_text()
    # All three resilience flags present in the source
    assert "--disable-features=AsyncDns,EnableHTTP3,DnsOverHttps" in content
    assert "--dns-prefetch-disable" in content
    assert "--no-pings" in content
    # Layer 3 marker comment
    assert "R145.E Layer 3" in content


def test_r145_e_layer3_resilience_killswitch_wired():
    """R145.E — ARTA_R145_E_RESILIENCE_FLAGS_DISABLE=1 must short-circuit
    Layer 3 flag injection (preserving operator escape hatch)."""
    content = _PW_CONFIG.read_text()
    assert "ARTA_R145_E_RESILIENCE_FLAGS_DISABLE" in content
    # Killswitch checked before flag concatenation
    assert "R145_E_RESILIENCE_DISABLED" in content


def test_r145_e_layer2_dns_lookup_in_auth_setup():
    """R145.E Layer 2 — auth-setup.ts globalSetup imports dns + promisify,
    pre-resolves the SUT host BEFORE chromium launches when the env var
    is absent."""
    content = _AUTH_SETUP.read_text()
    assert "import * as dns" in content
    assert "_r145_e_derive_resolver_rule" in content
    assert "promisify(dns.lookup)" in content
    # Layer 2 marker
    assert "R145.E Layer 2" in content


def test_r145_e_layer2_killswitch_wired():
    """R145.E — ARTA_R145_E_LAYER2_DNS_DISABLE=1 must short-circuit
    the TS-side DNS pre-resolution path."""
    content = _AUTH_SETUP.read_text()
    assert "ARTA_R145_E_LAYER2_DNS_DISABLE" in content


def test_r145_e_layer2_skips_when_dispatcher_already_set_env_var():
    """R145.E Layer 2 — when dispatcher has ALREADY set
    TARGET_CHROMIUM_HOST_RESOLVER_RULES (Layer 1 fired), Layer 2 logs
    that fact + does not re-derive. Operator visibility into which
    layer delivered."""
    content = _AUTH_SETUP.read_text()
    # Check that the logic gates Layer 2 on env var absence
    assert "if (!process.env.TARGET_CHROMIUM_HOST_RESOLVER_RULES)" in content
    # And logs the already-present case with a Layer 1 marker
    assert "[R145.E Layer2] dispatcher-side resolver rule already present" in content
