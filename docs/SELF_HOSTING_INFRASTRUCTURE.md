# ARTA Self-Hosting Infrastructure Specification

> Everything required to run ARTA on your own infrastructure. Derived from the shipped `docker-compose.yml`, `docker-compose.prod.yml`, `docker-compose.train.yml`, `Dockerfile`, and `.env.example` — numbers reflect measured behavior (including OOM headroom found in real runs), not guesses.
>
> Last updated: 2026-08-10 · Reviewed through a systems-architecture / platform-engineering / QE lens.

---

## 1. Deployment profiles

| Profile | What runs | Use case | Host sizing |
|---|---|---|---|
| **P0 — Evaluation (mock-data mode)** | `uvicorn src.api.main:app` only — no Postgres/Neo4j/Redis (skip `DATABASE_URL`/`NEO4J_URI`/`REDIS_URL`) | Kick the tires, UI walkthrough. **Demo only — runs on mock users; never expose beyond localhost** | 2 vCPU · 4 GB RAM · 10 GB disk · Python 3.12 |
| **P1 — Full stack, cloud LLM** | All 7 containers; LLM = Anthropic / Gemini / OpenAI / Azure (or Claude Code CLI) | Standard team deployment | 8 vCPU · 32 GB RAM min, **64 GB recommended** · 100 GB SSD |
| **P2 — Full stack, local LLM (default / air-gap capable)** | P1 + Ollama on the host or a peer GPU box | Data-sovereign / regulated / no cloud keys | P1 host **+ GPU (see §5)** · +50 GB model storage |
| **P3 — Model training (optional)** | `arta-train` image (CUDA), QLoRA fine-tune of the upskill model | Refreshing the fine-tuned generation model | 1× NVIDIA GPU ≥24 GB VRAM · CUDA 12.1 driver · ~10 GB image |

Memory guidance for P1/P2: the sum of shipped per-service `mem_limit`s is ≈ 38.5 GB. Limits are ceilings, not steady-state usage — 32 GB hosts work for API-test-centric use with ZAP capped or disabled, but all-tools runs (discovery HAR parsing + ZAP active scan + parallel browsers) have *measured* peaks that want the 64 GB host.

---

## 2. Architectural constraints that shape deployment

Read this before sizing anything — these constraints dominate operations more than hardware does.

1. **Single-instance, single-process, stateful.** The API keeps run registries and the generated-test inventory in process memory (a disk-scan fallback re-synthesizes inventory after restarts, but in-flight state is not recoverable). The production command runs **one uvicorn worker**; do not add `--workers` and do not run two replicas against the same database — neither is supported.
2. **No HA today.** Availability model = healthchecks + `restart: always` + fast single-node recovery. Plan for it: this is a pipeline tool, not a serving path — a short outage delays test runs, it doesn't take down your product.
3. **A restart kills in-flight runs.** Generation and execution runs do not survive an API container restart. Operational rules: never restart mid-run; drain (let runs finish) before upgrades; schedule maintenance windows; treat `docker compose restart arta-api` as run-destroying.
4. **Execution runs where ARTA runs.** Playwright browsers, Newman, k6, and ZAP all execute inside/alongside the API container. Test traffic originates from this host — its network position relative to the SUT (VPN, firewall rules, latency) is part of the deployment design, and its resource contention is part of your results (see §6, k6).
5. **DB availability is a security control.** When Postgres is unreachable, authentication falls back to built-in mock users (see §8). Keep the database healthy or fenced.

---

## 3. Service matrix (the 7-container stack)

| Service | Image | Container port | Host port (default) | Mem limit (base) | Prod overlay (cpu / mem) | Persistent volume |
|---|---|---|---|---|---|---|
| `arta-api` | built from `Dockerfile` target `runtime` (python:3.12-slim + Node 20 + Chromium + Newman + k6) | 8000 | **38087** (`ARTA_API_HOST_PORT`) | **16 g** | 2.0 / 2 G ⚠ see §9 | `arta_artifacts` → `/var/arta/artifacts`; bind `./.arta` |
| `arta-frontend` | built from `frontend/Dockerfile` (Next.js 14; prod = standalone ~157 MB) | 3000 | **38088** (`ARTA_FRONTEND_HOST_PORT`) | 2 g | 1.0 / 1 G | — |
| `postgres` | postgres:16-alpine | 5432 | 5432 | 1 g | 2.0 / 2 G | `postgres_data` |
| `neo4j` | neo4j:5-community | 7474 (HTTP UI), 7687 (Bolt) | 7474 / 7687 | 2 g | 1.0 / 2 G | `neo4j_data` |
| `redis` | redis:7-alpine | 6379 | 6379 | 256 m | 0.5 / 512 M | `redis_data` |
| `chromadb` | chromadb/chroma:latest ⚠ floating tag | 8080 | 8080 | 1 g | 1.0 / 1 G | `chroma_data` |
| `zap` | zaproxy/zap-stable:latest ⚠ floating tag | 8080 | **38090** (`ZAP_HOST_PORT`) | **16 g** | — | — |

