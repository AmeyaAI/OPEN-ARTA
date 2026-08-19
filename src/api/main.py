"""
ARTA FastAPI Application
Main entry point for the AI Test Architect platform REST API.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .dependencies import require_api_key as _require_api_key  # noqa: E402

from ..agents.sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

# D2: Configure root logging BEFORE importing routers so all agent/router
# loggers (`arta.*`) inherit a handler. Without this, INFO logs are silently
# dropped — making the entire generation pipeline appear silent.
#
# F1-5: Formatter now includes `%(trace_short)s` so every log line emitted during
# a generation request carries the trace_id prefix — debugging multi-requirement
# runs just becomes `docker compose logs | grep trace=c26a52f6`.
_LOG_LEVEL = os.environ.get("ARTA_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s [%(name)s] [trace=%(trace_short)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,  # override any uvicorn-installed root handler
)
# F1-5: Install the trace-id filter AFTER basicConfig so the formatter has something to format.
from ..observability.log_context import install as _install_trace_filter
_install_trace_filter()

# Tame noisy third-party loggers
for noisy in ("httpx", "httpcore", "neo4j", "anthropic._base_client"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from .routers import requirements, tests, execution, defects, gates, assistant, projects, healing, auth, users, traceability, dashboard, reports, cicd, settings as settings_router, discovery as discovery_router, triage as triage_router, sut_quality as sut_quality_router
# R306.E: `exploratory` router deprovisioned — see the include-router site below.

log = logging.getLogger("arta")


# F3-4: Production deploy guard — refuse to start when dev-only flags reach prod.
# Triggered by ENVIRONMENT=production in docker-compose.prod.yml.
def _r144_e_resolve_ttl() -> int:
    """R144.E.1 — resolve orphan-sweep TTL minutes from env.

    ARTA_ORPHAN_SWEEP_TTL_MIN (default 75) — operator-tunable to extend
    the window for long-running smokes (Iter 2 + Iter 3 of R143.F both
    hit the 75min ceiling before PW phase persisted results). Clamped
    to a sane range [15, 1440] minutes.
    """
    raw = os.environ.get("ARTA_ORPHAN_SWEEP_TTL_MIN", "75")
    try:
        val = int(raw)
    except Exception:
        return 75
    if val < 15:
        return 15
    if val > 1440:
        return 1440
    return val


def _r144_e_build_sweeper_sql(ttl_min: int, heartbeat_min: int) -> str:
    """R144.E.2 — compose the orphan-sweep SQL with TTL + heartbeat.

    Heartbeat clause exploits the existing schema (no migration): a run
    that has persisted at least one execution_result row in the last
    ``heartbeat_min`` minutes is alive, even when started_at is older
    than TTL.

    R147.A FIX — the persistence-time column in `execution_results` is
    ``executed_at`` (default NOW()), not ``created_at``. Pre-R147.A:
    `er.created_at` referenced a non-existent column → the sweep SQL
    raised `UndefinedColumnError` every 5 minutes, the heartbeat path
    silently no-op'd, and the operator's logs were flooded with
    misleading exception traces. Post-R147.A: `er.executed_at` is the
    correct heartbeat signal — every per-spec persist sets it via the
    column's DEFAULT NOW() clause, resetting the orphan clock.
    """
    return (
        "UPDATE test_runs "
        "SET status = 'failed', "
        "    completed_at = NOW(), "
        "    gate_decision = 'FAIL', "
        f"    gate_summary = 'orphan_recovery: Run abandoned >{ttl_min}min ago (R144.E)' "
        "WHERE status = 'running' "
        f"  AND COALESCE(started_at, created_at) < NOW() - INTERVAL '{ttl_min} minutes' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM execution_results er "
        "    WHERE er.run_id = test_runs.id "
        f"      AND er.executed_at > NOW() - INTERVAL '{heartbeat_min} minutes'"
        "  ) "
        "RETURNING run_id"
    )


def _r145_a_2_sweep_disk(
    project_id: str,
    *,
    dry_run: bool = False,
) -> dict:
    """R145.A.2 — sweep on-disk Newman items for REPLACE_ME-tainted URLs
    and substitute from project env_block.variables OR stamp BLOCKED.

    Walks `src/automation/newman/*.json` + `.arta/regen_queue/applied/*.json`.
    For each item containing a placeholder token in `request.url.raw`:
      (a) attempt substitution using positional-guess heuristic against
          the project's env_block.variables (same algorithm as R145.A.1
          preflight sanitizer)
      (b) on success, rewrite the file in-place
      (c) on failure, stamp `info._r145_a_replaceme_unresolved=true` so
          the existing _filter_collection_for_unresolved_vars at
          execution.py:4778 emits BLOCKED at dispatch

    Returns: {newman_files_scanned, items_substituted, items_blocked,
              items_unchanged, samples[:5]}

    Idempotent: items already substituted are skipped (no double-write).
    Dry-run mode reports what would change without modifying disk.

    R145.A.3 callers (startup / post-paste / pre-smoke triggers) invoke
    this SAME helper — single source of truth, three trigger sites.
    """
    from pathlib import Path as _Path_R145
    from ..shared.env_var_patterns import (
        path_has_placeholder, find_placeholder_segments,
        resolve_r43_synthetic_value, is_placeholder_value,
    )

    audit: dict = {
        "project_id": project_id,
        "dry_run": dry_run,
        "newman_files_scanned": 0,
        "items_substituted": 0,
        "items_blocked": 0,
        "items_unchanged": 0,
        "samples": [],
    }

    # Resolve env_variables from the project's env_block
    env_vars: dict = {}
    try:
        from .routers.projects import _PROJECTS
        proj = _PROJECTS.get(project_id) or {}
        env_block = (
            (proj.get("environments") or {})
            .get("staging") or {}
        ).get("variables") or {}
        for k, v in env_block.items():
            if isinstance(v, str) and not is_placeholder_value(v):
                env_vars[k] = v
    except Exception as exc:
        log.debug("R145.A.2: env_vars resolution skipped: %s", exc)

    # Walk the disk surfaces
    surfaces = [
        _Path_R145("src/automation/newman"),
        _Path_R145(".arta/regen_queue/applied"),
    ]
    for surface in surfaces:
        if not surface.is_dir():
            continue
        for f in sorted(surface.glob("*.json")):
            try:
                content = json.loads(f.read_text())
            except Exception:
                continue
            audit["newman_files_scanned"] += 1
            items = content.get("item") or []
            file_dirty = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                req = item.get("request") or {}
                url = req.get("url") or {}
                raw = url.get("raw") if isinstance(url, dict) else None
                if not raw or not path_has_placeholder(raw):
                    continue
                segs = raw.split("/")
                any_unsub = False
                for idx, ph in find_placeholder_segments(raw):
                    if idx >= len(segs) or segs[idx] != ph:
                        any_unsub = True
                        continue
                    # Positional guess: name from preceding non-empty segment
                    var_guess = None
                    for back in range(idx - 1, -1, -1):
                        prev = segs[back].strip()
                        if prev and "{" not in prev and not is_placeholder_value(prev):
                            var_guess = f"{prev}_id"
                            break
                    if not var_guess:
                        any_unsub = True
                        continue
                    val = env_vars.get(var_guess) or env_vars.get(var_guess.lower())
                    if val and not is_placeholder_value(val):
                        segs[idx] = str(val)
                        continue
                    synth = resolve_r43_synthetic_value(var_guess)
                    if synth and not is_placeholder_value(synth):
                        segs[idx] = synth
                        continue
                    any_unsub = True
                if any_unsub:
                    # Stamp the BLOCKED marker (dispatcher reads this)
                    info = item.setdefault("info", {}) if isinstance(item.get("info"), dict) else None
                    if info is None:
                        item["info"] = {"_r145_a_replaceme_unresolved": True}
                    else:
                        info["_r145_a_replaceme_unresolved"] = True
                    audit["items_blocked"] += 1
                    if len(audit["samples"]) < 5:
                        audit["samples"].append({
                            "file": f.name, "url": raw[:120], "outcome": "blocked",
                        })
                    file_dirty = True
                else:
                    # All placeholders substituted; rewrite URL.raw
                    new_raw = "/".join(segs)
                    url["raw"] = new_raw
                    item["request"]["url"] = url
                    audit["items_substituted"] += 1
                    if len(audit["samples"]) < 5:
                        audit["samples"].append({
                            "file": f.name, "before": raw[:80], "after": new_raw[:80],
                            "outcome": "substituted",
                        })
                    file_dirty = True
            if file_dirty and not dry_run:
                try:
                    f.write_text(json.dumps(content, indent=2))
                except Exception as exc:
                    log.warning("R145.A.2: failed to rewrite %s: %s", f, exc)
    audit["items_unchanged"] = (
        audit["newman_files_scanned"] - audit["items_substituted"] - audit["items_blocked"]
    )
    return audit


def _r145_a_3_audit_append(trigger: str, audit: dict) -> None:
    """R145.A.3 — append one JSONL entry to
    `.arta/audit/r145_a3_autopurge.jsonl` recording trigger + outcome.
    """
    try:
        audit_dir = Path(".arta/audit")
        audit_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            **{k: v for k, v in audit.items() if k != "samples"},
            "samples_count": len(audit.get("samples", [])),
        })
        with (audit_dir / "r145_a3_autopurge.jsonl").open("a") as f:
            f.write(line + "\n")
    except Exception as exc:
        log.debug("R145.A.3: audit append skipped: %s", exc)


def _r145_a_3_autopurge_disabled() -> bool:
    return os.environ.get("ARTA_R145_A_AUTO_PURGE_DISABLE") == "1"


def _r145_a_3_autopurge_dry_run() -> bool:
    return os.environ.get("ARTA_R145_A_AUTO_PURGE_DRY_RUN") == "1"


def _r145_a_3_autopurge(trigger: str, project_id: str | None = None) -> dict | None:
    """R145.A.3 — invoke R145.A.2's sweep at one of three trigger sites
    (startup / post-paste / pre-smoke), then append to audit JSONL.

    Returns the audit dict (or None when disabled). Best-effort: failures
    log at warn but do NOT raise (instrumentation must not break the
    trigger site path).
    """
    if _r145_a_3_autopurge_disabled():
        return None
    if not project_id:
        return None
    try:
        audit = _r145_a_2_sweep_disk(
            project_id, dry_run=_r145_a_3_autopurge_dry_run(),
        )
        _r145_a_3_audit_append(trigger, audit)
        log.info(
            "R145.A.3: %s auto-purge swept project=%s "
            "(scanned=%d substituted=%d blocked=%d, dry_run=%s)",
            trigger, project_id,
            audit.get("newman_files_scanned", 0),
            audit.get("items_substituted", 0),
            audit.get("items_blocked", 0),
            audit.get("dry_run"),
        )
        return audit
    except Exception as exc:
        log.warning("R145.A.3: %s auto-purge failed: %s", trigger, exc)
        return None


def _enforce_production_safety() -> None:
    if os.environ.get("ENVIRONMENT", "development").lower() != "production":
        return
    forbidden = {"--reload", "--reload-dir", "--reload-include", "--reload-exclude"}
    leaked = sorted(forbidden.intersection(sys.argv))
    if leaked:
        msg = (
            "FATAL: ENVIRONMENT=production but uvicorn was invoked with dev-only "
            f"flag(s) {leaked}. Hot-reload in production is a security and "
            "stability risk (file-watcher leaks, double-execution of startup). "
            "Use docker-compose.prod.yml's command override (no --reload)."
        )
        logging.critical(msg)
        sys.stderr.write(msg + "\n")
        sys.exit(78)  # EX_CONFIG — sysexits(3): configuration error
    if os.environ.get("ARTA_LOG_LEVEL", "").upper() == "DEBUG":
        logging.warning("ENVIRONMENT=production with ARTA_LOG_LEVEL=DEBUG — "
                        "verbose logging may leak sensitive data; consider INFO.")

    # F6-4: Refuse known-default secrets in production. The defaults are fine
    # for local dev (everything works out of the box) but signing JWTs with
    # "arta-dev-secret-change-in-production" or accepting "arta-dev-key" as the
    # API key in prod is an instant compromise.
    bad_defaults = {
        "JWT_SECRET":   "arta-dev-secret-change-in-production",
        "ARTA_API_KEY": "arta-dev-key",
    }
    leaked_secrets = [
        k for k, default in bad_defaults.items()
        if os.environ.get(k, default) == default
    ]
    if leaked_secrets:
        msg = (
            f"FATAL: ENVIRONMENT=production but these secret(s) are still "
            f"using the development default value: {leaked_secrets}. Rotate "
            "them in your secret store / .env before deploying."
        )
        logging.critical(msg)
        sys.stderr.write(msg + "\n")
        sys.exit(78)


_enforce_production_safety()


def _warn_on_change_me_placeholder_secrets() -> None:
    """F20-27: Warn when any secret env var still uses a `change-me-*`
    placeholder from .env.example. These work fine for local dev but are
    a hygiene smell — they look the same across all developer machines,
    making any leaked dev DB trivially open. The F6-4 prod guard already
    HARD-EXITS on leaked dev defaults in production; this is the dev
    equivalent: visible warning, no exit.
    """
    placeholder_keys = (
        "POSTGRES_PASSWORD", "NEO4J_PASSWORD", "REDIS_PASSWORD",
        "ARTA_API_KEY", "JWT_SECRET_KEY", "JWT_SECRET",
        "ANTHROPIC_API_KEY",
    )
    leaked = []
    for k in placeholder_keys:
        v = os.environ.get(k, "")
        if v and ("change-me" in v.lower() or v.lower().startswith("change_me")):
            leaked.append(k)
    if leaked:
        logging.warning(
            "[F20-27] %d secret(s) still use 'change-me-*' placeholder values: %s. "
            "Rotate before sharing this .env or deploying anywhere beyond your "
            "local dev box. Generate replacements with `./scripts/gen-secret.sh --all`.",
            len(leaked), leaked,
        )


_warn_on_change_me_placeholder_secrets()


def _warn_on_pat_leak_in_projects_file() -> None:
    """F10-5: Loud startup warning when a GitHub PAT literal is on disk.

    F6-3 added on-disk chmod 0o600 + masked the field in API responses, but
    the file itself still contains the literal token after a write — and old
    tokens from before that fix were never rotated. Surface this every boot
    so the operator sees it until they actually rotate at github.com/settings/tokens.
    """
    import re
    from pathlib import Path
    candidates = [
        Path(os.environ.get("ARTA_PROJECTS_FILE", ".arta/projects.json")),
        Path(".arta/projects.json"),
    ]
    pat_re = re.compile(r"github_pat_[A-Za-z0-9_]{20,}")
    for p in candidates:
        try:
            if not p.exists():
                continue
            text = p.read_text(errors="ignore")
        except Exception:
            continue
        matches = pat_re.findall(text)
        if matches:
            logging.warning(
                "SECURITY: %s contains %d GitHub PAT literal(s) (e.g. %s…). "
                "Rotate at https://github.com/settings/tokens immediately and "
                "store the new value in a secrets backend, not on disk.",
                p, len(matches), matches[0][:24],
            )
        return  # only check the first existing candidate


_warn_on_pat_leak_in_projects_file()


def _try_init_llm(provider: str):
    """Try to initialise an LLM client for the given provider. Returns client or None.

    Strict provider separation:
      - claude_code: ONLY uses the host CLI binary mounted into the container.
                     Never falls back to API keys.
      - anthropic:   ONLY uses ANTHROPIC_API_KEY for direct API calls.
                     Never invokes the CLI.
      - ollama:      ONLY uses OLLAMA_BASE_URL + ARTA_LLM_MODEL.
    """
    try:
        if provider == "claude_code":
            # CLI-only: require the binary to exist. Do NOT fall back to API keys.
            cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
            import subprocess
            try:
                result = subprocess.run([cli_path, "--version"], capture_output=True, timeout=5, text=True)
                if result.returncode != 0:
                    log.warning("claude_code: CLI at %s exited %d (%s). "
                                "Mount the host binary or pick a different provider.",
                                cli_path, result.returncode, result.stderr.strip()[:200])
                    return None
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                log.warning("claude_code: CLI not available at %s (%s). "
                            "Mount the host binary or pick a different provider — "
                            "API key fallback is disabled.", cli_path, e)
                return None
            log.info("Claude Code CLI found: %s (path=%s)", result.stdout.strip(), cli_path)
            from ..agents.claude_cli_client import ClaudeCLIClient
            return ClaudeCLIClient(cli_path)

        elif provider == "anthropic":
            # API-only: requires ANTHROPIC_API_KEY. Never invokes the CLI.
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                log.warning("anthropic: ANTHROPIC_API_KEY not set. "
                            "Set it in .env or pick a different provider.")
                return None
            if "sk-ant-oat" in api_key:
                from ..agents.claude_oauth_client import ClaudeOAuthClient
                return ClaudeOAuthClient(api_key)
            from anthropic import AsyncAnthropic
            return AsyncAnthropic(api_key=api_key)

        elif provider == "ollama":
            base_url = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
            model = os.environ.get("ARTA_LLM_MODEL", "arta-qwen-pro:latest")
            # Verify Ollama is reachable + check model availability
            import httpx
            try:
                resp = httpx.get(f"{base_url}/api/tags", timeout=3)
                if resp.status_code != 200:
                    log.warning("ollama: %s/api/tags returned %d", base_url, resp.status_code)
                    return None
            except Exception as exc:
                log.warning("ollama: %s unreachable (%s). Is Ollama running on the host?",
                            base_url, exc)
                return None
            # Verify configured models are pulled (warn if not — generation will fail later)
            try:
                tags = resp.json().get("models", [])
                available = {m.get("name", "") for m in tags}
                fast_model = os.environ.get("ARTA_FAST_MODEL", "qwen3:8b")
                primary_model = os.environ.get("ARTA_PRIMARY_MODEL", model)
                deep_model = os.environ.get("ARTA_DEEP_MODEL", "qwen3:32b")
                for required in {model, fast_model, primary_model, deep_model}:
                    if required and required not in available:
                        log.warning("ollama: model '%s' not found locally. Run: ollama pull %s",
                                    required, required)
            except Exception:
                pass  # availability check is best-effort; don't fail init
            from ..agents.ollama_client import OllamaDirectClient
            return OllamaDirectClient(base_url, model)

        else:
            # Unknown provider — try via LiteLLM adapter
            from ..models.llm_config import LLMConfig, LLMProvider
            from ..agents.llm_client import create_llm_client
            try:
                prov = LLMProvider(provider)
            except ValueError:
                log.warning("Unknown LLM provider: %s", provider)
                return None
            model = os.environ.get("ARTA_LLM_MODEL", "claude-sonnet-4-6")
            cfg = LLMConfig(provider=prov, model=model)
            return create_llm_client(cfg)

    except Exception as exc:
        log.warning("Failed to init LLM provider %s: %s", provider, exc)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise DB pool + LLM client.  Shutdown: dispose connections."""

    # ── Phase 5.4: HMAC audit-trail secret enforcement ───────────────────
    # In production, refuse to start without ARTA_AUDIT_HMAC_SECRET — the
    # ephemeral fallback inside `_sign_audit_trail` keeps the API working but
    # produces audit hashes that can't be verified after a restart, which
    # silently breaks the compliance audit trail. Production deploys MUST
    # set this env var. Dev deploys (ARTA_DEV=1 or ARTA_ENV=dev/local) keep
    # the ephemeral fallback so a missing-secret doesn't block local work.
    _hmac = os.environ.get("ARTA_AUDIT_HMAC_SECRET", "")
    # Phase 5 follow-up #6 — accept the more common env-var names too so a
    # deploy using `ENVIRONMENT=production` (Heroku-style) or `APP_ENV=prod`
    # (12-factor) doesn't silently bypass the HMAC enforcement. ARTA_ENV
    # remains the project-canonical name.
    _env = (
        os.environ.get("ARTA_ENV")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("APP_ENV")
        or ""
    ).lower()
    _is_prod = _env in {"prod", "production"} or os.environ.get("ARTA_PRODUCTION") == "1"
    if not _hmac and _is_prod:
        log.error(
            "STARTUP FAILED: ARTA_AUDIT_HMAC_SECRET is unset in a production "
            "environment. Audit-trail signatures would use a process-local "
            "ephemeral key and become unverifiable after restart. Set this "
            "env var (e.g. via secrets manager) and restart. Set ARTA_ENV=dev "
            "to bypass this check on developer machines."
        )
        raise RuntimeError(
            "ARTA_AUDIT_HMAC_SECRET required in production — see startup log"
        )
    if not _hmac:
        log.warning(
            "ARTA_AUDIT_HMAC_SECRET unset — audit signatures will use an "
            "ephemeral key and won't survive a restart. OK for local dev; "
            "set this env var before deploying to production."
        )

    # ── Database (with retry) ────────────────────────────────────────────
    from .db_adapter import reset_db_check
    reset_db_check()  # Clear stale _db_available flag from previous worker
    from tenacity import retry, stop_after_attempt, wait_fixed, RetryError
    for attempt in range(3):
        try:
            from ..db.session import init_db, close_db
            await init_db()
            log.info("Database connection pool initialised")
            break
        except Exception as exc:
            if attempt < 2:
                log.info("Database not ready (attempt %d/3), retrying in 2s...", attempt + 1)
                import asyncio
                await asyncio.sleep(2)
            else:
                log.warning("Database not available — falling back to in-memory mode: %s", exc)
    try:
        from .routers.auth import ensure_bootstrap_admin
        await ensure_bootstrap_admin()
    except Exception as _bs_exc:  # noqa: BLE001 — bootstrap must never block boot
        log.warning("bootstrap admin check skipped: %s", _bs_exc)
    try:
        from ..telemetry import start as _telemetry_start
        _telemetry_start()
    except Exception as _tel_exc:  # noqa: BLE001 — telemetry must never block boot
        log.debug("telemetry start skipped: %s", _tel_exc)

    # ── R117.F — auto-migrate dirty DOM catalogs at startup ───────────
    # Pre-R117: existing `dom_catalog.json` files may contain smushed
    # role+name pairs captured by pre-R117.E probes. Without auto-rebuild,
    # operators must manually trigger `POST /api/discovery/refresh` (needs
    # ATDD automation promise.
    #
    # R117.F: at boot, scan `.arta/discovery/*/dom_catalog.json`. For each
    # catalog flagged dirty by `_is_dirty_catalog()` (R117.G), rebuild it
    # by invoking `ingest_dom_snapshots()` against cached HAR sidecars
    # (which already exist on disk from prior discovery runs). The
    # rebuild applies R117.A's filter → clean catalog written in-place.
    # Zero operator action required.
    try:
        from ..agents.api_discovery import ingest_dom_snapshots, _is_dirty_catalog
        from pathlib import Path
        catalogs_root = Path(".arta/discovery")
        if catalogs_root.is_dir():
            scanned = dirty = rebuilt = no_har = 0
            for pid_dir in catalogs_root.iterdir():
                if not pid_dir.is_dir():
                    continue
                catalog_path = pid_dir / "dom_catalog.json"
                if not catalog_path.is_file():
                    continue
                scanned += 1
                try:
                    import json as _json_r117f
                    cat_data = _json_r117f.loads(catalog_path.read_text())
                except Exception:
                    continue
                if not _is_dirty_catalog(cat_data):
                    continue
                dirty += 1
                # Look for cached HAR sidecars in this project's discovery dir
                har_candidates = list(pid_dir.glob("**/discovery.har"))
                if not har_candidates:
                    no_har += 1
                    log.warning(
                        "R117.F: catalog %s is dirty but no cached HAR — "
                        "operator must trigger /api/discovery/refresh to clean",
                        pid_dir.name,
                    )
                    continue
                # R203 — pick the RICHEST HAR (most DOM sidecar elements), not
                # an arbitrary glob()[0]. Rebuilding from a sparse/login-walled
                # HAR is exactly how a 30-testid catalog got clobbered to 2.
                def _r203_har_selectors(har_p) -> int:
                    total = 0
                    for sc in har_p.parent.glob("dom*.json"):
                        try:
                            _d = _json_r117f.loads(sc.read_text())
                            els = _d.get("elements") if isinstance(_d, dict) else None
                            if isinstance(els, list):
                                total += sum(1 for e in els if isinstance(e, dict)
                                             and (e.get("testid") or e.get("ariaLabel")
                                                  or e.get("text") or e.get("name")))
                        except Exception:
                            continue
                    return total
                best_har = max(har_candidates, key=_r203_har_selectors)
                try:
                    ingest_dom_snapshots(pid_dir.name, best_har)
                    rebuilt += 1
                    log.info(
                        "R117.F: auto-rebuilt dirty catalog %s from cached HAR",
                        pid_dir.name,
                    )
                except Exception as exc:
                    log.warning(
                        "R117.F: rebuild failed for %s: %s",
                        pid_dir.name, exc,
                    )
            log.info(
                "R117.F migration complete: scanned=%d dirty=%d "
                "rebuilt=%d no_har=%d",
                scanned, dirty, rebuilt, no_har,
            )
    except Exception as _r117_f_exc:
        # Best-effort; never block startup on migration failure
        log.warning("R117.F startup migration skipped: %s", _r117_f_exc)

    # ── LLM client (default: ollama; auto-detect order: ollama > claude_code > anthropic) ─────
    requested_provider = os.environ.get("ARTA_LLM_PROVIDER", "").lower()
    requested_model = os.environ.get("ARTA_LLM_MODEL", "")
    provider = requested_provider
    client = None
    source = "env"  # how the provider was chosen: "env" (explicit) | "auto-detect"

    # If explicit provider set, use it
    if provider and provider != "auto":
        client = _try_init_llm(provider)

    # Auto-detect: try Ollama first (default), then claude_code, then anthropic
    if not client:
        for try_provider in ["ollama", "claude_code", "anthropic"]:
            client = _try_init_llm(try_provider)
            if client:
                provider = try_provider
                source = "auto-detect"
                break

    # OAuth headers only relevant for Anthropic API direct path
    _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if provider == "anthropic" and "sk-ant-oat" in _api_key:
        app.state.llm_extra_headers = {
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        }
        log.info("OAuth token detected for anthropic provider — beta headers enabled")
    else:
        app.state.llm_extra_headers = {}

    if client:
        # I3: app.state.llm_client is the canonical attribute (provider-agnostic).
        # app.state.anthropic kept as a deprecated alias for backward compatibility
        # with existing routers; new code should use llm_client.
        app.state.llm_client = client
        app.state.anthropic = client  # deprecated alias
        app.state.llm_provider = provider
        # Clear startup banner so the resolved provider is unambiguous in logs
        log.info("=" * 60)
        log.info("LLM resolved: provider=%s, model=%s, source=%s",
                 provider, requested_model or "(provider default)", source)
        if requested_provider and requested_provider != provider:
            log.warning("Requested provider '%s' unavailable — fell back to '%s'",
                        requested_provider, provider)
        log.info("=" * 60)
    else:
        app.state.llm_client = None
        app.state.anthropic = None  # deprecated alias
        app.state.llm_provider = None
        log.error("No LLM provider available — AI features disabled. "
                  "Check ARTA_LLM_PROVIDER, OLLAMA_BASE_URL, ANTHROPIC_API_KEY, "
                  "or CLAUDE_CLI_PATH.")

    # ── Jira ──────────────────────────────────────────────────────────────
    # Phase 5.5: classify the connection result so the admin /api/admin/health
    # endpoint can surface "Jira available — auto-file enabled" vs "Jira
    # disconnected — defect intel will classify but never file". Previously
    # `app.state.jira` was set to None on failure with no breadcrumb in the
    # health response, so operators saw classified-but-unfiled defects and
    # didn't know why.
    app.state.jira_status = {"available": False, "reason": "unknown"}
    try:
        from ..integrations.jira_client import JiraClient
        jira = JiraClient()
        if await jira.connect():
            app.state.jira = jira
            app.state.jira_status = {"available": True, "reason": "connected"}
            log.info("Jira integration connected — auto-file is enabled")
        else:
            app.state.jira = None
            app.state.jira_status = {
                "available": False,
                "reason": (
                    "JiraClient.connect() returned False — server reachable but "
                    "auth/credentials likely wrong. Auto-file disabled."
                ),
            }
            log.warning(
                "Jira not connected — defect classification will run but auto-file "
                "(Phase H Fix SSS-2) is disabled"
            )
    except Exception as exc:
        log.warning("Jira integration not available: %s", exc)
        app.state.jira = None
        app.state.jira_status = {
            "available": False,
            "reason": f"JiraClient init raised {type(exc).__name__}: {exc}",
        }

    # ── Notifications (Slack / Teams) ────────────────────────────────────
    try:
        from ..integrations.notifier import NotificationService
        notifier = NotificationService()
        app.state.notifier = notifier if notifier.available else None
        if notifier.available:
            log.info("Notification service initialised (Slack: %s, Teams: %s)",
                     bool(notifier.slack_url), bool(notifier.teams_url))
    except Exception as exc:
        log.warning("Notification service not available: %s", exc)
        app.state.notifier = None

    # ── ChromaDB RAG (with retry) ────────────────────────────────────────
    app.state.chroma = None
    # 2 attempts × 1s backoff = up to 1s of sleep on cold start. Compose's
    # `chromadb` healthcheck already gates startup; this loop only covers the
    # narrow window between healthy-status and the HTTP listener accepting.
    for attempt in range(2):
        try:
            from ..rag import ChromaRAG
            chroma_url = os.environ.get("CHROMA_URL", "http://chromadb:8001")
            rag = ChromaRAG(chroma_url=chroma_url)
            if await rag.connect():
                app.state.chroma = rag
                log.info("ChromaDB RAG initialised at %s", chroma_url)
                from ..rag.embedder import start_embedding_listener
                from ..observability.task_supervisor import supervise as _supervise_embed
                import asyncio as _aio
                redis_url = os.environ.get("REDIS_URL", "redis://redis:6379")
                # F8-3: Supervise — a crashed embedding listener otherwise silently stops
                # indexing documents and nobody notices until RAG search returns stale results.
                _embed_task = _aio.create_task(start_embedding_listener(rag, redis_url))
                _supervise_embed(_embed_task, "embedding_listener")
                app.state._embedding_task = _embed_task
                break
            else:
                raise ConnectionError("ChromaDB connect() returned False")
        except Exception as exc:
            if attempt < 1:
                log.info("ChromaDB not ready (attempt %d/2), retrying in 1s...", attempt + 1)
                import asyncio
                await asyncio.sleep(1)
            else:
                log.warning("ChromaDB unavailable (%s) — RAG features disabled", exc)

    # ── Neo4j (Traceability Graph) ───────────────────────────────────────
    try:
        from neo4j import AsyncGraphDatabase
        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_pw = os.environ.get("NEO4J_PASSWORD")
        if neo4j_pw:
            driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pw))
            await driver.verify_connectivity()
            app.state.neo4j = driver
            log.info("Neo4j connected at %s", neo4j_uri)
        else:
            log.info("NEO4J_PASSWORD not set — traceability uses stub mode")
            app.state.neo4j = None
    except Exception as exc:
        log.warning("Neo4j not available — traceability uses stub mode: %s", exc)
        app.state.neo4j = None

    # R215 — register the driver process-wide so background tasks + agent modules
    # (discovery_executor / architecture_discovery) that have no FastAPI app
    # reference can still reach it. Without this, those paths read a None
    # ctx.neo4j_driver and silently skipped all graph writes for EVERY SUT
    # (discovery_summary.neo4j_written == False). SUT-agnostic.
    try:
        from ..graph.writer import set_driver as _set_graph_driver
        _set_graph_driver(app.state.neo4j)
        if app.state.neo4j is not None:
            log.info("R215: registered process-wide Neo4j driver for background/agent writes")
    except Exception as _sd_exc:
        log.debug("R215: graph driver registration skipped: %s", _sd_exc)

    # Suppress noisy Neo4j warnings about missing labels in empty graph
    import logging as _logging
    _logging.getLogger("neo4j").setLevel(_logging.ERROR)

    # Auto-create Neo4j constraints if graph is empty
    if app.state.neo4j:
        try:
            async with app.state.neo4j.session() as neo_session:
                for constraint in [
                    "CREATE CONSTRAINT IF NOT EXISTS FOR (r:Requirement) REQUIRE r.id IS UNIQUE",
                    "CREATE CONSTRAINT IF NOT EXISTS FOR (ac:AcceptanceCriteria) REQUIRE ac.id IS UNIQUE",
                    "CREATE CONSTRAINT IF NOT EXISTS FOR (tc:TestCase) REQUIRE tc.id IS UNIQUE",
                    "CREATE CONSTRAINT IF NOT EXISTS FOR (er:ExecutionResult) REQUIRE er.id IS UNIQUE",
                    "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Defect) REQUIRE d.id IS UNIQUE",
                ]:
                    await neo_session.run(constraint)
                log.info("Neo4j schema constraints verified")
        except Exception as exc:
            log.warning("Could not create Neo4j constraints: %s", exc)

        # Phase J5 — Phase C constraints + indexes (Endpoint, EnvVar, CallChain).
        # These can't live in the canonical schema.cypher because that file
        # is permission-locked; schema_phase_c.cypher is the sibling that
        # apply_phase_c_schema reads and applies idempotently.
        try:
            from ..graph.schema_loader import apply_phase_c_schema, apply_arch_discovery_schema
            phase_c_result = await apply_phase_c_schema(app.state.neo4j)
            log.info(
                "Phase C schema: applied=%d skipped=%d errors=%d",
                phase_c_result["applied"],
                phase_c_result["skipped"],
                len(phase_c_result["errors"]),
            )
            # Phase AD — Architecture Discovery node types (Service/Token/Scenario).
            ad_result = await apply_arch_discovery_schema(app.state.neo4j)
            log.info(
                "Phase AD schema: applied=%d skipped=%d errors=%d",
                ad_result["applied"], ad_result["skipped"], len(ad_result["errors"]),
            )
        except Exception as exc:
            log.warning("Phase C/AD schema apply failed: %s", exc)

    # ── Phase J post-review: clean polluted discovered_endpoints sidecars ──
    # Pre-J the post-run capture wrote `__ARTA_UNSET_*` sentinel paths into
    # `.arta/discovered_endpoints/{project_id}.json`. Walk all sidecars at
    # startup and purge any sentinel-laden entries so the harvester can
    # work cleanly on the next discovery run.
    try:
        from ..agents.api_discovery import _CAPTURED_DIR, purge_polluted_endpoints
        if _CAPTURED_DIR.is_dir():
            total_purged = 0
            for sidecar in _CAPTURED_DIR.glob("*.json"):
                purged = purge_polluted_endpoints(sidecar.stem)
                total_purged += purged
            if total_purged:
                log.info("Discovered-endpoints sidecar cleanup: removed %d polluted entries", total_purged)
    except Exception as exc:
        log.debug("Discovered-endpoints cleanup skipped: %s", exc)

    # ── Confluence ─────────────────────────────────────────────────────────
    try:
        from ..integrations.confluence_client import ConfluenceClient
        confluence = ConfluenceClient()
        if await confluence.connect():
            app.state.confluence = confluence
            log.info("Confluence integration connected")
        else:
            app.state.confluence = None
    except Exception as exc:
        log.warning("Confluence integration not available: %s", exc)
        app.state.confluence = None

    # ── Redis Event Bus ────────────────────────────────────────────────────
    try:
        import redis.asyncio as aioredis
        redis_url = os.environ.get("REDIS_URL", "redis://redis:6379")
        app.state.redis = aioredis.from_url(redis_url, decode_responses=True)
        await app.state.redis.ping()
        log.info("Redis event bus connected at %s", redis_url)
    except Exception as exc:
        log.warning("Redis event bus not available: %s", exc)
        app.state.redis = None

    # ── GitHub ─────────────────────────────────────────────────────────────
    try:
        from ..integrations.github_client import GitHubClient
        gh = GitHubClient()
        if gh.available:
            app.state.github = gh
            log.info("GitHub integration available (repo: %s)", gh.repo)
        else:
            app.state.github = None
    except Exception as exc:
        log.warning("GitHub integration not available: %s", exc)
        app.state.github = None

    # ── Sync persisted projects to DB ──────────────────────────────────────
    try:
        from .routers.projects import sync_projects_to_db
        await sync_projects_to_db()
    except Exception as exc:
        log.warning("Project sync to DB skipped: %s", exc)

    # ── Sync seed requirements to DB ──────────────────────────────────────
    try:
        from .routers.requirements import _sync_requirements_to_db
        await _sync_requirements_to_db(app)
    except Exception as e:
        log.warning("Could not sync requirements to DB: %s", e)

    # ── Recover stale runs from previous crash ────────────────────────────
    try:
        from .routers.execution import recover_stale_runs
        await recover_stale_runs()
    except Exception as exc:
        log.warning("Stale run recovery skipped: %s", exc)

    # ── R42.6: regen-queue consumer (closes the self-heal loop) ───────────
    # Drains `.arta/regen_queue/{test_id}.json` markers written by R37.1
    # / R38.4 → invokes automation_engineer.generate_single() with the
    # runtime failure as a Tier-2 hint → overwrites the broken spec.
    # Without this, test_gen_regen proposals stayed approved-but-unapplied
    # forever; pillar 5 (self-healing) only "succeeded" on paper.
    try:
        from .services.regen_consumer import start_regen_consumer, is_alive as _regen_alive
        start_regen_consumer()
        # R55.11 — verify the consumer task actually started. Pre-R55.11
        # start_regen_consumer could fail silently (event-loop edge cases),
        # leaving the queue undrained for days. Surface the failure now
        # so operators see it in container startup logs.
        if not _regen_alive():
            log.error(
                "R55.11: regen consumer failed to start (task is None or done) "
                "— self-healing loop is BROKEN. Queue markers will not drain. "
                "Restart the api container or check /api/health/regen-consumer."
            )
        else:
            log.info("R42.6/R55.11: regen consumer healthy + running")
    except Exception as exc:
        log.warning("R42.6: regen consumer skipped: %s", exc)

    # R57.2 — prune per-run Newman sidecar dirs older than 7d. Each run
    # writes its filtered Newman collections to .arta/runs/{run_id}/newman/;
    # retention keeps the last 7 days of run artefacts so operators can
    # forensically inspect prior runs. Older dirs get cleaned up here on
    # startup (one-shot; cheap O(n) scan).
    try:
        import time as _t_57_2, shutil as _sh_57_2
        from pathlib import Path as _Path_57_2
        runs_root = _Path_57_2(".arta/runs")
        if runs_root.is_dir():
            cutoff = _t_57_2.time() - 7 * 86400
            pruned = 0
            for run_dir in runs_root.iterdir():
                if not run_dir.is_dir():
                    continue
                try:
                    if run_dir.stat().st_mtime < cutoff:
                        _sh_57_2.rmtree(run_dir, ignore_errors=True)
                        pruned += 1
                except OSError:
                    continue
            if pruned:
                log.info("R57.2: pruned %d per-run dir(s) older than 7d", pruned)
    except Exception as exc:
        log.debug("R57.2: per-run dir cleanup skipped: %s", exc)

    # R55.4 — prune stale `auth_failed.flag` files older than 24h. Pre-
    # R55.4 these flags accumulated forever (29 stale flags live during
    # the audit) because R39.1 wrote them but no cleanup task removed
    # them. With R55.4's writer canonicalisation + alias symlink, fresh
    # discovery runs will succeed; this prune sweeps the historical
    # flags so the dashboard's auth-state banner doesn't trigger on
    # stale signals.
    try:
        import time as _t_55_4_p, os as _os_55_4_p
        from pathlib import Path as _Path_55_4_p
        disc_root = _Path_55_4_p(".arta/discovery")
        if disc_root.is_dir():
            cutoff = _t_55_4_p.time() - 24 * 3600
            pruned_flags = 0
            for flag in disc_root.rglob("auth_failed.flag"):
                try:
                    if flag.stat().st_mtime < cutoff:
                        flag.unlink()
                        pruned_flags += 1
                except OSError:
                    continue
            if pruned_flags:
                log.info("R55.4: pruned %d stale auth_failed.flag(s) older than 24h", pruned_flags)
    except Exception as exc:
        log.debug("R55.4: auth_failed.flag cleanup skipped: %s", exc)

    # ── F6-16: DB retention — prune once now, then every 24h ──────────────
    try:
        from ..db.retention import prune_once, schedule_periodic
        from ..observability.task_supervisor import supervise
        import asyncio as _aio_ret
        await prune_once()  # synchronous boot prune so first launch tidies up immediately
        app.state._retention_task = supervise(
            _aio_ret.create_task(schedule_periodic()),
            "db_retention_scheduler",
        )
    except Exception as exc:
        log.warning("DB retention scheduler skipped: %s", exc)

    # Fix D: periodic orphan-run sweeper — flips test_runs rows stuck at
    # status='running' for >30min to 'failed'. Without this, an interrupted
    # run (server kill, OOM, deploy) leaves the row showing "running"
    # indefinitely until the next container restart. Verified live in
    # run-170e18: row was stuck for hours.
    try:
        from ..observability.task_supervisor import supervise as _supervise_orphan
        import asyncio as _aio_orphan

        # R144.E.1 — env-configurable orphan-sweep TTL. Pre-R144.E.1: a
        # hardcoded 75min ceiling murdered Iter 2 (run-1d5b96) + Iter 3
        # (run-84c4e8) of R143.F before PW phase persisted results.
        # Operator can now extend the window via ARTA_ORPHAN_SWEEP_TTL_MIN
        # without code change.
        # R144.E.2 — heartbeat via execution_results. A run that's actively
        # persisting per-spec results in the last ttl/3 minutes is alive,
        # even if started_at is older than TTL. Sweeper now requires BOTH
        # the started_at threshold AND no recent execution_results
        # heartbeat to mark a run failed. Reuses the existing schema (no
        # migration) by exploiting that execution_results.created_at is
        # stamped on every per-spec persist.
        _r144_e_ttl_min = _r144_e_resolve_ttl()
        _r144_e_heartbeat_min = max(5, _r144_e_ttl_min // 3)
        _r144_e_sql = _r144_e_build_sweeper_sql(
            _r144_e_ttl_min, _r144_e_heartbeat_min,
        )

        async def _orphan_run_sweeper():
            from ..db.session import async_session_factory
            from sqlalchemy import text as _t
            while True:
                try:
                    async with async_session_factory() as sess:
                        result = await sess.execute(_t(_r144_e_sql))
                        cleared = [r[0] for r in result.fetchall()]
                        await sess.commit()
                        if cleared:
                            log.warning(
                                "R144.E orphan-run sweep: cleared %d stale runs "
                                "(ttl=%dmin, heartbeat=%dmin): %s",
                                len(cleared), _r144_e_ttl_min,
                                _r144_e_heartbeat_min, cleared,
                            )
                except Exception as inner_exc:
                    log.warning("orphan-run sweep iteration failed: %s", inner_exc)
                await _aio_orphan.sleep(300)  # every 5 min

        app.state._orphan_sweeper_task = _supervise_orphan(
            _aio_orphan.create_task(_orphan_run_sweeper()),
            "orphan_run_sweeper",
        )
    except Exception as exc:
        log.warning("orphan-run sweeper failed to start: %s", exc)

    # R145.A.3 — startup trigger for the REPLACE_ME auto-purge sweep.
    # Walks all projects in `_PROJECTS` and invokes R145.A.2's sweep
    # per project. Best-effort; failures log at warn but never block
    # arta-api boot. Killswitch: ARTA_R145_A_AUTO_PURGE_DISABLE=1.
    if not _r145_a_3_autopurge_disabled():
        try:
            from .routers.projects import _PROJECTS
            _r145_a_3_scanned = 0
            for _pid in list(_PROJECTS.keys()):
                if _r145_a_3_autopurge("startup", _pid):
                    _r145_a_3_scanned += 1
            log.info(
                "R145.A.3: startup auto-purge swept %d project(s)",
                _r145_a_3_scanned,
            )
        except Exception as exc:
            log.warning("R145.A.3: startup auto-purge failed: %s", exc)

    # R75.4 — auth-staleness notification poller. Polls every project's
    # cookie TTL hourly and dispatches Slack/Teams notifications on
    # state TRANSITIONS (fresh → stale_soon, stale_soon → expired) so
    # operators get advance notice BEFORE the autonomous loop breaks
    # on auth expiry. Idempotent via in-memory _last_notified_state
    # dict — repeated polls in the same state produce one notification,
    # not spam.
    try:
        import asyncio as _aio_r75_4

        async def _auth_staleness_poller() -> None:
            from .routers.discovery import auth_staleness as _auth_staleness
            from .routers.projects import _PROJECTS
            from ..integrations.notifier import NotificationService

            notifier = NotificationService()
            if not notifier.available:
                log.info(
                    "R75.4: auth-staleness poller idle — neither "
                    "SLACK_WEBHOOK_URL nor TEAMS_WEBHOOK_URL configured; "
                    "frontend badge (R74.2) still surfaces the signal."
                )
                return
            last_notified: dict[str, str] = {}
            while True:
                try:
                    for project_id, project_data in list(_PROJECTS.items()):
                        if not isinstance(project_data, dict):
                            continue
                        try:
                            staleness = await _auth_staleness(project_id, environment="staging")
                        except Exception:
                            continue
                        state = staleness.get("state")
                        if state not in ("stale_soon", "expired"):
                            # Reset the marker when state is fresh/unknown so a
                            # later transition back into stale_soon fires again.
                            if last_notified.get(project_id) and state == "fresh":
                                last_notified.pop(project_id, None)
                            continue
                        prev = last_notified.get(project_id)
                        # Notify on FIRST entry into the state (transition).
                        # Also notify on `stale_soon → expired` upgrade.
                        if prev == state:
                            continue
                        if prev == "expired" and state == "stale_soon":
                            # Going back to less-severe — don't double-notify
                            last_notified[project_id] = state
                            continue
                        try:
                            await notifier.notify_auth_stale(
                                project_id=project_id,
                                project_name=(project_data.get("name") or project_id[:8]),
                                state=state,
                                ttl_hours_remaining=staleness.get("ttl_remaining_hours"),
                                hint=staleness.get("hint"),
                            )
                            log.info(
                                "R75.4: auth-staleness notification sent for "
                                "project=%s state=%s prev=%s",
                                project_id, state, prev,
                            )
                            last_notified[project_id] = state
                        except Exception as send_exc:
                            log.warning(
                                "R75.4: notify_auth_stale failed for project=%s: %s",
                                project_id, send_exc,
                            )
                except Exception as iter_exc:
                    log.warning("R75.4: poller iteration failed: %s", iter_exc)
                await _aio_r75_4.sleep(3600)  # hourly cadence

        app.state._auth_staleness_poller_task = _aio_r75_4.create_task(
            _auth_staleness_poller()
        )
    except Exception as r75_4_exc:
        log.warning("R75.4: auth-staleness poller failed to start: %s", r75_4_exc)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    # Cancel background embedding listener to prevent "aclose() already running" warnings
    embedding_task = getattr(app.state, "_embedding_task", None)
    if embedding_task and not embedding_task.done():
        embedding_task.cancel()
        try:
            await embedding_task
        # BaseException: CancelledError stopped subclassing Exception in 3.8,
        # and naming `asyncio` here hit UnboundLocalError (conditional local
        # `import asyncio` earlier in this function shadows the module) —
        # every shutdown ended with "Application shutdown failed. Exiting."
        except BaseException:
            pass

    # R75.4 — cancel auth-staleness poller on shutdown
    poller_task = getattr(app.state, "_auth_staleness_poller_task", None)
    if poller_task and not poller_task.done():
        poller_task.cancel()
        try:
            await poller_task
        except BaseException:   # see embedding_task note above
            pass

    try:
        gh_client = getattr(app.state, "github", None)
        if gh_client:
            await gh_client.close()
    except Exception:
        pass
    try:
        neo4j_driver = getattr(app.state, "neo4j", None)
        if neo4j_driver:
            await neo4j_driver.close()
    except Exception:
        pass
    try:
        redis_client = getattr(app.state, "redis", None)
        if redis_client:
            await redis_client.aclose()
    except Exception:
        pass
    try:
        from ..db.session import close_db
        await close_db()
    except Exception:
        pass


# Allow CORS from configured origins + common local/network access patterns
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:3001").split(",")
    if o.strip()
]
# Also allow access from any host on common frontend ports (for remote/IP access)
_ALLOWED_ORIGINS += [
    "http://localhost:3001",
    "http://localhost:3000",
    "http://192.168.1.4:3001",
    "http://192.168.1.4:3000",
]
# Deduplicate
_ALLOWED_ORIGINS = list(set(_ALLOWED_ORIGINS))

