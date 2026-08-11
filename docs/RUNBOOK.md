# ARTA Operator + Developer Runbook

This document covers everything needed to operate ARTA in production:
provider switching, troubleshooting, observability, and recovery from common failures.

---

## 1. Switching the LLM Provider

ARTA supports three providers with strict separation:

| Provider | What it uses | When to use |
|----------|--------------|-------------|
| `ollama` | Local Ollama server + Qwen models | Default. Zero per-request cost. Slower than cloud. |
| `anthropic` | Direct Anthropic API + `ANTHROPIC_API_KEY` | When you want Sonnet/Opus quality + speed. |
| `claude_code` | Host machine's Claude Code CLI binary | When the user already has Claude Code installed and authenticated. |

### Switch via Settings UI (no restart)

```
GET  /api/settings/llm                 → see current provider + available models
PUT  /api/settings/llm                 → switch (body: {provider, model, base_url?, api_key?})
POST /api/settings/llm/test            → ping the new provider, see latency
```

Example:
```bash
curl -X PUT http://localhost:38087/api/settings/llm \
  -H "X-API-Key: $ARTA_API_KEY" -H "Content-Type: application/json" \
  -d '{"provider": "ollama", "model": "arta-qwen-pro:latest"}'
```

### Switch via env vars (requires container restart)

Edit `.env`:
```
ARTA_LLM_PROVIDER=ollama
ARTA_LLM_MODEL=arta-qwen-pro:latest
ARTA_PRIMARY_MODEL=arta-qwen-pro:latest
ARTA_FAST_MODEL=qwen3:8b
ARTA_DEEP_MODEL=qwen3:32b
```

Then `docker compose restart arta-api`.

---

## 2. Pulling a Missing Ollama Model

If you see `ollama: model 'X' not found locally. Run: ollama pull X` in startup logs:

```bash
ollama pull arta-qwen-pro:latest
ollama pull qwen3:8b
ollama pull qwen3:32b
```

Verify availability:
```bash
curl http://localhost:11434/api/tags | jq '.models[].name'
```

---

## 3. Aborting a Stuck Generation Job

When a generate-all job is taking longer than expected:

```bash
# 1. Find the running job
curl -s "http://localhost:38087/api/tests/generate-all/active?project_id=$PID" \
  -H "X-API-Key: $ARTA_API_KEY"

# 2. Abort it gracefully (loop exits at next requirement boundary)
curl -X POST "http://localhost:38087/api/tests/generate-all/abort?job_id=$JOB_ID" \
  -H "X-API-Key: $ARTA_API_KEY"
```

Aborted jobs preserve their partial results. To resume from where they left off:
```bash
curl -X POST "http://localhost:38087/api/tests/generate-all/retry?job_id=$JOB_ID&from_requirement=REQ-005"
```

---

## 4. Inspecting Circuit-Breaker State

Each LLM provider has a circuit breaker (5 failures in 60s → OPEN for 30s).

The breaker logs state transitions:
```
circuit_breaker[ollama]: 5 failures in 60s — OPEN for 30s
circuit_breaker[ollama]: cooldown elapsed (30.1s) — HALF_OPEN trial
circuit_breaker[ollama]: HALF_OPEN trial succeeded — CLOSED
```

Currently no /metrics endpoint exposes breaker state directly; check container logs:
```bash
docker compose logs --since=10m arta-api | grep circuit_breaker
```

---

## 5. Reading Stage-by-Stage Logs

Every requirement's generation is logged with timing per stage:

```
[REQ-001] ▶ Generation pipeline START (workflow=c26a52f6, provider=ollama)
[REQ-001] Stage 1/4: Risk scoring (provider=ollama)
[REQ-001] ✓ Stage 1/4 done in 82.3s (priority=P1, score=6, types=['UI','API','Security'])
[REQ-001] Stage 2/4: ATDD Gherkin generation
[REQ-001] ✓ Stage 2/4 done in 283.3s (1 Gherkin scenarios)
[REQ-001] Stage 3/4: Automation script generation (tools=['playwright','newman','zap'])
[REQ-001] ✓ Stage 3/4 done in 612.1s (3 scripts)
[REQ-001] Stage 4/4: Writing scripts to disk + persisting tests
[REQ-001] ✓ Stage 4/4 done in 0.4s (3 files written, 12 tests created, trace=c26a52f6)
[REQ-001] ▣ Generation pipeline COMPLETE in 977.8s (workflow=c26a52f6, gen_source=llm, total_tests=12)
```

