---
title: Integrations
description: LLM providers (local Ollama by default, Anthropic, OpenAI, Gemini), Jira defect filing, GitHub source context, and CI/CD integration.
---

## LLM providers

ARTA is local-first: the default provider is **Ollama** on your own hardware,
so a stock install makes no cloud LLM calls at all.

```bash
ARTA_LLM_PROVIDER=ollama        # ollama | anthropic | google_gemini | openai
ARTA_LLM_MODEL=qwen2.5:32b
OLLAMA_BASE_URL=http://localhost:11434
```

Cloud providers are opt-in — set the provider and its key (for example
`ANTHROPIC_API_KEY`). Providers can be configured **per project**, so one team
can run a local model while another uses a cloud model in the same install.
If a provider is misconfigured, ARTA raises an error rather than silently
falling back to a different endpoint — data residency should never fail open.

See [`.env.example`](https://github.com/gangadharneeli/OPEN-ARTA/blob/main/.env.example)
for the complete provider configuration surface.

## Jira

Two roles:

- **Requirement source** — import requirements from Jira into a project.
- **Defect filing** — when the defect-intelligence agent classifies a failure
  as `sut_regression`, ARTA can auto-file it to Jira with the evidence
  attached:

```bash
JIRA_HOST=https://your-org.atlassian.net
JIRA_USER=your-email@example.com
JIRA_API_TOKEN=your-jira-api-token
```

## GitHub (source-code context)

Point a project at its SUT source repository and generation gets grounded in
real route definitions and request-body shapes extracted from the code —
frontend routes, backend endpoints, DTOs. This is one of the inputs the
grounding validators check generated tests against.

## CI/CD

ARTA is API-first: everything the UI does, the FastAPI backend exposes. The
practical CI pattern is to call the API from your pipeline — trigger a
generation or a run, then poll for the quality-gate result — which works the
same way from **GitHub Actions, GitLab CI, or Jenkins**. The gate gives CI a
truthful red/green: a `sut_regression` fails the build; a `test_gen_bug` is
ARTA's problem, counted against ARTA.

## Requirement sources beyond Jira

OpenAPI specs and plain documents also work as requirement sources. New
requirement-source connectors are one of the explicitly invited
[first contributions](/OPEN-ARTA/docs/contributing/).