app = FastAPI(
    title="ARTA — AI Requirements & Test Architect",
    description="Autonomous ATDD platform powered by BMAD TEA methodology",
    version="1.0.0",
    lifespan=lifespan,
)

# F7-4: OpenTelemetry FastAPI instrumentation. No-op when ARTA_TRACING_ENABLED
# is unset/false or the SDK isn't installed. Activated by setting
# ARTA_TRACING_ENABLED=true (+ optional OTEL_EXPORTER_OTLP_ENDPOINT for a
# real collector instead of console output).
try:
    from ..observability.tracing import install_fastapi_instrumentation
    install_fastapi_instrumentation(app)
except Exception as _trace_exc:
    log.warning("Tracing instrumentation skipped: %s", _trace_exc)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ── Rate Limiting (F6-15) ────────────────────────────────────────────────────
# Default: 60 req/min per client. Stricter limits for LLM-heavy paths are
# applied per-router via @limiter.limit("…/minute") decorators (see tests.py
# generate-all). Bypassed entirely when ARTA_API_KEY is empty (dev mode).
def _rate_limit_key(request: Request) -> str:
    """Per-API-key when present, fall back to remote IP."""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return f"bearer:{auth[7:][:20]}"  # truncate so log lines aren't huge
    return f"ip:{request.client.host if request.client else 'unknown'}"