Filter live:
```bash
docker compose logs -f arta-api | grep "Stage\|pipeline"
```

---

## 6. Three-Tier CI Execution

Generated analytics tests carry `@pytest.mark.tier1`, `tier2`, or `tier3` decorators
plus `@pytest.mark.analytics` or `@pytest.mark.adversarial`.

| Tier | Trigger | Target SLA | What runs |
|------|---------|------------|-----------|
| 1 | Every commit | ≤30s | Schema + structure checks (no LLM) |
| 2 | PR open/update | ≤5min | + Mocked LLM tests on canned data |
| 3 | Nightly cron | ≤60min | + Full LLM judge + adversarial suite |

Run a tier locally:
```bash
pytest src/automation/pytest/analytics/ -m tier1
pytest src/automation/pytest/analytics/ -m "tier1 or tier2"
pytest src/automation/pytest/analytics/ -m adversarial
```

CI workflow: `.github/workflows/arta-quality.yml` (jobs `analytics-tier1-commit`, `analytics-tier2-pr`, `analytics-tier3-nightly`).

---

## 7. Debugging a Failing Analytics Test (Trace ID Workflow)

Every test entry carries `trace_id`, `model_version`, `prompt_version`. To debug:

1. Find the failing test's `trace_id`:
   ```bash
   curl "http://localhost:38087/api/tests?project_id=$PID" \
     -H "X-API-Key: $ARTA_API_KEY" | jq '.tests[] | select(.id == "TC-X")'
   ```

2. The trace_id, model_version, prompt_version tell you what produced the test.
   - If `model_version` differs between failing and last-passing run → model regression
   - If `prompt_version` differs → prompt-template change
   - If `dataset_version` (analytics only) differs → fixture changed

3. To re-generate with the previous prompt_version, check out that git revision and
   trigger `/api/tests/regenerate?requirement_id=REQ-X&force=true`.

---

## 8. Materialising Frozen Fixtures

Analytics tests need their `.parquet` fixtures present locally before running.
The orchestrator generates them automatically during test generation. To regenerate manually:

```bash
python -m src.fixtures.generator REQ-001
```

Output:
```
Materialised fixture: fixtures/analytics/req_001_dataset_v1_0_0.parquet
SHA-256: 8c4f...
```

The SHA-256 is the `dataset_version` recorded on every test entry — change the seed
or row count and the hash changes, which makes regression bisection clear.

---

## 9. Health & Monitoring

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness probe (always 200 unless API is down) |
| `GET /api/admin/health` | F3-5: per-service status + `degraded: bool` flag (postgres, neo4j, redis, chromadb, llm_provider). Frontend `AppShell` polls every 60 s and shows a red banner whenever `degraded` is true. |
| `GET /metrics` | Prometheus exposition (no auth) — `arta_llm_calls_total`, `arta_generation_stage_duration_seconds`, `arta_judge_score`, etc. |
| `GET /api/settings/llm` | Resolved provider + available models |
| `GET /api/dashboard/events` | SSE stream of pipeline events (Redis pub/sub) |
| `GET /api/agents/status` | Per-agent status (which agent is currently busy) |

Sample admin health check:
```bash
curl http://localhost:38087/api/admin/health
# → {"services":[{"name":"PostgreSQL","status":"Connected",...}], "degraded":false, "checked_at":"..."}
```

When `degraded: true`, the red banner in the web UI links to `/admin` for the
service-by-service breakdown. The probe is cheap (every backing service is hit
with a single `SELECT 1` / `PING` / `RETURN 1`), safe to scrape on a 60 s tick.

---

## 9a. Persistent Test Artifacts (F3-1)

Screenshots, traces, k6/Newman JSON results, and Playwright HTML reports are
written to `ARTA_ARTIFACTS_DIR` (default `/var/arta/artifacts`, backed by the
named docker volume `arta_artifacts` so they survive container restarts).

