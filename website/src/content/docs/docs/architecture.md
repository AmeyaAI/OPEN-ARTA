---
title: Architecture
description: How ARTA is put together — the FastAPI engine, agent pipeline, data stores, six execution runtimes, and the truthful reporting layer.
---

ARTA is a self-hosted stack: a Python engine orchestrating specialized agents
over shared stores, dispatching external test runtimes, and reporting through
a quality gate. The full design document is
[ARTA_ARCHITECTURE.md](https://github.com/AmeyaAI/OPEN-ARTA/blob/main/ARTA_ARCHITECTURE.md)
in the repository; this page is the map.

## The shape of the system

```
Requirements (Jira / OpenAPI / docs)     Source code (GitHub)     Live behavior (discovery probe)
        └───────────────────────┬────────────────────┬───────────────────┘
                                ▼
                  ┌──────────────────────────────┐
                  │   ARTA engine (FastAPI)      │
                  │   agent pipeline:            │
                  │   strategy → risk → ATDD →   │
                  │   generation → validation    │
                  ├──────────────────────────────┤
                  │ Postgres · Neo4j · Redis ·   │
                  │ Chroma                       │
                  └──────────────┬───────────────┘
                                 ▼
      Playwright · Newman · k6 · ZAP · Axe · Pytest      (execution runtimes)
                                 ▼
        truthful report · traceability graph · quality gate · CI/CD
```

## Components

| Component | Where | Role |
| --- | --- | --- |
| API engine | `src/api/` | FastAPI backend: projects, requirements, generation, execution dispatch, gates |
| Agents | `src/agents/` | One module per pipeline responsibility — see [AI agents](/docs/agents/) |
| Prompts | `src/prompts/` | The prompt templates that encode the test-engineering methodology |
| Frontend | `frontend/` | Next.js 14 dashboard |
| Postgres | compose service | Primary store: projects, requirements, tests, results |
| Neo4j | compose service | Traceability graph: requirement → criterion → test → execution → defect |
| Redis | compose service | Queues and pub/sub between pipeline stages (including the self-healing regen queue) |
| ChromaDB | compose service | Vector store for retrieval during generation |
| ZAP | compose service | OWASP ZAP daemon the security runtime drives |

## The pipeline, end to end

1. **Ingest** — requirements arrive from Jira, an OpenAPI spec, or documents.
2. **Score** — each requirement gets a probability × impact risk score.
3. **Discover** — ARTA probes the SUT read-only: DOM catalog, captured
   endpoints, source-code context. This evidence is what generation is
   grounded against.
4. **Design** — ATDD Gherkin acceptance criteria per requirement.
5. **Generate + validate** — scripts per runtime, each checked by the
   grounding validators; failures retry with hints, then block honestly.
6. **Execute** — per-runtime dispatch with retries and self-healing.
7. **Report** — failure classification, traceability links, quality gate.

## Design positions worth knowing

- **Evidence over inference** — generation is constrained by discovery output,
  not by what an LLM believes a typical app looks like.
- **Truthfulness over green** — `BLOCKED` is a first-class, visible outcome;
  silent fallbacks are treated as bugs.
- **Non-mutation by default** — enforced independently at the network,
  generation, and dispatch layers.
- **Local-first AI** — the default provider is Ollama on your hardware; cloud
  providers are opt-in per project, and misconfiguration is an error rather
  than a silent cloud fallback.