try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    _redis_url = os.environ.get("REDIS_URL", "redis://redis:6379")
    if not os.environ.get("ARTA_API_KEY", ""):
        log.info("Rate limiting bypassed (ARTA_API_KEY empty — dev mode)")
        limiter = Limiter(key_func=_rate_limit_key, default_limits=[])
    else:
        limiter = Limiter(
            key_func=_rate_limit_key,
            default_limits=["60/minute"],   # global per-client default
            storage_uri=_redis_url,
        )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    log.info("Rate limiting enabled (Redis: %s, default 60/min/client)", _redis_url)
except ImportError:
    log.warning("slowapi not installed — rate limiting DISABLED. "
                "Install via `pip install slowapi` or rebuild the container "
                "to pick up requirements.txt change.")
except Exception as exc:
    log.warning("Rate limiting setup failed: %s — continuing without limits", exc)


# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(requirements.router, prefix="/api/requirements", tags=["Requirements"])
app.include_router(tests.router,        prefix="/api/tests",        tags=["Tests"])
app.include_router(execution.router,    prefix="/api/execution",    tags=["Execution"])
app.include_router(defects.router,      prefix="/api/defects",      tags=["Defects"])
app.include_router(gates.router,        prefix="/api/gates",        tags=["Quality Gates"])
app.include_router(assistant.router,    prefix="/api/assistant",    tags=["AI Assistant"])
app.include_router(projects.router,     prefix="/api/projects",     tags=["Projects"])
app.include_router(healing.router,      prefix="/api/healing",      tags=["Self-Heal Approvals"])
app.include_router(auth.router,         prefix="/api/auth",          tags=["Authentication"])
app.include_router(users.router,        prefix="/api",               tags=["User Management"])
app.include_router(traceability.router, prefix="/api/traceability",  tags=["Traceability"])
# R30.2 — Operator triage queue (defects classified as "operator_review").
app.include_router(triage_router.router, prefix="/api/triage", tags=["Triage"])
# R306.E: /api/exploratory deprovisioned. The SBTM prototype was broken in DB mode
# (router passed ISO strings to DateTime columns → asyncpg DataError 500 on every
# create/finding/complete), had no project scoping, and no ARTA pillar consumed its
# data. Router file + repos + models retained (dormant) for a one-line restore.
# app.include_router(exploratory.router, prefix="/api/exploratory", tags=["Exploratory Testing"])
app.include_router(dashboard.router,    prefix="/api/dashboard",     tags=["Dashboard Events"])
# F12-9: /api/test-blocks router removed — had zero frontend callers
# (companion to F12-3 which removed the dead Test Blocks UI panel).
app.include_router(reports.router,      prefix="/api/reports",        tags=["Report Export"])
# Phase H — discovery / chains / steps surface for the new frontend panels.
app.include_router(discovery_router.router, prefix="/api/discovery", tags=["Discovery"])
app.include_router(cicd.router,        prefix="/api/projects",       tags=["CI/CD Configuration"])
app.include_router(settings_router.router, prefix="/api",           tags=["Settings"])
# R37.6 — SUT quality score + 30-day trend.
app.include_router(sut_quality_router.router, prefix="/api/sut", tags=["SUT Quality"])


