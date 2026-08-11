"""Telemetry event schema — the single source of truth for what ARTA may send.

Every event and property is allow-listed here; anything not listed is DROPPED
at the client. Property values are restricted to closed enums and bucketed
counts — there is structurally no way to transmit source code, prompts, URLs,
requirement text, or any free-form string. The full field list is documented
publicly in docs/TELEMETRY.md and must be kept in sync with this module.
"""
from __future__ import annotations

# Bucketed-count labels (never raw numbers above 9)
_BUCKETS = ("0", "1-9", "10-49", "50-199", "200+")

# Closed enums
_RUNTIMES = {"playwright", "newman", "k6", "zap", "axe", "pytest"}
_SOURCE_TYPES = {"jira", "github", "openapi", "confluence", "docx", "xlsx", "pdf", "md", "text"}
_GROUNDED_BY = {"openapi", "dom", "network", "source", "requirement", "none"}
_PROVIDER_TYPES = {"local", "cloud"}
_DEPLOY = {"docker", "pip", "source"}
_MODES = {"lite", "full"}
_VIOLATION_KINDS = {
    "unknown_endpoint", "hallucinated_role", "hallucinated_role_name",
    "bad_playwright_api", "hook_use_misuse", "request_factory_misuse",
    "pw_syntax_error", "undefined_symbol", "destructive_test_pattern", "other",
}
_GATE_DECISIONS = {"pass", "concerns", "block"}

# event -> {prop: allowed-values set, or "bucket"}
TIER1_EVENTS: dict[str, dict[str, object]] = {
    "installation.created": {"deploy": _DEPLOY, "mode": _MODES},
    "server.heartbeat": {"mode": _MODES, "uptime_bucket": set(_BUCKETS)},
    "project.created": {"count_bucket": set(_BUCKETS)},
    "requirements.imported": {"source_type": _SOURCE_TYPES, "count_bucket": set(_BUCKETS)},
    "test.generated": {"runtime": _RUNTIMES, "count_bucket": set(_BUCKETS), "grounded_by": _GROUNDED_BY},
    "validator.violation": {"violation_kind": _VIOLATION_KINDS, "runtime": _RUNTIMES},
    "run.completed": {
        "tools_bucket": set(_BUCKETS), "total_bucket": set(_BUCKETS),
        "pass_rate_bucket": {"0-24", "25-49", "50-74", "75-100"},
        "duration_bucket": {"<5m", "5-30m", "30-90m", ">90m"},
        "sut_health_degraded": {"true", "false"},
    },
    "telemetry.opted_out": {},
}

# Tier-2 (opt-in) events — defined for schema completeness, gated by the client.
TIER2_EVENTS: dict[str, dict[str, object]] = {
    "healing.applied": {"accepted": {"true", "false"}},
    "gate.evaluated": {"decision": _GATE_DECISIONS},
    "ci.integration.created": {"provider": {"github", "gitlab", "jenkins", "azure", "circle"}},
    "llm.request": {"provider_type": _PROVIDER_TYPES, "tokens_bucket": set(_BUCKETS), "cache_hit": {"true", "false"}},
    "activation.first_passing_test": {"minutes_bucket": {"<10", "10-30", "30-120", ">120"}, "runtime": _RUNTIMES},
}


def bucket(n: int) -> str:
    """Coarse count bucket — the only way a count leaves the machine."""
    if n <= 0:
        return "0"
    if n < 10:
        return "1-9"
    if n < 50:
        return "10-49"
    if n < 200:
        return "50-199"
    return "200+"


def validate(event: str, props: dict | None, *, tier2_enabled: bool = False) -> dict | None:
    """Return sanitized props for an allow-listed event, else None (drop).

    Unknown events are dropped. Unknown props are dropped. Prop values that
    are not in the closed enum for that prop are dropped. The result contains
    only strings drawn from this module's enums.
    """
    schema = TIER1_EVENTS.get(event)
    if schema is None and tier2_enabled:
        schema = TIER2_EVENTS.get(event)
    if schema is None:
        return None
    clean: dict[str, str] = {}
    for key, value in (props or {}).items():
        allowed = schema.get(key)
        if allowed is None:
            continue
        sval = str(value).lower() if isinstance(value, bool) else str(value)
        if isinstance(allowed, set) and sval in allowed:
            clean[key] = sval
    return clean
