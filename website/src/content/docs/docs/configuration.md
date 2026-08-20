---
title: Configuration
description: Every ARTA configuration surface — environment variables for LLM providers, databases, authentication, telemetry, and integrations.
---

All configuration is environment variables, and the single source of truth is
[`.env.example`](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/.env.example)
— every variable is documented inline where it is defined. Copy it and edit:

```bash
cp .env.example .env
```

Never commit `.env` to source control.

## The variables you will actually touch

### LLM provider

| Variable | Default | Notes |
| --- | --- | --- |
| `ARTA_LLM_PROVIDER` | `ollama` | `ollama` \| `anthropic` \| `google_gemini` \| `openai` |
| `ARTA_LLM_MODEL` | `qwen2.5:32b` | Model name for the chosen provider |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama endpoint |
| `ANTHROPIC_API_KEY` | — | Only when `ARTA_LLM_PROVIDER=anthropic` |

### Access and authentication

| Variable | Notes |
| --- | --- |
| `ARTA_API_KEY` | Required for admin endpoints — change the placeholder |
| `ARTA_BOOTSTRAP_ADMIN_EMAIL` / `ARTA_BOOTSTRAP_ADMIN_PASSWORD` | First-run admin, created only when the users table is empty; unset after first login |
| `ARTA_DEMO_MODE` | `1` = no-database demo login (`admin@arta.dev` / `demo1234`) — never in production |

### Stores

| Variable | Notes |
| --- | --- |
| `DATABASE_URL` | The primary secret — contains the Postgres password; keep in sync with `POSTGRES_PASSWORD` |
| `POSTGRES_*` | Needed for the Postgres service to initialize at bootstrap |
| `NEO4J_*` | Traceability graph credentials |
| `REDIS_PASSWORD` / `REDIS_URL` | Blank password is local-dev only; set a strong one for staging/prod |

### Network

| Variable | Default | Notes |
| --- | --- | --- |
| `ARTA_API_HOST_PORT` | `38087` | Host-side API port mapping (container-internal port stays 8000) |
| CORS origins | — | Comma-separated allowed origins for the API |

### Telemetry

| Variable | Default | Notes |
| --- | --- | --- |
| `ARTA_TELEMETRY` | `1` | `0` = zero network calls (air-gap safe); fields documented in [docs/TELEMETRY.md](https://github.com/Dpod-Labs-Private-Limited/OPEN-ARTA/blob/main/docs/TELEMETRY.md) |
| `ARTA_TELEMETRY_EXTENDED` | off | Opt-in Tier-2 events |

### Integrations

| Variable | Notes |
| --- | --- |
| `JIRA_HOST` / `JIRA_USER` / `JIRA_API_TOKEN` | Jira import + auto-filing of `sut_regression` defects |

## Per-project configuration

SUT-specific behavior — base URLs, auth, discovery settings, provider
overrides — belongs in **project configuration in the UI**, never hard-coded.
That is a project policy, not just a convention: core code with SUT-specific
defaults is rejected in review.