# I7: Prometheus metrics endpoint (no auth — operators scrape this)
@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    from fastapi.responses import PlainTextResponse
    from ..observability.metrics import metrics as _metrics
    return PlainTextResponse(_metrics.expose(), media_type="text/plain; version=0.0.4")


# F3-1: Serve Playwright test artifacts from the persistent volume so they survive
# container restarts. ARTIFACTS_DIR is resolved once in execution.py (env-driven,
# defaults to /var/arta/artifacts mounted from the named docker volume).
from fastapi.staticfiles import StaticFiles
from .routers.execution import ARTIFACTS_DIR as _ARTIFACTS_DIR, prune_old_artifacts as _prune_artifacts
_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
try:
    _prune_artifacts()
except Exception as _e:
    log.warning("Artifact retention prune failed: %s", _e)
app.mount("/artifacts", StaticFiles(directory=str(_ARTIFACTS_DIR), html=True), name="artifacts")


@app.get("/health")
async def health():
    return {"status": "ok", "platform": "ARTA", "methodology": "BMAD-TEA"}


@app.get("/api/health/jira-config")
async def jira_config_health():
    """R90.3 — operator-facing readiness probe for the Jira defect
    close-loop. Returns whether JIRA_URL + JIRA_EMAIL + JIRA_API_TOKEN
    are all set. When NOT configured, R37.4 silently skips auto-filing
    sut_regression defects → operators can't tell why P0 defects never
    reach the SUT team. This endpoint makes the gap explicit.
    """
    import os
    url = os.environ.get("JIRA_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    configured = bool(url and email and token)
    missing = [k for k, v in [
        ("JIRA_URL", url), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token),
    ] if not v]
    return {
        "configured": configured,
        "url_set": bool(url),
        "email_set": bool(email),
        "token_set": bool(token),
        "missing_env_vars": missing,
        "note": (
            "Jira defect close-loop ready" if configured
            else f"Set {', '.join(missing)} env vars to enable R37.4 auto-filing"
        ),
    }


@app.get("/api/health/llm-circuit-breaker")
async def llm_circuit_breaker_health():
    """R90.7 — operator-facing health probe for LLM-provider circuit
    breakers. When k6 gen (or any tool's gen) consistently fails, the
    canonical signal is `state=OPEN` for the relevant provider. Pre-
    R90.7 operators saw silent stub k6 files on disk and assumed
    inventory was healthy; this endpoint surfaces the underlying
    Pillar-1 LLM outage so operators can wait, scale Ollama, or fall
    back to Claude API.

    Returns: dict keyed by provider name → state, failure counts,
    cooldown remaining. Each entry shape:
        {state, failure_count_in_window, fail_threshold, window_secs,
         cooldown_secs, cooldown_remaining_secs, opened_at_monotonic}
    """
    try:
        from ..agents.circuit_breaker import get_breakers_snapshot
        snapshot = get_breakers_snapshot()
        # Aggregate convenience field: are ANY breakers in trouble?
        any_open = any(
            v.get("state") in ("OPEN", "HALF_OPEN") for v in snapshot.values()
        )
        return {
            "any_circuit_degraded": any_open,
            "breakers": snapshot,
            "note": (
                "If state=OPEN: LLM provider is degraded. Wait for "
                "cooldown_remaining_secs OR scale provider concurrency. "
                "k6/Newman/Playwright gen will silently fail until "
                "circuit closes. The R90.5 dispatch gate prevents stub "
                "files from reaching dispatch in the meantime."
            ),
        }
    except Exception as exc:
        return {"error": str(exc), "breakers": {}}


@app.get("/api/health/regen-consumer")
async def regen_consumer_health():
    """R55.11 — operator-facing health probe for the self-heal regen
    consumer loop. Returns `alive` (whether the asyncio task is running)
    + queue depth diagnostics. When `alive=false` the operator should
    restart the api container; queued markers will sit undrained.
    """
    try:
        from .services.regen_consumer import is_alive, queue_depth, CYCLE_SECS, MAX_PER_CYCLE
        depth = queue_depth()
        return {
            "alive": is_alive(),
            "cycle_secs": CYCLE_SECS,
            "max_per_cycle": MAX_PER_CYCLE,
            **depth,
        }
    except Exception as exc:
        return {"alive": False, "error": str(exc)}


@app.post("/api/admin/build-authz-model",
          dependencies=[Depends(_require_api_key)])
async def build_authz_model_endpoint(project_id: str, request: Request = None):
    """Build the SUT authorization model (route-catalog half of the RBAC
    oracle) from its OpenAPI contract — the first concrete step toward derived
    (not LLM-guessed) RBAC test generation.

    Reads the cached spec (`.arta/openapi/<pid>.json`). SUTs that do NOT serve
    a machine-readable OpenAPI (e.g. an SPA-only SUT) may POST the spec body
    directly: `{"openapi_doc": {...}}` — it is ingested without being cached.

    Returns the per-operation catalog summary: scope / visibility / auth-gated /
    exempt counts + domains. Fail-open (404-style message when no spec)."""
    from ..agents.authz_discovery import build_authz_model, summarize_authz_for_prompt
    doc = None
    if request is not None:
        try:
            body = await request.json()
            doc = (body or {}).get("openapi_doc")
        except Exception:
            doc = None
    model = build_authz_model(project_id, openapi_doc=doc)
    if not model:
        return {"project_id": project_id, "built": False,
                "reason": "no OpenAPI spec cached and none posted — "
                          "POST {\"openapi_doc\": {...}} or run discovery first"}
    return {"project_id": project_id, "built": True,
            "operation_count": model["operation_count"],
            "summary": model["summary"],
            "prompt_preview": summarize_authz_for_prompt(project_id, max_chars=600)}


@app.post("/api/admin/regen-queue/backfill",
          dependencies=[Depends(_require_api_key)])
async def regen_queue_backfill(limit: int | None = None):
    """R57.7 — admin one-click orphan backfill. Moves orphaned regen
    markers back to the live queue so the R42.6 consumer re-attempts
    resolution with R57.10's improved fuzzy matcher.

    Body: optional `?limit=<int>` query param to cap the move count.
    Returns: `{requeued, skipped, still_orphan_before, queue_depth_after}`.
    """
    from .services.regen_consumer import backfill_orphans, queue_depth
    result = backfill_orphans(limit=limit)
    result["queue_depth_after"] = queue_depth()
    return result


