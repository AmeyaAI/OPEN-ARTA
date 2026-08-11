# ARTA Telemetry — the complete field list

ARTA ships an **anonymous, opt-out** usage ping so the project can answer
"how many installations exist and does the tool actually work for people" —
nothing else. This page is the contract: **everything ARTA can ever send is
listed here**, the client code is open at [`src/telemetry/`](../src/telemetry/),
and the schema allow-list ([`src/telemetry/schema.py`](../src/telemetry/schema.py))
structurally prevents transmitting anything not on this page.

## Disable it

```bash
ARTA_TELEMETRY=0
```

One env var. When disabled, ARTA makes **zero network calls and zero DNS
lookups** for telemetry — enforced by a unit test
([`tests/unit/test_telemetry_client.py`](../tests/unit/test_telemetry_client.py))
that fails the build if a disabled client touches a socket. Air-gapped
deployments: set it and forget it.

## What is sent (Tier 1 — anonymous, on by default)

Envelope on every event: random install UUID (`install_id` — generated once,
stored in `.arta/telemetry.json`, **not** derived from hostname/MAC/anything),
event name, timestamp, ARTA version, OS + arch, deploy method
(`docker|pip|source`), mode (`lite|full`).

| Event | Properties | Why |
|---|---|---|
| `installation.created` | deploy, mode | How many installations exist |
| `server.heartbeat` (weekly) | mode, uptime_bucket | Weekly-active installations |
| `project.created` | count_bucket | Multi-project depth |
| `requirements.imported` | count_bucket | Is ingestion used |
| `test.generated` | runtime, count_bucket, grounded_by | Core-loop usage |
| `validator.violation` | violation_kind (closed enum), runtime | Which hallucination classes occur in the wild — this directly improves the validators |
| `run.completed` | tools/total buckets, pass_rate_bucket, duration_bucket, sut_health_degraded | Does the full loop work |
| `telemetry.opted_out` | — | Honest denominators |

All counts are **buckets** (`0`, `1-9`, `10-49`, `50-199`, `200+`), never raw
numbers. All values are closed enums — free-form strings are dropped by the
schema before anything is queued.

## Tier 2 (opt-in only: `ARTA_TELEMETRY_EXTENDED=1`)

`healing.applied`, `gate.evaluated`, `ci.integration.created` (provider),
`llm.request` (provider *type* local|cloud, token bucket, cache hit),
`activation.first_passing_test` (minutes bucket). Same bucketing/enum rules.

## Never collected, under any tier

Source code · prompts · generated test content · SUT URLs, hostnames, or
endpoints · repository or project names · requirement text · credentials,
tokens, or keys · IP addresses (discarded at the gateway edge, geo resolved
to country only) · emails or names · any free-form text.

## Example payload (verbatim shape)

```json
{
  "install_id": "5f3c9a2e-....-....-....-............",
  "event": "test.generated",
  "ts": "2026-08-11T10:00:00Z",
  "version": "0.1.0",
  "os": "linux", "arch": "x86_64",
  "deploy": "docker", "mode": "lite", "tier": 1,
  "props": { "runtime": "newman", "count_bucket": "10-49" }
}
```

## Mechanics & retention

Events are batched, sent asynchronously with a 1-second timeout, and are
**fail-silent** — telemetry can never slow down or break a test run. After 3
consecutive delivery failures the client stops for the process lifetime.
Raw events are retained 90 days; only aggregates are kept beyond that.
Deletion requests: open an issue with your install ID. Aggregate statistics
are published back to the community periodically.

## North Star instrumentation note

The project's activation metric — *Weekly Verified Runs* (installations that
executed at least one generated test this week) — is computed entirely from
Tier-1 `server.heartbeat` ∩ `run.completed`. No identification is required
for the project to know whether it is useful.