Operationally significant details baked into the compose file:

- **`arta-api` runs `init: true` (tini)** — subprocess reaping for Playwright/Newman/k6 children; don't remove it.
- **`shm_size: 256m` on `arta-api`** — Chromium's shared memory. Raise (512m–1g) if you increase Playwright parallelism (§6).
- **`extra_hosts: host.docker.internal:host-gateway`** on both `arta-api` and `zap` — required on Linux so containers reach host-exposed SUTs and the host Ollama daemon.
- **All services carry healthchecks**; `restart: on-failure` (base) / `always` (prod overlay).
- The two 16 g limits are evidence-based, not padding: the API was OOM-killed parsing multi-GB discovery HARs at 4 g and 8 g; ZAP's passive-scan Java process was OOM-killed at 1 g, 4 g, and ~7.6 g before 16 g gave clean full-scan completion (RC=0).
- **Supply-chain note:** two images float (`latest`/`stable` tags) and `requirements.txt` is lower-bound style with no lockfile. For reproducible enterprise deployments, pin image digests and snapshot a constraints file at install time.

---

## 4. Host software prerequisites

| Requirement | Version / detail |
|---|---|
| OS | Linux x86_64 (primary; tested on Ubuntu-class kernels). macOS/Windows via Docker Desktop 4.x fine for evaluation |
| Docker | Engine 20.10+ with Compose v2 (`docker compose`), or Docker Desktop 4.x. `host-gateway` support required |
| Ollama (P2 default path) | Running on the host at `localhost:11434` **before** starting the API; containers reach it via `host.docker.internal:11434`. May run on a separate GPU server — point `OLLAMA_BASE_URL` at it |
| NVIDIA stack (P3 only) | Driver supporting CUDA 12.1 + `nvidia-container-toolkit` |
| Claude Code CLI (only if `ARTA_LLM_PROVIDER=claude_code`) | Prod compose mounts the host's Node install + `~/.claude` credentials read-only into the container |
| Python 3.12 (P0 only) | `pip install -r requirements.txt` + `uvicorn` |

---

## 5. LLM capacity planning (on-prem SLM sizing)

| Model role | Default | Weights (Q4) | KV-cache reality | Hardware guidance |
|---|---|---|---|---|
| Primary (`ARTA_LLM_MODEL`) | `qwen2.5:32b` | ~20 GB | ARTA's prompt budget grows to 16K/32K tokens on truncation retries; long-context 32B inference adds several GB of KV cache on top of weights | **24 GB VRAM = entry** (constrain `num_ctx`, expect occasional offload slowdowns) · **32–48 GB = comfortable** (RTX 5090/A6000/L40S class) |
| Fast tier (`ARTA_FAST_MODEL`) | `qwen3:8b` | ~5–6 GB | negligible | 8 GB VRAM or CPU |
| Deep tier (`ARTA_DEEP_MODEL`) | `qwen3:32b` / fine-tuned variant | ~20 GB | as primary | as primary |
| Cloud providers | Anthropic / Gemini / OpenAI / Azure | n/a | n/a | No GPU; egress to provider API only |