@app.post("/api/admin/regen-queue/drain-now",
          dependencies=[Depends(_require_api_key)])
async def regen_queue_drain_now():
    """R57.7 sibling — on-demand drain. Triggers a single pass of the
    consumer loop. Useful after a backfill to avoid waiting for the
    300s cycle.
    """
    from .services.regen_consumer import trigger_drain_now, queue_depth
    consumed = await trigger_drain_now()
    return {"consumed": consumed, "queue_depth_after": queue_depth()}


@app.post("/api/admin/rescue-tool-inventory",
          dependencies=[Depends(_require_api_key)])
async def rescue_tool_inventory(
    project_id: str,
    tool: str = "k6",
    request: Request = None,
):
    """R78.4 — rescue path for tools that show "inventory empty" because
    their entries never made it to GENERATED_TESTS (e.g., k6 for the example SUT
    after R71.1 typo fix shipped: 23 `.broken-*` historical quarantines
    + 0 live entries → R71.4 dispatcher SKIPs every run).

    For each requirement that should have a `tool` entry but doesn't,
    trigger the standard regenerate-by-tool flow (the same R54 modal
    operators use). Post-R78.3 the resulting entries land with
    `project_id` stamped, so R71.4 finds them on the next dispatch.

    Idempotent: requirements that already have a `tool` entry are
    skipped. Safe to call repeatedly.

    Use case: ARTA admin one-click rescue without operator UI clicks.
    The R54 modal still works for per-tool/per-requirement targeted
    regen; this endpoint is the bulk version.
    """
    from .routers.tests_state import GENERATED_TESTS
    from .routers.requirements import PROJECT_REQUIREMENTS
    from .routers.tests import regenerate_by_tool

    tool_norm = (tool or "").lower().strip()
    project_reqs = PROJECT_REQUIREMENTS.get(project_id, []) or []
    if not project_reqs:
        return {
            "status": "no_requirements",
            "project_id": project_id,
            "tool": tool_norm,
            "message": f"No requirements registered for project {project_id}",
        }

    # Find requirements that should have a `tool` entry but don't.
    existing_for_tool: set[str] = set()
    for t in GENERATED_TESTS:
        if not isinstance(t, dict):
            continue
        if (t.get("automation_tool") or t.get("tool") or "").lower() != tool_norm:
            continue
        if (t.get("project_id") or "") != project_id:
            continue
        rid = t.get("requirement_id")
        if isinstance(rid, str):
            existing_for_tool.add(rid)

    missing_reqs: list[str] = []
    for r in project_reqs:
        if not isinstance(r, dict):
            continue
        rid = r.get("req_id") or r.get("id")
        if isinstance(rid, str) and rid not in existing_for_tool:
            missing_reqs.append(rid)

    if not missing_reqs:
        return {
            "status": "no_work",
            "project_id": project_id,
            "tool": tool_norm,
            "requirements_with_existing_entry": len(existing_for_tool),
            "message": f"All requirements already have a {tool_norm} entry.",
        }

    log.info(
        "R78.4: rescuing tool inventory for project=%s tool=%s — "
        "%d requirements missing entries (will trigger regenerate-by-tool)",
        project_id, tool_norm, len(missing_reqs),
    )

    # Delegate to the existing R54 endpoint. force=True clears any
    # stale on-disk files; the gen then runs fresh. Iterate per-req so
    # ONE bad LLM call doesn't poison the whole batch.
    succeeded: list[str] = []
    failed: list[dict] = []
    for rid in missing_reqs:
        try:
            res = await regenerate_by_tool(
                project_id=project_id,
                tool=tool_norm,
                force=True,
                requirement_id=rid,
                request=request,
            )
            if isinstance(res, dict) and res.get("succeeded"):
                succeeded.append(rid)
            else:
                failed.append({"requirement_id": rid, "result": res})
        except Exception as exc:
            failed.append({"requirement_id": rid, "error": str(exc)[:200]})
            log.warning(
                "R78.4: regenerate-by-tool failed for %s/%s: %s",
                rid, tool_norm, exc,
            )

    return {
        "status": "completed",
        "project_id": project_id,
        "tool": tool_norm,
        "requirements_targeted": len(missing_reqs),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_requirements": succeeded[:50],
        "failed_requirements": failed[:50],
    }


@app.post("/api/admin/clear-github-cache",
          dependencies=[Depends(_require_api_key)])
async def clear_github_cache(
    project_id: str | None = None,
    request: Request = None,
):
    """R105.A — flush the on-disk + in-memory GitHub API cache.

    R104.B + R105.A pre-fetch SUT repo trees + file contents via the
    GitHub REST API and cache them under `.arta/github_cache/` with a
    24h TTL. The cache eliminates the rate-limit risk for repeat regens
    (live evidence: 189 API calls per regen batch × 19 reqs ≈ 3,591
    calls without caching → 403 rate-limit ceiling hit within minutes).

    Operators call this endpoint after pushing significant changes to
    the SUT repos (e.g. new routes, renamed components) to force the
    next regen to re-fetch from GitHub instead of using stale cache.

    Returns `{cleared_count, cache_dir, project_id}`. When
    `project_id` is provided, only clears entries whose repo set is
    declared in that project's integrations (best-effort match by key
    hash prefix; the cache key is sha256 of `repo:branch:operation:args`
    so we can't reverse-engineer the project from the filename — this
    is a full flush in the project-scoped path, which is correct
    behavior for a one-shot operator action).
    """
    from ..agents.github_context import (
        _R105_CACHE_DIR as _cache_dir,
        _r105_a_cache_clear,
    )
    cleared = _r105_a_cache_clear(project_id)
    return {
        "status": "ok",
        "cleared_count": cleared,
        "cache_dir": str(_cache_dir),
        "project_id": project_id,
    }


@app.post("/api/admin/sweep-replaceme",
          dependencies=[Depends(_require_api_key)])
async def sweep_replaceme(
    project_id: str,
    dry_run: bool = False,
):
    """R145.A.2 — sweep on-disk Newman items for REPLACE_ME-tainted URLs.

    Walks `src/automation/newman/*.json` + `.arta/regen_queue/applied/*.json`
    and substitutes placeholder tokens (REPLACE_ME / REPLACE-ME /
    REPLACEME / __ARTA_UNSET_*) from project env_block.variables. On
    failed substitution, stamps `info._r145_a_replaceme_unresolved=true`
    so the existing dispatcher filter at execution.py emits BLOCKED.

    Pre-R145.A: 9+ the example SUT captured paths carried REPLACE_ME literals;
    Newman items dispatched against `/api/collection/REPLACE_ME/...`
    URLs that the SUT can't serve → 2158 × 500 / 162 × 400 / 57 × 404
    cluster in Iter 4.

    Operator-controlled override of R145.A.3 auto-purge (which fires
    at startup / post-paste / pre-smoke automatically).

    Returns the audit dict: `{newman_files_scanned, items_substituted,
    items_blocked, items_unchanged, samples[:5]}`.
    """
    return _r145_a_2_sweep_disk(project_id, dry_run=dry_run)


@app.post("/api/admin/bulk-regen-newman-for-bearer",
          dependencies=[Depends(_require_api_key)])
async def bulk_regen_newman_for_bearer(
    project_id: str,
    request: Request = None,
):
    """R93.2 — bulk regen for Newman specs missing Authorization headers.

    Pre-R93.2 the R91.A predicate gate was wrong for cookie-auth
    projects (the example SUT): R91.A's Bearer-injection silently disabled →
    20 of 30 Newman specs emitted Cookie-only → 401-wall against
    api.example.internal. R93.1 fixed the predicate; R93.2 propagates
    the fix onto disk by triggering regen for each Newman spec whose
    script_content lacks "Authorization" while the project's R93.1
    predicate says Bearer is required.

    Mission: *"execute test scripts flawlessly"* — R93.1 alone doesn't
    fix existing on-disk specs (regen markers only fire on test_gen_bug
    classification; 401-noise is operator_review). One-shot sweep
    explicitly triggers regen so the next run uses Bearer-injected specs.

    Idempotent: Newman specs that already carry Authorization are
    skipped. Safe to call repeatedly.

    Returns `{regenerated_count, failed_count, failed_reqs, ...}`
    for operator visibility (per R74.3 hit-list pattern).
    """
    from .routers.tests_state import GENERATED_TESTS
    from .routers.tests import regenerate_by_tool
    from ..agents.automation_engineer import AutomationEngineerAgent

    # Step 1 — check whether this project needs Bearer at all.
    # If not (legacy cookie-only), skip entirely.
    from .routers.projects import _load_projects
    projects = _load_projects() or {}
    project = projects.get(project_id) or {}
    envs = project.get("environments") or {}
    # Find any environment with api_base_url (used by R93.1 Tier 1).
    api_base_url = ""
    for _env_name, env_block in envs.items():
        if isinstance(env_block, dict):
            api_base_url = (
                env_block.get("api_base_url") or env_block.get("base_url") or ""
            )
            if api_base_url:
                break

    needs_bearer = await AutomationEngineerAgent._r93_1_needs_bearer_header(
        project_id, api_base_url,
    )
    if not needs_bearer:
        return {
            "status": "no_work",
            "project_id": project_id,
            "needs_bearer": False,
            "message": (
                "Project does not require Bearer auth per R93.1 predicate "
                "(no OpenAPI Bearer scheme + no token vars declared). Pure "
                "cookie-only auth; no regen needed."
            ),
        }

    # Step 2 — find Newman specs lacking Authorization.
    needs_regen_reqs: list[str] = []
    skipped_specs: int = 0
    for t in GENERATED_TESTS:
        if not isinstance(t, dict):
            continue
        if (t.get("automation_tool") or t.get("tool") or "").lower() != "newman":
            continue
        if (t.get("project_id") or "") != project_id:
            continue
        content = t.get("script_content") or ""
        if not isinstance(content, str):
            continue
        if "Authorization" in content:
            skipped_specs += 1
            continue
        rid = t.get("requirement_id")
        if isinstance(rid, str) and rid not in needs_regen_reqs:
            needs_regen_reqs.append(rid)

    if not needs_regen_reqs:
        return {
            "status": "no_work",
            "project_id": project_id,
            "needs_bearer": True,
            "newman_specs_already_bearer_compliant": skipped_specs,
            "message": (
                f"All {skipped_specs} Newman specs already carry Authorization. "
                f"R93.1 predicate had nothing to propagate."
            ),
        }

    log.info(
        "R93.2: triggering bulk Newman regen for project=%s — %d req(s) "
        "missing Authorization headers (%d already compliant)",
        project_id, len(needs_regen_reqs), skipped_specs,
    )

    # Step 3 — trigger regen for each requirement. force=True clears
    # stale on-disk file; regen produces a fresh spec under the R93.1+
    # R91.A path with Bearer headers injected.
    succeeded: list[str] = []
    failed: list[dict] = []
    for rid in needs_regen_reqs:
        try:
            res = await regenerate_by_tool(
                project_id=project_id,
                tool="newman",
                force=True,
                requirement_id=rid,
                request=request,
            )
            if isinstance(res, dict) and res.get("succeeded"):
                succeeded.append(rid)
            else:
                failed.append({"requirement_id": rid, "result": res})
        except Exception as exc:
            failed.append({"requirement_id": rid, "error": str(exc)[:200]})
            log.warning(
                "R93.2: regenerate-by-tool failed for %s/newman: %s",
                rid, exc,
            )

    return {
        "status": "completed",
        "project_id": project_id,
        "needs_bearer": True,
        "newman_specs_already_bearer_compliant": skipped_specs,
        "requirements_targeted": len(needs_regen_reqs),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_requirements": succeeded[:50],
        "failed_requirements": failed[:50],
    }


@app.post("/api/admin/rescue-k6-quarantine",
          dependencies=[Depends(_require_api_key)])
async def rescue_k6_quarantine(
    project_id: str,
    request: Request = None,
):
    """R93.3 — rescue k6 specs stuck in `.broken-r90/` quarantine.

    Pre-R93.3 evidence (run 97a2e255): 11 quarantined k6 stubs, only
    4 drained by R42.6 consumer — 7 markers landed in orphans/ or
    failed fuzzy-match resolution → BLOCKED-on-stub-content forever.

    This endpoint walks `src/automation/k6/.broken-r90/` for each
    requirement that has a quarantined file, derives the requirement_id
    from the filename, and triggers regenerate_by_tool. Combined with
    R93.A's Bearer-injection backstop, the regenerated k6 specs land
    with Authorization params + valid bodies (R90.1 body-required).

    Idempotent: requirements whose k6 spec is now valid are skipped.
    """
    from .routers.tests import regenerate_by_tool
    from pathlib import Path as _Path_R93_3
    import re as _re_R93_3

    quarantine_dir = _Path_R93_3("src/automation/k6/.broken-r90")
    if not quarantine_dir.is_dir():
        return {
            "status": "no_quarantine_dir",
            "project_id": project_id,
            "message": f"{quarantine_dir} does not exist; no quarantined specs to rescue.",
        }

    quarantined = sorted(quarantine_dir.glob("*.js*"))
    if not quarantined:
        return {
            "status": "no_work",
            "project_id": project_id,
            "message": "Quarantine directory is empty.",
        }

    # Derive requirement_ids from filenames like
    #   req_am_002_performance.js.broken-r90-2026-05-13T17:30:00Z
    FNAME_RE = _re_R93_3.compile(
        r"^(req_am_\d+)_performance\.js", _re_R93_3.IGNORECASE,
    )
    reqs_to_rescue: list[str] = []
    seen: set[str] = set()
    for p in quarantined:
        m = FNAME_RE.match(p.name)
        if not m:
            continue
        slug = m.group(1).upper().replace("_", "-")
        if slug not in seen:
            seen.add(slug)
            reqs_to_rescue.append(slug)

    if not reqs_to_rescue:
        return {
            "status": "no_work",
            "project_id": project_id,
            "quarantine_files": len(quarantined),
            "message": (
                f"Found {len(quarantined)} quarantined files but none match "
                f"the expected pattern req_am_NNN_performance.js.broken-*"
            ),
        }

    log.info(
        "R93.3: rescuing %d quarantined k6 requirements for project=%s: %s",
        len(reqs_to_rescue), project_id, reqs_to_rescue[:10],
    )

    succeeded: list[str] = []
    failed: list[dict] = []
    for rid in reqs_to_rescue:
        try:
            res = await regenerate_by_tool(
                project_id=project_id,
                tool="k6",
                force=True,
                requirement_id=rid,
                request=request,
            )
            if isinstance(res, dict) and res.get("succeeded"):
                succeeded.append(rid)
            else:
                failed.append({"requirement_id": rid, "result": res})
        except Exception as exc:
            failed.append({"requirement_id": rid, "error": str(exc)[:200]})
            log.warning(
                "R93.3: regenerate-by-tool failed for %s/k6: %s",
                rid, exc,
            )

    return {
        "status": "completed",
        "project_id": project_id,
        "quarantine_files_found": len(quarantined),
        "requirements_targeted": len(reqs_to_rescue),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_requirements": succeeded[:50],
        "failed_requirements": failed[:50],
        "message": (
            f"Rescued {len(succeeded)}/{len(reqs_to_rescue)} k6 quarantined "
            f"requirements. Quarantined files remain on disk in "
            f"src/automation/k6/.broken-r90/ for audit; delete manually "
            f"after verifying the regenerated specs run cleanly."
        ),
    }


