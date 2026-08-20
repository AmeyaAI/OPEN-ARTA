---
title: Installation
description: The full ARTA installation — Docker Compose services, environment configuration, LLM providers, first login, and telemetry controls.
---

This page covers everything the [quickstart](/OPEN-ARTA/docs/getting-started/)
glossed over. The single source of truth for configuration is
[`.env.example`](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/.env.example)
in the repository — every variable is documented inline there.

## What `docker compose up -d` starts

| Service | Purpose |
| --- | --- |
| `arta-api` | The ARTA engine — FastAPI backend, agents, dispatch pipeline |
| `arta-frontend` | The dashboard — Next.js 14 |
| `postgres` | Primary database |
| `neo4j` | Traceability graph (requirement → test → result) |
| `redis` | Queues and pub/sub between pipeline stages |
| `chromadb` | Vector store for retrieval during generation |
| `zap` | OWASP ZAP daemon for security scans |

Default host ports: UI on **38088**, API on **38087** (the API container's
internal port stays 8000; override the host mapping with `ARTA_API_HOST_PORT`
if 38087 clashes on your machine).

## Configure the LLM provider

ARTA defaults to a local model — no key required:

```bash
ARTA_LLM_PROVIDER=ollama
ARTA_LLM_MODEL=qwen2.5:32b
OLLAMA_BASE_URL=http://localhost:11434
```

Supported providers are `ollama`, `anthropic`, `google_gemini`, and `openai` —
set the matching API key variable (for example `ANTHROPIC_API_KEY`) when using
a cloud provider. Providers can also be configured per project in the UI.

ARTA explicitly refuses to silently fall back to a cloud endpoint when a
provider is misconfigured — a misconfiguration is an error, not a quiet
data-residency violation.

## Secrets and first login

- `DATABASE_URL` is the primary secret (it contains the Postgres password);
  keep it in sync with `POSTGRES_PASSWORD` when you rotate.
- `ARTA_API_KEY` protects admin endpoints — change it from the placeholder.
- Bootstrap the first admin account with `ARTA_BOOTSTRAP_ADMIN_EMAIL` +
  `ARTA_BOOTSTRAP_ADMIN_PASSWORD` (created only when the users table is empty;
  unset after first login), or use `ARTA_DEMO_MODE=1` for a no-database demo
  (`admin@arta.dev` / `demo1234` — never in production).
- Never commit `.env` to source control.

## Telemetry

ARTA sends an anonymous, bucketed usage ping by default. Every field is
documented in
[docs/TELEMETRY.md](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/docs/TELEMETRY.md),
free-form strings are structurally impossible, and:

```bash
ARTA_TELEMETRY=0
```

produces **zero network calls** — there is a unit test that proves it. This
makes air-gapped deployment a supported configuration, not a workaround.

## Production self-hosting

For sizing, deployment profiles, and security hardening, read the
[self-hosting infrastructure spec](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/docs/SELF_HOSTING_INFRASTRUCTURE.md)
and the [runbook](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/docs/RUNBOOK.md).
The repo also ships `docker-compose.prod.yml` as a production-oriented overlay.