- CPU-only inference of the 32B works (32 GB+ free RAM) but is too slow for bulk generation; acceptable for single-requirement trials only.
- **Do not share one GPU between Ollama serving and P3 training during working hours** — training evicts the serving model. Train off-hours or on a second GPU.
- Cost/traffic controls are built in (per-tool model overrides, escalation caps, optional Redis-backed LLM response cache) and need no extra infrastructure.
- **Training profile (P3):** dataset generation ~5–10 min (teacher = Gemini or Claude; needs that provider's key). QLoRA fine-tune of the 32B student: **~30–90 min on a single 24 GB GPU** (Unsloth + flash-attn). Output adapter → `models/arta-qwen-upskill/`; merge into Ollama via `ollama create -f models/arta-qwen-upskill/Modelfile`.

---

## 6. Workload model — what actually drives load

Size against these, not against averages:

| Load driver | Resource shape | Notes |
|---|---|---|
| LLM generation (bulk regen) | GPU/provider-bound; API CPU light | Concurrency deliberately capped (CLI providers serialize at 1) — generation throughput scales with the model server, not the API host |
| Discovery probe | One Chromium + HAR capture; **HAR parsing is the API's peak-RSS event** (multi-GB `json.loads`) | This is what the 16 g API limit exists for |
| Playwright execution | ~200–300 MB per Chromium context; workers × contexts is your memory knob | Raise `shm_size` beyond 256m if you raise parallelism |
| ZAP active scan | Java heap grows with scanned surface (the 16 g service) | Longest-running phase in all-tools runs |
| k6 performance tests | CPU + network burst **from this host** | See caveat below |
| Newman / Axe / Pytest | Light | Negligible next to the above |

**k6 co-location caveat (QE-critical):** load is generated from the ARTA host, so perf results include this host's CPU contention and its network path to the SUT — not purely the SUT's behavior. Co-located k6 is fine for smoke-level thresholds; for load numbers you intend to act on, run ARTA on an otherwise-idle host near the SUT, or treat latency results as relative trends rather than absolute measurements.

**Reference workload:** a ~40-requirement project generating ~1,000 test units across all six runtimes → bulk generation is hours-scale (LLM-bound), an all-tools execution run is 1–3 hours (ZAP-dominated). Plan run windows, not interactive latency.

---

## 7. Network specification

**Inbound (expose to users):**

| Port | Service | Exposure guidance |
|---|---|---|
| 38087 | ARTA API | Users/CI. Put behind a TLS reverse proxy in production (uvicorn already runs `--proxy-headers`) |
| 38088 | ARTA UI | Users. Same proxy |
| 5432, 7474, 7687, 6379, 8080 (chroma), 38090 (ZAP) | Data stores + scanner | **Dev-convenience mappings — never expose beyond localhost/internal network.** In production, remove these port publications or firewall them |

**Outbound (egress) — all optional except the SUT:**

| Destination | Purpose | Needed when |
|---|---|---|
| Your SUT (HTTP/S; VPN if applicable) | Discovery, test execution, ZAP scans | Always — and note the container inherits the host's routing/VPN |
| Host Ollama :11434 | Local LLM | P2 (no external egress — the air-gap path) |
| LLM provider API | Cloud LLM | P1 with a cloud provider |
| Jira / GitHub / Confluence / Slack / Teams / SMTP | Integrations | Only if configured |
| Package registries (deb, npm, PyPI, k6 repo) | **Build time only** | Image builds; runtime containers need none |

**Air-gapped deployments:** build/pull images and `ollama pull` models on a connected machine, transfer images (`docker save`/`load`) and Ollama model blobs, run P2. Steady-state runtime requires **zero external egress** — ARTA ships no telemetry, no phone-home, no update checks.

---

## 8. Security specification

- **Set `ARTA_API_KEY` — always.** Authentication intentionally no-ops in dev when it is empty.
- **Mock-user fallback (landmine):** when the database is unreachable, auth falls back to six built-in mock users (`admin@arta.dev`, …). On a self-hosted instance a Postgres outage therefore *downgrades authentication*. Mitigate: monitor DB health (§10), fence the API ports, and strip the mock fallback for hardened deployments.
- **The base compose is a development posture end-to-end**: published DB ports, ZAP API key disabled (`api.disablekey=true`), source bind-mounts, dev-mode frontend. The prod overlay (§9) is **mandatory** for real deployments, plus: set `ZAP_API_KEY` and remove the disable flag, and close the DB port publications.
- **Secrets checklist** (all ship as `change-me-*`; the API logs a startup WARNING if any remain): `ARTA_API_KEY`, `JWT_SECRET_KEY` (256-bit; `scripts/gen-secret.sh` generates these), `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`, optional `REDIS_PASSWORD`. Rotate anything used during evaluation before go-live. Review `CORS_ORIGINS` and JWT expiries (`60 min` access / `7 d` refresh defaults).
- **Container hardening already shipped:** non-root user (UID 1001); shells, package managers, and download tools removed from the runtime image; setuid binaries stripped; healthcheck via Python urllib (no shell). SUT credentials pasted into ARTA live in `.arta/` and Postgres — include both in your data-classification scope.
- **Non-mutation posture:** ARTA's pipeline is read-only against the SUT by default (triple-enforced: network-layer probe blocking, gen-time validation, dispatch-time deny). Destructive testing requires explicit opt-in env vars plus per-spec markers — safe default for shared/staging SUTs.

---

## 9. Production deployment

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The prod overlay: drops `--reload` and source bind-mounts (image is authoritative), sets `ENVIRONMENT=production` (a runtime guard refuses to start if dev flags leak in), `restart: always`, explicit CPU/memory limits, and builds the slim standalone frontend image.

⚠ **Known overlay discrepancy — adjust before heavy use:** the prod overlay caps `arta-api` at **2 G**, but the base compose carries **16 g** for a measured reason (discovery-HAR parsing OOM-killed the API at 4 g and 8 g). For any deployment running discovery against real SUTs, override the prod limit to 8–16 G. Keep ZAP at 16 g wherever security scans run.

**Upgrade procedure (consequence of §2):** wait for in-flight runs to complete (or accept their loss) → back up volumes + `.arta/` → `docker compose build` → `up -d` → verify `/health`. No rolling upgrades; single-node blue-green (second stack on alternate ports, switch the proxy) is the zero-downtime option if you need one.

---

## 10. Observability & operations

| Signal | Mechanism |
|---|---|
| Liveness | `GET /health` (API), per-service Docker healthchecks, frontend polls `/api/admin/health` every 60 s and shows a degradation banner |
| Tracing | OpenTelemetry built in, **off by default** — set `ARTA_TRACING_ENABLED=1` + `OTEL_EXPORTER_OTLP_ENDPOINT` to ship to your collector; no-op otherwise |
| Logs | `docker compose logs` — **container logs are UTC**; don't compare raw timestamps to a local-time host clock when judging stalls |
| OOM diagnosis | Trust `dmesg` (`Killed process … (java|python)`), **not** Docker's `OOMKilled` flag — cgroup-v2 reports false negatives for these kills. Watch ZAP/API `RestartCount` climbing mid-run |
| Disk watch | `arta_artifacts` is the top grower — auto-pruned after `ARTA_ARTIFACT_RETENTION_DAYS` (default 30); alert at 80% volume usage |
| Backups | `pg_dump` (Postgres) + `neo4j-admin database dump` + the `.arta/` directory (config, discovery catalogs, environments — small but precious). Chroma/Redis are reconstructible. RPO = your backup cadence; RTO = restore volumes + `up -d` on any Docker host |

---

## 11. Storage

| Volume | Contents | Growth profile |
|---|---|---|
| `postgres_data` | Projects, users, requirements, test cases, execution results | Steady; GBs over months |
| `neo4j_data` | Traceability graph | Small–medium |
| `chroma_data` | RAG vector store | Small |
| `redis_data` | Cache / eventing | Small |
| `arta_artifacts` | Screenshots, Playwright traces, HTML/JSON reports | **Largest grower** — 30-day auto-prune |
| `.arta/` (bind mount) | Config, discovery catalogs, captured endpoints, environments | Small but **back it up** |

Disk sizing: 100 GB SSD covers P1 (images are several GB — Python + Node 20 + Chromium + k6 — plus data volumes and 30-day artifacts). P2 adds ~50 GB for Ollama models (each 32B ≈ 20 GB plus fast/deep tiers) → 150–200 GB NVMe.

---

## 12. Quick reference: minimum vs recommended

| | Evaluation (P0) | Team, cloud LLM (P1) | Air-gap / local LLM (P2) |
|---|---|---|---|
| vCPU | 2 | 8 | 8–16 |
| RAM | 4 GB | 32 GB min / **64 GB rec.** | 64 GB |
| GPU | — | — | 24 GB VRAM entry / **32–48 GB rec.** (or peer Ollama server) |
| Disk | 10 GB | 100 GB SSD | 150–200 GB NVMe |
| External egress | LLM provider | SUT + LLM provider + integrations | **SUT only — zero external** |
| HA | — | Single node, healthcheck + auto-restart (no HA; see §2) | Same |
| Install | `pip` + `uvicorn` | `docker compose` + **prod overlay (mandatory)** | Same + host Ollama |