@app.post("/api/admin/bulk-regen-newman-grounding",
          dependencies=[Depends(_require_api_key)])
async def bulk_regen_newman_grounding(
    project_id: str,
    request: Request = None,
):
    """R97.C — bulk regen for Newman specs stuck on grounding_violation.

    Pre-R97.C evidence (run-a1f111): 127 Newman BLOCKED rows across 14
    reqs (req_am_001/002/003/004/006/008/009/010/012/013/014/016/017/018)
    with R55.1 grounding_violation. Some are recoverable via R93.B's
    alternatives-hint (re-gen with captured-endpoint context); others
    genuinely have no API surface in the SUT (e.g. req_am_001 Google
    OAuth, where auth happens at the IdP not on api.example.internal).

    Mission: *"generate high quality test cases"* — R97.C bulk regen
    propagates R93.B to disk for the 14 stale reqs. The classifier
    `_r97_c_classify_irrecoverable` (wired in automation_engineer at
    R55.1 stamp site) tags each regen's outcome: succeeded → spec gains
    grounded endpoints; no_api_surface → architectural truth surfaces
    on dashboard as "Recommend UI-only test (E2E)".

    Idempotent: Newman specs without `info._grounding_violations` are
    skipped. Serial per-req throttling (asyncio.sleep(2)) avoids R94.5
    `generation_in_flight` lock conflicts.

    Returns `{requirements_targeted, succeeded, failed, ...}`.
    """
    from .routers.tests_state import GENERATED_TESTS
    from .routers.tests import regenerate_by_tool
    import asyncio as _asyncio_R97_C
    import json as _json_R97_C
    from pathlib import Path as _Path_R97_C

    # Step 1 — walk Newman specs on disk; collect reqs whose specs carry
    # _grounding_violations. Reading from disk (not GENERATED_TESTS) is
    # authoritative because gen-time stamps live in info._grounding_violations.
    newman_dir = _Path_R97_C("src/automation/newman")
    if not newman_dir.is_dir():
        return {
            "status": "no_newman_dir",
            "project_id": project_id,
            "message": f"{newman_dir} does not exist.",
        }

    needs_regen_reqs: list[str] = []
    seen_reqs: set[str] = set()
    inspected = 0
    for spec_path in sorted(newman_dir.glob("req_am_*_api*.json")):
        inspected += 1
        try:
            doc = _json_R97_C.loads(spec_path.read_text())
        except Exception:
            continue
        info = (doc.get("info") or {}) if isinstance(doc, dict) else {}
        if not info.get("_grounding_violations"):
            continue
        import re as _re_R97_C
        m = _re_R97_C.match(r"^(req_am_\d+)_api", spec_path.name, _re_R97_C.IGNORECASE)
        if not m:
            continue
        slug = m.group(1).upper().replace("_", "-")
        if slug not in seen_reqs:
            seen_reqs.add(slug)
            needs_regen_reqs.append(slug)

    if not needs_regen_reqs:
        return {
            "status": "no_work",
            "project_id": project_id,
            "specs_inspected": inspected,
            "message": (
                f"Inspected {inspected} Newman specs; none carry "
                f"_grounding_violations stamps. Nothing to regen."
            ),
        }

    log.info(
        "R97.C: triggering bulk Newman regen for grounding-blocked specs "
        "in project=%s — %d req(s): %s",
        project_id, len(needs_regen_reqs), needs_regen_reqs[:10],
    )

    succeeded: list[str] = []
    failed: list[dict] = []
    no_api_surface: list[str] = []
    for rid in needs_regen_reqs:
        try:
            res = await regenerate_by_tool(
                project_id=project_id,
                tool="newman",
                force=True,
                requirement_id=rid,
                request=request,
            )
            if isinstance(res, dict) and res.get("succeeded"):
                succeeded.append(rid)
                # Post-regen: re-read the spec to see if R97.C classifier
                # marked it as no_api_surface. This is the architectural
                # truth signal — operator dashboard will show the row as
                # "UI-only test recommended".
                try:
                    spec_glob = list(newman_dir.glob(
                        f"{sanitize_req_id(rid)}_api*.json"
                    ))
                    if spec_glob:
                        doc2 = _json_R97_C.loads(spec_glob[0].read_text())
                        info2 = (doc2.get("info") or {}) if isinstance(doc2, dict) else {}
                        if info2.get("_dispatch_block_kind") == "no_api_surface":
                            no_api_surface.append(rid)
                except Exception:
                    pass
            else:
                failed.append({"requirement_id": rid, "result": res})
        except Exception as exc:
            failed.append({"requirement_id": rid, "error": str(exc)[:200]})
            log.warning("R97.C: regenerate-by-tool failed for %s/newman: %s", rid, exc)
        # R94.5 — sleep between iterations to avoid generation_in_flight lock
        await _asyncio_R97_C.sleep(2)

    return {
        "status": "completed",
        "project_id": project_id,
        "specs_inspected": inspected,
        "requirements_targeted": len(needs_regen_reqs),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "no_api_surface_count": len(no_api_surface),
        "no_api_surface_requirements": no_api_surface[:50],
        "succeeded_requirements": succeeded[:50],
        "failed_requirements": failed[:50],
        "message": (
            f"Regenerated {len(succeeded)}/{len(needs_regen_reqs)} Newman "
            f"specs. {len(no_api_surface)} req(s) classified as "
            f"no_api_surface (UI-only test recommended)."
        ),
    }


@app.post("/api/admin/bulk-regen-newman-r111",
          dependencies=[Depends(_require_api_key)])
async def bulk_regen_newman_r111(
    project_id: str,
    request: Request = None,
):
    """R112.D — bulk regen ALL Newman specs to materialize R111.G/I impact.

    Mission framing: R111.G (validate_newman_assertion_grounded) + R111.I
    (`info._gen_metrics.body_schema_grounded` stamp) activate at GEN time.
    Newman specs currently on disk were generated PRE-R111 → R111.G's
    unknown_response_field violations aren't stamped + R111.I's
    body_schema_grounded metric is missing.

    The existing `bulk_regen_newman_grounding` filters by grounding-violation
    stamps — but R111 stamps don't exist yet on these specs (chicken-and-egg).
    R112.D regenerates ALL Newman specs unconditionally so the post-R111
    chain (R111.G assertion grounding + R111.H 5xx cascade decomposition +
    R112.B response_body classifier feed) can fully fire on the next smoke.

    Reuses R97.C's pattern: per-req serial loop with asyncio.sleep(2)
    throttle to avoid R94.5 `generation_in_flight` lock conflicts.

    Returns `{requirements_targeted, succeeded, failed, ...}`.
    """
    from .routers.tests_state import GENERATED_TESTS
    from .routers.tests import regenerate_by_tool
    from .routers.requirements import PROJECT_REQUIREMENTS
    import asyncio as _asyncio_R112_D
    from pathlib import Path as _Path_R112_D

    # Step 1 — walk Newman specs on disk; collect ALL reqs (no filter).
    newman_dir = _Path_R112_D("src/automation/newman")
    if not newman_dir.is_dir():
        return {
            "status": "no_newman_dir",
            "project_id": project_id,
            "message": f"{newman_dir} does not exist.",
        }

    seen_reqs: set[str] = set()
    target_reqs: list[str] = []
    inspected = 0
    import re as _re_R112_D
    for spec_path in sorted(newman_dir.glob("req_am_*_api*.json")):
        inspected += 1
        m = _re_R112_D.match(r"^(req_am_\d+)_api", spec_path.name, _re_R112_D.IGNORECASE)
        if not m:
            continue
        slug = m.group(1).upper().replace("_", "-")
        if slug not in seen_reqs:
            seen_reqs.add(slug)
            target_reqs.append(slug)

    if not target_reqs:
        return {
            "status": "no_work",
            "project_id": project_id,
            "specs_inspected": inspected,
            "message": f"Inspected {inspected} Newman specs; no req_am_NNN_api.json files found.",
        }

    log.info(
        "R112.D: triggering bulk Newman regen for ALL specs in project=%s — "
        "%d req(s): %s",
        project_id, len(target_reqs), target_reqs[:10],
    )

    succeeded: list[str] = []
    failed: list[dict] = []
    for rid in target_reqs:
        try:
            res = await regenerate_by_tool(
                project_id=project_id,
                tool="newman",
                force=True,
                requirement_id=rid,
                request=request,
            )
            if isinstance(res, dict) and res.get("succeeded"):
                succeeded.append(rid)
            else:
                failed.append({"requirement_id": rid, "result": str(res)[:160]})
        except Exception as exc:
            log.warning("R112.D: regen failed for %s: %s", rid, exc)
            failed.append({"requirement_id": rid, "error": str(exc)[:160]})
        # R94.5 throttle — let gen-lock release before next req
        await _asyncio_R112_D.sleep(2)

    return {
        "status": "completed",
        "project_id": project_id,
        "specs_inspected": inspected,
        "requirements_targeted": len(target_reqs),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_requirements": succeeded[:50],
        "failed_requirements": failed[:50],
        "message": (
            f"R112.D: regenerated {len(succeeded)}/{len(target_reqs)} Newman "
            f"specs (force=True, all reqs, no filter). Post-regen specs will "
            f"carry R111.G/R111.I stamps + body_schema_grounded metrics."
        ),
    }


def _req_id_to_stem_prefix(req_id: str) -> str | None:
    """R313.C (IdConventionAdapter) — inverse of execution._spec_to_requirement_id:
    map a canonical requirement id to the on-disk spec-filename stem prefix, so
    admin bulk-regen scopes to a project's OWN specs generically instead of a
    hardcoded `req_am_` glob (which silently returned no_work for every non-the example SUT
    SUT). Generic across all ARTA conventions — no SUT literal:
      REQ-XY-001 → req_am_   ; REQ-XY-012 → req_or_    (ARTA requirement ids)
      ABC-499    → kcs_      ; ABC-539    → kui_        (an SSR SUT bare Jira keys)
      XY-12345   → op_       (a vendor SUT numeric key)
    Returns None for unrecognized shapes."""
    import re
    if not req_id:
        return None
    s = str(req_id).strip().upper()
    m = re.match(r"^REQ[-_]([A-Z]{2,})[-_]\d+$", s)
    if m:
        return f"req_{m.group(1).lower()}_"
    m = re.match(r"^([A-Z]{2,5})[-_]\d+$", s)
    if m:
        return f"{m.group(1).lower()}_"
    return None


def _project_spec_stem_prefixes(project_id: str) -> list[str]:
    """R313.C — the on-disk spec-stem prefixes owned by a project, derived from its
    requirement ids in the persisted store. Empty list ⇒ caller falls back to a
    convention-agnostic scan. SSOT for scoping bulk-regen to one SUT's specs."""
    from .routers.requirements import PROJECT_REQUIREMENTS as _PR
    proj_reqs = _PR.get(project_id) or []
    return sorted({
        p for r in proj_reqs
        if (p := _req_id_to_stem_prefix((r.get("req_id") or r.get("id") or "")))
    })


@app.post("/api/admin/bulk-regen-playwright-grounding",
          dependencies=[Depends(_require_api_key)])
async def bulk_regen_playwright_grounding(
    project_id: str,
    request: Request = None,
):
    """R97.D — bulk regen for Playwright specs failing on stale 404s
    or API misuse.

    Pre-R97.D evidence (run-a1f111): 102 × `expected_ok_got_404` + 38 ×
    pw_api_misuse (toBeOK on locator, page.fixture, _test.test.info()
    .fixture). All 140 FAILs originate from PW specs generated BEFORE
    R95.2 wired R93.B alternatives-hint into PW retry AND BEFORE R95.3
    added the deterministic API-misuse linter.

    Mission: *"generate high quality test scripts"* — R97.D propagates
    R95.2 + R95.3 to on-disk specs. On regen:
    - R95.2: format_violations_as_hint(captured_endpoints=) surfaces
      valid alternatives → LLM stops hallucinating 404-prone paths.
    - R95.3: validate_playwright_api_usage() emits violations for
      `_test.test.info().fixture`, `toBeOK` on page locators, and
      `page.fixture()` → R57.1 retry-with-hint forces correction.

    Idempotent: requirements with no on-disk PW spec are skipped.
    Serial per-req throttling matches R97.C.

    Returns `{requirements_targeted, succeeded, failed, ...}`.
    """
    from .routers.tests import regenerate_by_tool
    import asyncio as _asyncio_R97_D
    from pathlib import Path as _Path_R97_D

    # Step 1 — walk on-disk PW specs to derive req_ids. (Reading from
    # GENERATED_TESTS would be ideal but that in-memory list is empty
    # post-restart until tests are loaded; on-disk scan is authoritative.)
    pw_dir = _Path_R97_D("src/automation/playwright")
    if not pw_dir.is_dir():
        return {
            "status": "no_pw_dir",
            "project_id": project_id,
            "message": f"{pw_dir} does not exist.",
        }

    # R313.C (IdConventionAdapter) — scope the scan to THIS project's spec-stem
    # prefixes (derived from its own requirement ids), not the old hardcoded
    # ending `.spec.ts` are globbed, so `.broken-*`/`.pre-*`/`.bak` quarantine
    # variants are naturally excluded. `_spec_to_requirement_id` (SSOT) resolves the
    # canonical requirement id + strips the _a11y/_chain tool suffixes.
    from .routers.execution import _spec_to_requirement_id as _spec2req_R97_D
    _stems = _project_spec_stem_prefixes(project_id)
    if _stems:
        _globs = [f"{s}*.spec.ts" for s in _stems]
    else:
        # No project reqs in the store — fall back to a convention-agnostic scan
        log.warning("R97.D/R313.C: project %s has no requirements in store; "
                    "scanning ALL on-disk PW specs (unscoped).", project_id)
        _globs = ["*.spec.ts"]
    seen_reqs: set[str] = set()
    needs_regen_reqs: list[str] = []
    _seen_paths: set[str] = set()
    for _g in _globs:
        for spec_path in sorted(pw_dir.glob(_g)):
            if spec_path.name in _seen_paths:
                continue
            _seen_paths.add(spec_path.name)
            slug = _spec2req_R97_D(spec_path.name)
            if not slug:
                continue
            if slug not in seen_reqs:
                seen_reqs.add(slug)
                needs_regen_reqs.append(slug)

    if not needs_regen_reqs:
        return {
            "status": "no_work",
            "project_id": project_id,
            "message": "No Playwright requirements found for this project.",
        }

    log.info(
        "R97.D: triggering bulk Playwright regen for project=%s — %d req(s)",
        project_id, len(needs_regen_reqs),
    )

    succeeded: list[str] = []
    failed: list[dict] = []
    for rid in needs_regen_reqs:
        try:
            res = await regenerate_by_tool(
                project_id=project_id,
                tool="playwright",
                force=True,
                requirement_id=rid,
                request=request,
            )
            if isinstance(res, dict) and res.get("succeeded"):
                succeeded.append(rid)
            else:
                failed.append({"requirement_id": rid, "result": res})
        except Exception as exc:
            failed.append({"requirement_id": rid, "error": str(exc)[:200]})
            log.warning("R97.D: regenerate-by-tool failed for %s/playwright: %s", rid, exc)
        await _asyncio_R97_D.sleep(2)

    return {
        "status": "completed",
        "project_id": project_id,
        "requirements_targeted": len(needs_regen_reqs),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "succeeded_requirements": succeeded[:50],
        "failed_requirements": failed[:50],
        "message": (
            f"Regenerated {len(succeeded)}/{len(needs_regen_reqs)} Playwright "
            f"specs via R95.2 + R95.3 retry path."
        ),
    }