```bash
# Inspect what's stored (volume name is prefixed with your compose project name)
docker volume inspect $(docker volume ls -q | grep arta_artifacts)
docker compose exec arta-api ls /var/arta/artifacts | head

# List artifacts for a run
curl -H "X-API-Key: $ARTA_API_KEY" \
  "http://localhost:38087/api/runs/$RUN_ID/artifacts"

# Download a specific artifact (path-traversal protected)
curl -OJ -H "X-API-Key: $ARTA_API_KEY" \
  "http://localhost:38087/api/runs/$RUN_ID/artifacts/screenshot.png"
```

Retention runs at API startup. Override the window:

```bash
# Keep artifacts for 14 days instead of 30
ARTA_ARTIFACT_RETENTION_DAYS=14 docker compose up -d arta-api
```

If `/var/arta/artifacts` is not writable (dev sandbox without the volume),
the resolver falls back to `/tmp/arta-results` and logs a warning that
artifacts will NOT survive restarts.

---

## 9b. Test Rollback (F3-2)

```bash
# Rollback one version (most common — restores the previous version)
curl -X POST -H "X-API-Key: $ARTA_API_KEY" -H "Content-Type: application/json" \
  -d '{}' "http://localhost:38087/api/tests/$TEST_ID/rollback"

# Rollback to a specific version number
curl -X POST -H "X-API-Key: $ARTA_API_KEY" -H "Content-Type: application/json" \
  -d '{"to_version": 3}' "http://localhost:38087/api/tests/$TEST_ID/rollback"

# Rollback to the version stamped with a particular trace_id
# (use the trace_id badge in Test Explorer — see Section 7)
curl -X POST -H "X-API-Key: $ARTA_API_KEY" -H "Content-Type: application/json" \
  -d '{"to_trace_id": "c26a52f6-..."}' \
  "http://localhost:38087/api/tests/$TEST_ID/rollback"
```

Returns 404 when there is no version history. Returns 409 when the test only
has one version and no prior state to restore.

---

## 9c. Production Deploy Guard (F3-4)

When `ENVIRONMENT=production` is set, `_enforce_production_safety()` runs at
import time and refuses to start the API if the uvicorn command line contains
any of `--reload`, `--reload-dir`, `--reload-include`, or `--reload-exclude`.
Exit code is **78** (sysexits EX_CONFIG) and a CRITICAL log line explains why.

The provided `docker-compose.prod.yml` already sets `ENVIRONMENT=production`
and uses a `--reload`-free uvicorn command — no manual configuration needed.

---

## 10. Test Execution & Results

```bash
# Trigger a test run for a project
curl -X POST "http://localhost:38087/api/execution/run?project_id=$PID&suite=full" \
  -H "X-API-Key: $ARTA_API_KEY"

# List recent runs
curl "http://localhost:38087/api/execution/runs?project_id=$PID" \
  -H "X-API-Key: $ARTA_API_KEY"

# Stream live execution events
curl -N "http://localhost:38087/api/dashboard/events"
```

For analytics requirements, results include `judge_score` (0.0-1.0) and `judge_issues`
when the test had an `eval_rubric`. Tests with `judge_score < passing_threshold` (default 0.8)
are downgraded PASS → FAIL automatically.

---

## 11. Self-Healing

When tests fail, the self-healing agent can propose fixes:

```bash
# List pending heal proposals
curl "http://localhost:38087/api/healing/queue" -H "X-API-Key: $ARTA_API_KEY"

# Approve a proposal (applies fix to the source file)
curl -X POST "http://localhost:38087/api/healing/{proposal_id}/approve" \
  -H "X-API-Key: $ARTA_API_KEY"

# Reject
curl -X POST "http://localhost:38087/api/healing/{proposal_id}/reject" \
  -H "X-API-Key: $ARTA_API_KEY"
```

---

## 12. Common Failures & Recovery

### "TimeoutError" repeatedly in automation generation
- Combined Sonnet/qwen call exceeded the timeout (default 600s for cloud, 2400s for Ollama)
- Cause: Model too slow OR prompt too large
- Fix: Switch to a smaller model via `PUT /api/settings/llm`, or reduce
  the requirement's description length

### Analytics tests show as PENDING with empty content
- **Past bug** (fixed): orchestrator was discarding the agent's `test_code`
- **If it recurs**: check `script_content` field in `/api/tests` response. Empty = bug
- **Workaround**: Trigger `/api/tests/regenerate?requirement_id=X&force=true`

