# ARTA — the quality-evidence platform

> **AI now writes the code. ARTA is the independent layer that verifies it** —
> requirements-grounded test generation, execution across six runtimes, and
> truthful, audit-grade reporting of what your system actually does.
> **Default LLM: Ollama (local). No API keys, no cloud, no telemetry you can't read.**

Every AI testing tool claims it doesn't hallucinate. ARTA is the one that
shows you the code that enforces it: every generated test passes through
[grounding validators](src/agents/grounding_validator.py) that reject
selectors, roles, and endpoints that don't exist in your system, and every
failure is classified **`sut_regression`** (your bug) vs **`test_gen_bug`**
(our bug) vs **`grounding_blocked`** — so a red report means something.

## What ARTA does

```
Requirements (Jira / OpenAPI / Confluence / docs)
    → risk scoring (probability × impact)
    → ATDD Gherkin acceptance criteria
    → grounded test scripts        Playwright · Newman · k6 · ZAP · Axe · Pytest
    → execution + self-healing
    → truthful mission report      pass/fail attribution · traceability graph · quality gate
```

- **Grounded generation** — tests are generated against what discovery actually
  found (DOM catalog, captured endpoints, OpenAPI spec, source code), then
  validated against it. Hallucinations are rejected at generation time, retried
  with hints, and honestly BLOCKED when they can't be fixed — never silently shipped.
- **Six execution runtimes** — Playwright (UI), Newman (API), k6 (perf),
  OWASP ZAP (security), Axe (a11y), Pytest (analytics). Cypress and Selenium
  scripts can be *generated*; execution for them is not implemented yet.
- **Non-mutation guarantee** — discovery and testing never mutate your system by
  default, enforced independently at the network layer, generation time, and
  dispatch time. Destructive testing requires explicit opt-in markers *and* env vars.
- **Traceability** — requirement → acceptance criterion → test → execution →
  defect, as a graph you can query.
- **On-prem AI, verifiably** — Ollama by default, per-project provider config
  (Anthropic, OpenAI, Gemini, Azure), and an explicit refusal to silently fall
  back to a cloud endpoint when a provider is misconfigured.

## Quickstart

Prerequisite: [Ollama](https://ollama.com) running locally (`ollama pull qwen2.5:32b`),
or any supported cloud-LLM key.

```bash
git clone https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA.git
cd OPEN-ARTA
cp .env.example .env          # defaults are Ollama-local; add keys only if you want cloud LLMs
docker compose up -d
open http://localhost:38088   # UI  (API: http://localhost:38087)
```

First login: set `ARTA_BOOTSTRAP_ADMIN_EMAIL` + `ARTA_BOOTSTRAP_ADMIN_PASSWORD`
in `.env` before first start (creates the initial admin when the users table is
empty), or run a no-database demo with `ARTA_DEMO_MODE=1`
(`admin@arta.dev` / `demo1234` — never use in production).

Then: create a project → point it at your SUT's OpenAPI spec or base URL →
import requirements → **Generate** → **Run**. The fastest first win is the
API-test path from an OpenAPI spec.

## ARTA tests itself

This repo's own quality bar runs on ARTA's principles: a
[CI pipeline](.github/workflows/ci.yml) with hundreds of unit tests covering
the validators, generators, and dispatch pipeline, plus frontend type-checking.
The validator suite is the product — inspect it.

## Free forever

The entire single-team loop is Apache-2.0 and will never be metered or
crippled: generation for all six runtimes, execution, grounding validation,
self-healing, truthful reports, single-project traceability, local LLMs,
per-project RBAC and invites, CI templates. Org-level capabilities
(organizations, cross-project analytics, SAML/SCIM, audit trails, fleet) are
the commercial edition — the boundary is documented in [ee/README.md](ee/README.md).

## Telemetry, honestly

ARTA sends an anonymous, bucketed usage ping by default so we know whether the
tool works in the wild. **Every field is documented in
[docs/TELEMETRY.md](docs/TELEMETRY.md)**, the client is open code, free-form
strings are structurally impossible, and `ARTA_TELEMETRY=0` produces zero
network calls (there's a unit test that proves it).

## Documentation

- [Self-hosting infrastructure spec](docs/SELF_HOSTING_INFRASTRUCTURE.md) — sizing, profiles, security
- [Architecture](ARTA_ARCHITECTURE.md) — system design
- [Runbook](docs/RUNBOOK.md) — operations
- [Telemetry](docs/TELEMETRY.md) — the complete field list
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Trademarks](TRADEMARKS.md)

## License

Apache License 2.0 — see [LICENSE](LICENSE). © 2026 DPOD Labs Private Limited.
The `ee/` directory is reserved for the commercial edition (see [ee/README.md](ee/README.md)).