@app.post("/api/admin/backfill-test-entry-project-id",
          dependencies=[Depends(_require_api_key)])
async def backfill_test_entry_project_id():
    """R78.3 backfill — one-shot admin endpoint to stamp `project_id`
    on existing GENERATED_TESTS entries that pre-date the field.

    Pre-R78.3, test_entry dicts were built without `project_id`. After
    the fix lands, the SOURCE write paths stamp the field correctly,
    but the existing 200+ entries from prior runs still lack it. R71.4
    k6 filter + R77.6.β path-param fill + any project-scoped consumer
    filters those entries out → invisible to dispatch.

    Resolution: walk the in-memory PROJECT_REQUIREMENTS store. For each
    requirement, derive its project_id, then update all GENERATED_TESTS
    entries whose `requirement_id` matches. Persist the updated list.

    Idempotent: entries that already carry a non-empty `project_id`
    are skipped. Safe to call repeatedly.
    """
    from .routers.tests_state import GENERATED_TESTS
    from .routers.tests_helpers import _save_tests_json
    from .routers.requirements import PROJECT_REQUIREMENTS

    # Build req_id → project_id index from both UUID and slug keys.
    req_to_project: dict[str, str] = {}
    for pid, reqs in (PROJECT_REQUIREMENTS or {}).items():
        for r in (reqs or []):
            if not isinstance(r, dict):
                continue
            rid_uuid = r.get("id")
            rid_slug = r.get("req_id")
            r_project = r.get("project_id") or pid
            if isinstance(rid_uuid, str) and r_project:
                req_to_project[rid_uuid] = r_project
            if isinstance(rid_slug, str) and r_project:
                req_to_project[rid_slug] = r_project

    updated = 0
    by_tool: dict[str, int] = {}
    skipped_already_set = 0
    unresolved: list[str] = []
    for t in GENERATED_TESTS:
        if not isinstance(t, dict):
            continue
        if t.get("project_id"):
            skipped_already_set += 1
            continue
        rid = t.get("requirement_id")
        if not isinstance(rid, str):
            continue
        pid = req_to_project.get(rid)
        if not pid:
            unresolved.append(rid)
            continue
        t["project_id"] = pid
        updated += 1
        tool = (t.get("automation_tool") or t.get("tool") or "unknown").lower()
        by_tool[tool] = by_tool.get(tool, 0) + 1

    if updated > 0:
        # Persist with empty seed-id set — backfill writes every entry,
        # since seed IDs already carry project_id from their hardcoded
        # initialiser (or never needed it).
        try:
            _save_tests_json(seed_test_ids=set())
        except Exception as exc:
            log.warning("R78.3 backfill: _save_tests_json failed: %s", exc)

    log.info(
        "R78.3: backfill stamped project_id on %d test entry/entries "
        "(skipped %d already-set; %d unresolved requirement_ids)",
        updated, skipped_already_set, len(unresolved),
    )
    return {
        "updated": updated,
        "by_tool": by_tool,
        "skipped_already_set": skipped_already_set,
        "unresolved_requirement_ids": sorted(set(unresolved))[:20],
        "unresolved_count": len(set(unresolved)),
        "total_entries": len(GENERATED_TESTS),
    }


@app.post("/api/admin/backfill-script-content",
          dependencies=[Depends(_require_api_key)])
async def backfill_script_content():
    """R81.1.f backfill — populate `script_content` on GENERATED_TESTS
    entries that have it empty BUT have a working script on disk at
    `script_path`.

    Live evidence (post-R78.4 rescue): 5 k6 entries for the example SUT were left
    with `script_content=""` because either (a) the prefix-check at
    tests.py:2912 zeroed the content (pre-R81.1.e — now preserved), OR
    (b) R42.6 regen consumer wrote the regen'd script to disk but never
    updated the in-memory entry (pre-R81.1.c — now syncs the content
    back). This endpoint reconciles the existing entries that were
    written by the pre-fix code paths.

    Idempotent: entries that already carry non-empty script_content OR
    whose script_path is missing on disk are skipped. Safe to call
    repeatedly.
    """
    from pathlib import Path as _Path_81_1_f
    from .routers.tests_state import GENERATED_TESTS
    from .routers.tests_helpers import _save_tests_json

    updated = 0
    by_tool: dict[str, int] = {}
    skipped_already_populated = 0
    skipped_no_disk_file = 0
    skipped_no_path = 0
    repo_root = _Path_81_1_f(".").resolve()
    for t in GENERATED_TESTS:
        if not isinstance(t, dict):
            continue
        existing = t.get("script_content")
        if isinstance(existing, str) and existing.strip():
            skipped_already_populated += 1
            continue
        script_path = t.get("script_path")
        if not isinstance(script_path, str) or not script_path:
            skipped_no_path += 1
            continue
        full_path = repo_root / script_path
        if not full_path.is_file():
            skipped_no_disk_file += 1
            continue
        try:
            content = full_path.read_text()
        except Exception as exc:
            log.debug("R81.1.f: read %s failed: %s", script_path, exc)
            skipped_no_disk_file += 1
            continue
        if not content.strip():
            skipped_no_disk_file += 1
            continue
        t["script_content"] = content
        # If the entry was flagged as invalid AND the on-disk content
        # is now well-formed, clear the flag so the UI stops showing
        # the warning. The post-R81.1.e code preserves content even on
        # prefix-check failure — backfill should resolve any stale flags
        # for entries whose disk file is the source of truth.
        if "generation_error" in t:
            t.pop("generation_error", None)
        updated += 1
        tool = (t.get("automation_tool") or t.get("tool") or "unknown").lower()
        by_tool[tool] = by_tool.get(tool, 0) + 1

    if updated > 0:
        try:
            _save_tests_json(seed_test_ids=set())
        except Exception as exc:
            log.warning("R81.1.f: _save_tests_json failed: %s", exc)

    log.info(
        "R81.1.f: backfill populated script_content on %d test entry/entries "
        "(skipped %d already-populated; %d missing-disk-file; %d missing-path)",
        updated, skipped_already_populated, skipped_no_disk_file, skipped_no_path,
    )
    return {
        "updated": updated,
        "by_tool": by_tool,
        "skipped_already_populated": skipped_already_populated,
        "skipped_no_disk_file": skipped_no_disk_file,
        "skipped_no_path": skipped_no_path,
        "total_entries": len(GENERATED_TESTS),
    }


# R115.C — Vision-locate endpoint for vision-assist fallback.
# Called by `src/automation/common/vision_assist.ts` at PW runtime when
# the operator has opted into vision-assist for a project AND the
# standard DOM-locator chain has timed out. Returns a {bbox} for the
# caller's `page.mouse.click(...)` OR {bbox: null} when LLM doesn't
# support vision / project config opted out / can't locate element.
_R115_C_VISION_CALLS_TOTAL = 0
_R115_C_VISION_HITS_TOTAL = 0
_R115_C_VISION_LATENCY_MS: list[int] = []


@app.post("/api/internal/vision-locate",
          dependencies=[Depends(_require_api_key)])