### Job stuck on first requirement for hours
- Check circuit breaker state in logs (`docker compose logs | grep circuit_breaker`)
- Abort + retry: `POST /api/tests/generate-all/abort` then `POST /api/tests/generate-all/retry`

### Generated tests fail with `ImportError: analytics_helpers`
- The helpers module wasn't found at `src/automation/pytest/analytics_helpers.py`
- Fix: ensure the file exists; re-run `git pull` if working from a feature branch

### Judge keeps returning `judge_unavailable`
- LLM provider is down or rate-limited
- Check `/api/settings/llm` → `available.{provider}.reachable`

---

## 13. Configuration File Cheat Sheet

| File | Purpose |
|------|---------|
| `.env` | Provider env vars, secrets, DB credentials |
| `.arta/arta.config.yaml` | Platform-wide quality gates, project types, agent behaviour |
| `.arta/projects.json` | Per-project config (LLM, integrations, environments) |
| `docker-compose.yml` | Container orchestration, volume mounts |
| `.github/workflows/arta-quality.yml` | CI pipeline (8 stages + 3 analytics tiers) |

Edit the YAML config and restart the container to apply.

---

## 14. Cost & Performance Reference

Per a full 21-requirement run (measured on dev hardware):

| Provider | Cost | Wall time | Notes |
|----------|------|-----------|-------|
| Ollama (arta-qwen-pro) | $0 | 25-45 min | GPU-bound; concurrency=1 |
| Anthropic Haiku-default | ~$0.55 | 8-12 min | Use for cost optimization |
| Anthropic Sonnet-everywhere | ~$3.20 | 15-25 min | Use only when quality demands |

To switch cost profile: change `ARTA_FAST_MODEL` and `ARTA_PRIMARY_MODEL` env vars.

---

## 15. Where to Get Help

- **Logs**: `docker compose logs --since=10m arta-api`
- **Architecture**: `ARTA_ARCHITECTURE.md`
- **API spec**: `http://localhost:38087/docs` (FastAPI Swagger UI) — fixed by F5-8

---

## 16. Known Limitations (post Final-5)

ARTA is genuinely usable end-to-end. These items are deferred — not "almost
done" — and may matter for your environment:

| Limitation | Impact | Workaround |
|------------|--------|------------|
| No compliance / audit-trail / reproducibility dimensions in the quality gate | Regulated industries (HIPAA, PCI-DSS, GDPR) cannot rely on the gate alone | Add custom checks via `GateThresholds` subclass; export decision JSON for external audit |
| `src/api/routers/tests.py` is 3,994 lines | Slow IDE; high merge-conflict surface | Plan documents a 6-way split; defer until next refactor sprint |
| Frontend TS `strict: false` with `ignoreBuildErrors: true` | ~50 hidden TS2xxx errors mask API contract drift | Run `npx tsc --noEmit` locally to surface them; fix opportunistically |
| Most router endpoints have no unit-test coverage | Regressions ship undetected | `tests/unit/` has 21 tests; expand alongside new features |
| No distributed tracing | Hard to follow a single request across agent → DB → graph | Prometheus metrics exist; traces are an open work item |
| `output: 'standalone'` next.config + `typescript.ignoreBuildErrors: true` | Production frontend skips type-check | Acceptable for now; CI runs `tsc --noEmit` separately |

For each, the active plan file (`~/.claude/plans/quiet-launching-willow.md`)
tracks the recommended fix and effort estimate.

### F5 features added but worth noting

- **Persistent strategy artifacts** (F5-2) live at `.arta/strategies/{project}_{timestamp}_{trace}.json`. List them via `GET /api/projects/{id}/strategies`. Useful for "why did the gate FAIL three sprints ago?" audits.
- **Axe a11y violations** (F5-1) populate `nfr.a11y_violations_critical/moderate` per-run. The gate's `_check_a11y` thresholds default to 0 critical / 5 moderate — adjust via `PUT /api/gates/thresholds`.
- **`red_phase_status` persisted** (F5-5) — query DB directly: `SELECT test_id, red_phase_status FROM test_cases WHERE red_phase_status = 'GREEN_UNEXPECTED'` flags tests that shouldn't have passed.
- **CI tier presence check** (F5-7) — analytics tier-1/2/3 jobs no longer silently skip; look for `::notice::Analytics Tier N SKIPPED` lines in the GitHub Actions log when no analytics tests exist.
