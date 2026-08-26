---
title: Getting started
description: Install ARTA with Docker Compose and a local Ollama LLM — no API keys, no cloud account — and open the UI in about five commands.
---

ARTA runs entirely on your machine: a Docker Compose stack for the platform and
a local LLM through [Ollama](https://ollama.com) by default. No API keys, no
cloud account, no data leaving your network.

## Prerequisites

- **Docker** with the Compose plugin (`docker compose version` should work)
- **Ollama** running locally, with a model pulled:

  ```bash
  ollama pull qwen2.5:32b
  ```

  Or skip Ollama and use a cloud LLM key (Anthropic, OpenAI, Google Gemini) —
  see [Configuration](/docs/configuration/).

## Quickstart

```bash
git clone https://github.com/AmeyaAI/OPEN-ARTA.git
cd OPEN-ARTA
cp .env.example .env          # defaults are Ollama-local; add keys only if you want cloud LLMs
docker compose up -d
```

Then open:

- **UI** — http://localhost:38088
- **API** — http://localhost:38087

## First login

Pick one of two paths **before** the first start:

1. **Real admin account** — set both variables in `.env`; ARTA creates this
   admin when the users table is empty, and you should unset them after first
   login:

   ```bash
   ARTA_BOOTSTRAP_ADMIN_EMAIL=admin@example.com
   ARTA_BOOTSTRAP_ADMIN_PASSWORD=change-me
   ```

2. **No-database demo** — set `ARTA_DEMO_MODE=1` and log in as
   `admin@arta.dev` / `demo1234`. **Never use demo mode in production.**

## Next steps

- [Your first test run](/docs/quickstart/) — from a project to a
  truthful report. The fastest first win is the API-test path from an OpenAPI
  spec.
- [Installation](/docs/installation/) — everything the quickstart
  glossed over: services, ports, environment, telemetry.