async def vision_locate(request: Request):
    """R115.C — LLM-vision element locator for PW vision-assist fallback.

    Body: {description: str, screenshot_b64: str, project_id: str}
    Returns: {bbox: {x, y, w, h} | null, source: str, latency_ms: int}

    Cost discipline:
      - Operator opts in per project (`integrations.vision_assist_enabled`)
      - PW caller gates the call BEHIND a 5s DOM fast-path
      - Bounded ≤1 call per timeout (NOT per action like browser-use)
      - LLM costs ≤1 message per call; max-tokens cap at 200

    LLM provider routing:
      - If project's LLM client supports vision (Claude Opus 4.7 + claude-3-5-sonnet
        and later; Gemini Pro Vision; GPT-4o) → call with multimodal message
      - Otherwise → return {bbox: null, source: "no_vision_capable_llm"} gracefully
        so the caller's existing logic still runs (vision-assist never poisons
        the test; it only ADDS a recovery path)
    """
    global _R115_C_VISION_CALLS_TOTAL, _R115_C_VISION_HITS_TOTAL, _R115_C_VISION_LATENCY_MS
    import time as _t_r115_c
    _t0 = _t_r115_c.monotonic()
    _R115_C_VISION_CALLS_TOTAL += 1

    try:
        body = await request.json()
    except Exception:
        return {"bbox": None, "source": "bad_request", "latency_ms": 0}

    description = (body.get("description") or "").strip()
    screenshot_b64 = body.get("screenshot_b64") or ""
    project_id = body.get("project_id") or ""

    if not description or not screenshot_b64:
        return {"bbox": None, "source": "missing_inputs", "latency_ms": 0}

    # Resolve the LLM client. Anthropic AsyncAnthropic supports vision via
    # message blocks with type=image; Ollama doesn't (returns null cleanly).
    llm_cli = getattr(request.app.state, "llm_client", None) or getattr(
        request.app.state, "anthropic", None
    )
    provider = getattr(request.app.state, "llm_provider", None) or ""

    if llm_cli is None or provider not in ("anthropic", "claude_code"):
        # Ollama / no LLM / non-vision-capable → graceful skip
        return {
            "bbox": None,
            "source": f"no_vision_capable_llm(provider={provider or 'none'})",
            "latency_ms": int((_t_r115_c.monotonic() - _t0) * 1000),
        }

    # Build the multimodal message. Claude vision expects type=image with
    # base64-encoded data + media_type. The model returns JSON with bbox.
    try:
        sys_prompt = (
            "You are a precise UI-element locator. Given a screenshot and an "
            "element description, return JSON `{bbox: {x, y, w, h}}` with the "
            "bounding-box center of the click target in PIXEL coordinates of "
            "the screenshot. If the element is not visible, return "
            "`{bbox: null}`. Return ONLY the JSON, no prose."
        )
        user_msg_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
            {
                "type": "text",
                "text": f"Locate this element: {description}",
            },
        ]

        # Anthropic SDK async client
        try:
            from anthropic import AsyncAnthropic
            is_anthropic_async = isinstance(llm_cli, AsyncAnthropic)
        except ImportError:
            is_anthropic_async = False

        if is_anthropic_async:
            resp = await llm_cli.messages.create(
                model="claude-opus-4-7",
                max_tokens=200,
                system=sys_prompt,
                messages=[{"role": "user", "content": user_msg_content}],
            )
            # resp.content is a list of TextBlock objects
            text_out = ""
            for block in resp.content:
                if hasattr(block, "text"):
                    text_out += block.text
        else:
            # ClaudeCLIClient / ClaudeOAuthClient — try generic .complete()
            text_out = ""
            if hasattr(llm_cli, "complete"):
                resp = await llm_cli.complete(
                    sys_prompt + "\n\n" + description,
                    images=[screenshot_b64],
                    max_tokens=200,
                )
                text_out = resp if isinstance(resp, str) else (
                    getattr(resp, "text", None) or ""
                )

        # Parse the JSON response
        import json as _json_r115_c
        import re as _re_r115_c
        match = _re_r115_c.search(r'\{[^{}]*"bbox"[^{}]*\}', text_out, _re_r115_c.DOTALL)
        bbox = None
        if match:
            try:
                parsed = _json_r115_c.loads(match.group(0))
                _b = parsed.get("bbox")
                if isinstance(_b, dict) and all(
                    isinstance(_b.get(k), (int, float)) for k in ("x", "y", "w", "h")
                ):
                    bbox = {
                        "x": int(_b["x"]),
                        "y": int(_b["y"]),
                        "w": int(_b["w"]),
                        "h": int(_b["h"]),
                    }
            except Exception:
                pass

        latency_ms = int((_t_r115_c.monotonic() - _t0) * 1000)
        _R115_C_VISION_LATENCY_MS.append(latency_ms)
        if len(_R115_C_VISION_LATENCY_MS) > 500:
            _R115_C_VISION_LATENCY_MS = _R115_C_VISION_LATENCY_MS[-500:]
        if bbox:
            _R115_C_VISION_HITS_TOTAL += 1
        log.info(
            "R115.C vision-locate: project=%s desc=%r found=%s latency=%dms",
            project_id, description[:60], bbox is not None, latency_ms,
        )
        return {
            "bbox": bbox,
            "source": "anthropic_vision" if is_anthropic_async else "llm_complete",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        log.warning("R115.C vision-locate: LLM call failed: %s", exc)
        return {
            "bbox": None,
            "source": f"llm_error({type(exc).__name__})",
            "latency_ms": int((_t_r115_c.monotonic() - _t0) * 1000),
        }


@app.get("/api/internal/vision-locate-telemetry",
         dependencies=[Depends(_require_api_key)])
async def vision_locate_telemetry():
    """R115.C cost telemetry — calls/hits/P95 latency for operator dashboard."""
    _lat = sorted(_R115_C_VISION_LATENCY_MS)
    p95 = _lat[int(len(_lat) * 0.95)] if _lat else 0
    p50 = _lat[len(_lat) // 2] if _lat else 0
    return {
        "calls_total": _R115_C_VISION_CALLS_TOTAL,
        "hits_total": _R115_C_VISION_HITS_TOTAL,
        "hit_rate": (
            round(_R115_C_VISION_HITS_TOTAL / _R115_C_VISION_CALLS_TOTAL * 100, 1)
            if _R115_C_VISION_CALLS_TOTAL else 0.0
        ),
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "sample_count": len(_lat),
    }


@app.post("/api/webhooks/jira")
async def jira_webhook(request: Request):
    """R55.10 — receive Jira webhooks and close the defect lifecycle.

    Atlassian fires this webhook on every issue change. We filter for
    status-transition events and update `defects.status` for any defect
    whose `jira_key` matches the issue key. Pre-R55.10 ARTA was a
    write-only system: it filed a Jira ticket (R37.4) on first
    sut_regression detection but never read back the SUT team's
    resolution. Operators saw "35 open P0s" even when 20 were already
    fixed days ago. Post-R55.10 the lifecycle is bidirectional.

    Security: when `JIRA_WEBHOOK_SECRET` env var is set, the request
    must carry `X-ARTA-Webhook-Secret: <secret>`. When unset (dev
    mode), accept all webhooks with a WARN log. Production deploys
    SHOULD set the secret.

    Idempotency: the UPDATE filters on `WHERE status <> :new_status`
    so retried webhook deliveries (Atlassian retries on 5xx) are
    no-ops. Each successful UPDATE stamps `metadata.jira_resolved_at`
    via `jsonb_set` for audit.
    """
    import os
    import json as _json_55_10
    secret = os.environ.get("JIRA_WEBHOOK_SECRET")
    if secret:
        provided = request.headers.get("x-arta-webhook-secret") or ""
        if provided != secret:
            log.warning(
                "R55.10: webhook rejected — X-ARTA-Webhook-Secret mismatch "
                "(got %r, len=%d)",
                provided[:8] + "..." if provided else "", len(provided),
            )
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_secret"},
            )
    else:
        log.warning(
            "R55.10: JIRA_WEBHOOK_SECRET not set — accepting webhook "
            "without auth (DEV MODE ONLY; production deploys MUST "
            "set this env var)",
        )

    try:
        body = await request.json()
    except Exception as exc:
        log.warning("R55.10: webhook payload parse failed: %s", exc)
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_json", "detail": str(exc)},
        )

    issue = body.get("issue") or {}
    issue_key = issue.get("key")
    status_block = (issue.get("fields") or {}).get("status") or {}
    status_cat = (
        (status_block.get("statusCategory") or {}).get("key") or ""
    ).lower()
    status_name = status_block.get("name") or ""

    if not issue_key:
        return JSONResponse(
            status_code=400,
            content={"error": "missing_issue_key"},
        )

    # Map Jira statusCategory.key → DefectStatusEnum
    if status_cat == "done":
        new_status = "resolved"
    elif status_cat == "indeterminate":   # "In Progress" family
        new_status = "in_progress"
    elif status_cat == "new":              # "To Do" / "Open"
        log.info(
            "R55.10: webhook for %s status=%s (category=new) — no-op "
            "(don't auto-reopen; operator must reopen manually)",
            issue_key, status_name,
        )
        return {
            "received": True,
            "issue_key": issue_key,
            "updated": 0,
            "no_op": True,
        }
    else:
        log.info(
            "R55.10: webhook for %s — unknown statusCategory=%r; no-op",
            issue_key, status_cat,
        )
        return {
            "received": True,
            "issue_key": issue_key,
            "updated": 0,
            "unknown_status_category": status_cat,
        }

    # Atlassian fires webhooks for any field change; only act on
    # status transitions. If a changelog is provided, require a
    # status item; absence of changelog (e.g., test webhook from
    # the Atlassian admin UI) → proceed.
    changelog_items = ((body.get("changelog") or {}).get("items")) or []
    if changelog_items:
        status_changed = any(
            (it.get("field") or "").lower() == "status"
            for it in changelog_items if isinstance(it, dict)
        )
        if not status_changed:
            return {
                "received": True,
                "issue_key": issue_key,
                "updated": 0,
                "no_status_change": True,
            }

    # Update defects table
    from .db_adapter import try_db
    from sqlalchemy import text
    updated = 0
    async with try_db() as db:
        if db:
            try:
                result = await db.execute(
                    text(
                        """
                        UPDATE defects
                           SET status = CAST(:new_status AS defect_status),
                               metadata = jsonb_set(
                                   COALESCE(metadata, '{}'::jsonb),
                                   '{jira_resolved_at}',
                                   to_jsonb(now()::text),
                                   true
                               )
                         WHERE jira_key = :jira_key
                           AND status <> CAST(:new_status AS defect_status)
                        """,
                    ),
                    {"new_status": new_status, "jira_key": issue_key},
                )
                updated = result.rowcount or 0
                await db.commit()
                if updated:
                    log.info(
                        "R55.10: webhook for %s → status=%s; updated %d defect row(s)",
                        issue_key, new_status, updated,
                    )
                else:
                    log.info(
                        "R55.10: webhook for %s → status=%s; no defects matched "
                        "(either no jira_key=%s in DB, or status already %s)",
                        issue_key, new_status, issue_key, new_status,
                    )
            except Exception as exc:
                log.warning(
                    "R55.10: defect update failed for %s: %s",
                    issue_key, exc,
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
        else:
            log.warning("R55.10: DB unavailable — webhook recorded but defect not updated")

    return {
        "received": True,
        "issue_key": issue_key,
        "new_status": new_status,
        "updated": updated,
    }


@app.get("/api/state/health")
async def state_health():
    """R32.4 — health endpoint for the durable state layer.

    Reports whether the Redis-backed `durable_state` store is reachable.
    The frontend reads this to render a `degraded` banner when state
    won't survive container restart (single-worker dev mode is fine;
    multi-worker production with Redis-down is operator-actionable).

    Returns:
        {"redis": "ok" | "unavailable" | "error:...", "degraded": bool}
    """
    try:
        from .services.durable_state import health as _ds_health
        return await _ds_health()
    except Exception as exc:
        return {"redis": f"error:{type(exc).__name__}", "degraded": True,
                "detail": str(exc)[:200]}


def _is_actually_running(run: dict) -> bool:
    """R-StaleAgents — true only if (a) status is running, (b) the run
    started recently enough to plausibly still be active.

    Pre-fix `_REAL_RUNS` accumulated stale entries (status=running) when
    a run terminalised in a prior process and got rehydrated from
    active_runs without checking test_runs.status. The dashboard's
    "Execution Agent: running" badge then stayed lit indefinitely.

    Cap at 6 hours: any run that's "been running" for longer is almost
    certainly stale (longest legitimate run we've observed is ~25 min;
    6h leaves a 14× safety margin).
    """
    if (run.get("status") or "").lower() != "running":
        return False
    started = run.get("started_at")
    if not started:
        return True   # status=running with no start time — trust the flag
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        return age_h < 6
    except Exception:
        return True


@app.get("/api/agents/status")
async def agents_status():
    """Return real-time agent status inferred from running tasks."""
    from .routers.tests import _GENERATE_ALL_JOBS
    from .routers.execution import _REAL_RUNS

    gen_running = any(j.get("status") == "running" for j in _GENERATE_ALL_JOBS.values())
    exec_running = any(_is_actually_running(r) for r in _REAL_RUNS.values())

    agents = [
        {"name": "Risk Analyzer", "status": "running" if gen_running else "idle"},
        {"name": "Test Generator", "status": "running" if gen_running else "idle"},
        {"name": "ATDD Designer", "status": "running" if gen_running else "idle"},
        {"name": "Automation Engineer", "status": "running" if gen_running else "idle"},
        {"name": "Execution Agent", "status": "running" if exec_running else "idle"},
        {"name": "Defect Intel", "status": "idle"},
        {"name": "Self-Healing", "status": "idle"},
        {"name": "Traceability", "status": "idle"},
    ]
    return {"agents": agents, "active": sum(1 for a in agents if a["status"] == "running")}


@app.get("/api/admin/health")
async def admin_health(request: Request):
    """F3-5: Service-level health for the admin page + degraded banner.

    Probes each backing dependency and reports:
      services: [{name, status, latency_ms, detail}]
      degraded: true when ANY required service is Disconnected.

    Cheap probes only — runs on every poll (60s cadence from frontend banner).
    """
    import time as _time
    services: list[dict] = []

    def _ms(t0: float) -> int:
        return int((_time.perf_counter() - t0) * 1000)

    # PostgreSQL
    pg_t = _time.perf_counter()
    try:
        from ..db.session import async_session_factory
        from sqlalchemy import text as _text
        async with async_session_factory() as _db:
            await _db.execute(_text("SELECT 1"))
        services.append({"name": "PostgreSQL", "status": "Connected", "latency_ms": _ms(pg_t), "detail": ""})
    except Exception as e:
        services.append({"name": "PostgreSQL", "status": "Disconnected", "latency_ms": _ms(pg_t), "detail": str(e)[:120]})

    # Neo4j
    neo_t = _time.perf_counter()
    neo_drv = getattr(request.app.state, "neo4j", None)
    if neo_drv is None:
        services.append({"name": "Neo4j", "status": "Disconnected", "latency_ms": 0, "detail": "driver not initialised"})
    else:
        try:
            async with neo_drv.session() as s:
                await s.run("RETURN 1")
            services.append({"name": "Neo4j", "status": "Connected", "latency_ms": _ms(neo_t), "detail": ""})
        except Exception as e:
            services.append({"name": "Neo4j", "status": "Disconnected", "latency_ms": _ms(neo_t), "detail": str(e)[:120]})

    # Redis
    redis_t = _time.perf_counter()
    redis_cli = getattr(request.app.state, "redis", None)
    if redis_cli is None:
        services.append({"name": "Redis", "status": "Disconnected", "latency_ms": 0, "detail": "client not initialised"})
    else:
        try:
            await redis_cli.ping()
            services.append({"name": "Redis", "status": "Connected", "latency_ms": _ms(redis_t), "detail": ""})
        except Exception as e:
            services.append({"name": "Redis", "status": "Disconnected", "latency_ms": _ms(redis_t), "detail": str(e)[:120]})

    # ChromaDB (presence-only — chroma client doesn't expose a quick ping)
    chroma_cli = getattr(request.app.state, "chroma", None)
    services.append({
        "name": "ChromaDB",
        "status": "Connected" if chroma_cli else "Disconnected",
        "latency_ms": 0,
        "detail": "" if chroma_cli else "client not initialised",
    })

    # LLM provider
    llm_cli = getattr(request.app.state, "llm_client", None) or getattr(request.app.state, "anthropic", None)
    provider = getattr(request.app.state, "llm_provider", None)
    services.append({
        "name": "LLM Provider",
        "status": "Connected" if llm_cli else "Disconnected",
        "latency_ms": 0,
        "detail": provider or "no provider available",
    })

    # Phase 5.5 — Jira integration. When Disconnected, defects are still
    # classified by DefectIntelAgent but auto-file is silently no-op'd; the
    # detail string surfaces the reason so operators see "auth failed" /
    # "config missing" instead of confused defect cards.
    jira_status = getattr(request.app.state, "jira_status", None) or {"available": False, "reason": "init not run"}
    services.append({
        "name": "Jira",
        "status": "Connected" if jira_status.get("available") else "Disconnected",
        "latency_ms": 0,
        "detail": jira_status.get("reason", ""),
    })

    # Phase 5.5 follow-up: only required-service outages trip the global banner.
    # Optional integrations (Jira / Slack / Teams) reporting "Disconnected" are
    # expected for unconfigured installs and must NOT degrade the platform.
    # Per-feature degradation paths handle Neo4j (Phase 5.3 coverage_degraded),
    # Redis (SSE fallback), ChromaDB (RAG-only), and Jira (defect_intel auto-file
    # silently no-ops with a clear log line + admin-page detail).
    REQUIRED_SERVICES = {"PostgreSQL", "LLM Provider"}
    degraded = any(
        s["status"] == "Disconnected"
        for s in services
        if s["name"] in REQUIRED_SERVICES
    )

    return {
        "services": services,
        "degraded": degraded,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


from .routers import requirements

@app.get("/api/dashboard")
async def dashboard(project_id: str | None = None):
    """Aggregated dashboard metrics — all fields from the same authoritative sources
    as the Quality Score page so both pages always show consistent data."""
    from .routers.requirements import list_requirements
    from .routers.defects import list_defects
    from .routers.gates import get_latest_quality_score

    # Coverage: dynamic from GENERATED_TESTS (project-aware, same source as Quality Score page)
    req_data = await list_requirements(project_id=project_id)
    reqs_list = req_data.get("requirements", [])
    total = len(reqs_list)
    coverage = round(sum(r.get("coverage_pct", 0) for r in reqs_list) / max(total, 1), 1)

    # Quality score + pass_rate: from gates/latest (composite formula, same as Quality Score page)
    quality_data = await get_latest_quality_score(project_id=project_id)
    quality_score = quality_data.get("score") or 0
    dims = quality_data.get("dimensions") or []
    pass_rate_dim = next((d for d in dims if d.get("name") == "Pass Rate"), None)
    pass_rate = float(pass_rate_dim["score"]) if pass_rate_dim else 0

    # Defects: from list_defects (project-aware)
    defect_data = await list_defects(project_id=project_id)
    all_defects = defect_data.get("defects", [])
    open_defects = sum(1 for d in all_defects if d.get("status") == "open")
    p0_defects = sum(
        1 for d in all_defects
        if d.get("status") == "open" and d.get("severity") == "P0"
    )

    # Pipeline: latest run info from DB if available
    pipeline: dict = {"build_id": None, "stages": []}
    try:
        from ..db.session import async_session_factory
        from sqlalchemy import text as _text
        async with async_session_factory() as _db:
            _pid_f = "AND project_id = CAST(:pid AS uuid)" if project_id else ""
            _params: dict = {"pid": project_id} if project_id else {}
            _row = (await _db.execute(_text(f"""
                SELECT run_id, gate_decision FROM test_runs
                WHERE completed_at IS NOT NULL {_pid_f}
                ORDER BY completed_at DESC LIMIT 1
            """), _params)).first()
            if _row:
                pipeline = {
                    "build_id": str(_row[0])[:8],
                    "stages": [
                        {"name": "Build",    "status": "done"},
                        {"name": "AI Gen",   "status": "done"},
                        {"name": "Execute",  "status": "done"},
                        {"name": "Security", "status": "done"},
                        {"name": "Perf",     "status": "done"},
                        {"name": "Gate",     "status": "done"},
                        {"name": "Deploy",   "status": "wait"},
                    ],
                }
    except Exception:
        pass

    # P0/P1 coverage: compute from requirements by priority
    p0_reqs = [r for r in reqs_list if r.get("priority") == "P0"]
    p1_reqs = [r for r in reqs_list if r.get("priority") == "P1"]
    p0_coverage_pct = round(sum(r.get("coverage_pct", 0) for r in p0_reqs) / max(len(p0_reqs), 1), 1) if p0_reqs else None
    p1_coverage_pct = round(sum(r.get("coverage_pct", 0) for r in p1_reqs) / max(len(p1_reqs), 1), 1) if p1_reqs else None

    return {
        "quality_score": quality_score,
        "coverage_pct": coverage,
        "pass_rate": pass_rate,
        "open_defects": open_defects,
        "p0_defects": p0_defects,
        "total_scenarios": total,
        "active_agents": ["risk-analyzer", "test-generator", "execution-agent"],
        "pipeline": pipeline,
        "p0_coverage_pct": p0_coverage_pct,
        "p1_coverage_pct": p1_coverage_pct,
        "p0_count": len(p0_reqs),
        "p1_count": len(p1_reqs),
    }
