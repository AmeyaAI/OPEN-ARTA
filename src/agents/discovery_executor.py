"""Phase B1 helper — runs Stage 2.5 (UI Discovery) end-to-end.

The orchestrator's `_run_ui_discovery` is the pipeline insertion point;
this module owns the actual sequence:

  1. Resolve the Playwright spec set + per-run HAR output path.
  2. Spawn `npx playwright test --project=discovery` against the SUT.
     Phase I8 retry: one retry on transient launch failure (non-zero exit
     with no test results); no retry on test-assertion failure.
  3. Parse the HAR via `sut_onboarding._ingest_har` (which redacts via
     Phase I1 + caps via Phase I2 internally).
  4. Harvest env vars + endpoint shapes via `api_discovery.harvest_envvars_from_har`.
  5. Persist captured endpoints (.arta/discovered_endpoints/{project_id}.json).
  6. Persist harvested env-var values via `bulk_add_environment_variables`
     (never overwrites operator-set values; Phase I3 idempotency guard).
  7. Stamp `discovery_settings` bookkeeping (last_discovery_at, …) on the
     project record.

Returns the harvest dict the orchestrator stamps onto `ctx.discovery_result`.

Note on test scope: for v1 we run all Playwright specs the orchestrator
generated for THIS workflow (a per-PR or per-requirement subset). Smarter
selection (smoke-only, top-N by AC priority) is deferred to a follow-up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .sanitize import sanitize_req_id  # R134.H — sanitize_req_id SSoT

log = logging.getLogger("arta.discovery_executor")


def _auth_profile_locked(env_block: dict | None) -> bool:
    """M4 — a SOLVED per-SUT auth profile is AUTHORITATIVE: discovery's only-when-
    absent auto-derive must NOT re-derive or overwrite it, so it stays solved across
    sessions and is never silently re-derived/downgraded. A profile is authoritative
    when ANY holds (all SUT-agnostic "solved" signals):
      - explicit `auth.locked` (operator/UI lock);
      - `auth.refresh._source_corrected` (an operator-corrected refresh — the same
        signal the per-block guards already honor, now for the WHOLE profile);
      - `variables.arta_refresh_reusable` truthy (a reusable grant like a
        long-lived api_key — self-healing, nothing to re-derive).
    Killswitch ARTA_AUTH_PROFILE_UNIFY_DISABLE=1 (nothing is treated as locked →
    pre-M4 always-re-derive-when-absent behavior)."""
    if os.environ.get("ARTA_AUTH_PROFILE_UNIFY_DISABLE") == "1":
        return False
    auth = (env_block or {}).get("auth") or {}
    if auth.get("locked"):
        return True
    if isinstance(auth.get("refresh"), dict) and auth["refresh"].get("_source_corrected"):
        return True
    _vars = (env_block or {}).get("variables") or {}
    if isinstance(_vars, list):
        _vars = {e.get("name"): e.get("value") for e in _vars if isinstance(e, dict) and e.get("name")}
    if str((_vars or {}).get("arta_refresh_reusable") or "").strip().lower() in ("1", "true"):
        return True
    return False


_DEFAULT_HAR_DIR = Path(".arta/discovery")
_DEFAULT_ENVS_DIR = Path(".arta/environments")


def _merge_file_discovery_settings(project: dict, project_id: str) -> dict:
    """R313.C — reconcile the dual project store at the executor chokepoint. Operator
    per-SUT probe config (auth_liveness_path, app_entry_routes, skip_routes, route_cap,
    …) lives in the FILE registry (`.arta/projects.json`), but the DB `Project` row has
    no such column, so a caller that builds `project` from the DB (or from an embedded
    ctx dict) loses that config. `refresh_discovery` already merges it (R221) for the
    operator-triggered path; doing the SAME merge HERE makes it hold for EVERY caller
    (orchestrator Stage 2.5, future entrypoints) — one reconciliation point instead of
    per-caller fragility. File config wins for overlapping keys; the passed dict's
    runtime bookkeeping (discovery_pending, last_discovery_at) is preserved. Idempotent
    with the R221 merge. Killswitch ARTA_DS_FILE_MERGE_DISABLE=1. Returns the (possibly
    updated) project dict."""
    if os.environ.get("ARTA_DS_FILE_MERGE_DISABLE") == "1" or not isinstance(project, dict):
        return project
    try:
        _pf = Path(os.environ.get("ARTA_PROJECTS_FILE", ".arta/projects.json"))
        if not _pf.is_file():
            return project
        _store = json.loads(_pf.read_text())
        # projects.json is keyed by id (dict) or a list of project dicts
        _fp = None
        if isinstance(_store, dict):
            _fp = _store.get(project_id) or next(
                (p for p in _store.values() if isinstance(p, dict) and p.get("id") == project_id), None)
        elif isinstance(_store, list):
            _fp = next((p for p in _store if isinstance(p, dict) and p.get("id") == project_id), None)
        _file_ds = (_fp or {}).get("discovery_settings")
        if isinstance(_file_ds, dict) and _file_ds:
            _passed = project.get("discovery_settings") if isinstance(project.get("discovery_settings"), dict) else {}
            # file config wins; runtime bookkeeping from the passed dict is preserved
            project["discovery_settings"] = {**_passed, **_file_ds}
            for _rt in ("discovery_pending", "last_discovery_at"):
                if _rt in _passed:
                    project["discovery_settings"][_rt] = _passed[_rt]
    except Exception as _exc:
        log.debug("R313.C: file discovery_settings merge skipped for %s: %s", project_id, _exc)
    return project


def _r185_build_host_resolver_rules(base_url: str | None) -> str | None:
    """R185 — build a chromium `--host-resolver-rules` MAP string for the SUT
    frontend host AND its sibling API hosts (backend.*, api.*).

    The discovery probe's chromium has BROKEN in-container DNS (the same
    asymmetry R143.D fixes for the PW execution stage), but discovery_executor
    never armed the bridge — so the probe could load the frontend (when DNS
    happened to work) but its SPA bootstrap XHRs to `backend.<host>` /
    `api.<host>` hit `net::ERR_FAILED` → the SPA redirected to /login → the
    probe wrote `auth_failed.flag` → the DOM catalog came back EMPTY
    (testid_count=0) → gen had zero grounding → hallucinated selectors.

    arta-api's own resolver works (it reaches the SUT for Newman + the refresh
    endpoint), so we resolve each host here and emit a per-host MAP rule. Mirrors
    R143.D + R183 from the execution side. Best-effort: a host that won't resolve
    is skipped. Extra hosts via ARTA_R183_EXTRA_API_HOSTS. Killswitch
    ARTA_R185_PROBE_BRIDGE_DISABLE=1.
    """
    if not base_url or os.environ.get("ARTA_R185_PROBE_BRIDGE_DISABLE") == "1":
        return None
    import socket
    from urllib.parse import urlparse

    def _resolve(host: str) -> str | None:
        try:
            for info in socket.getaddrinfo(host, None, family=socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith("127."):
                    return str(ip)
        except (socket.gaierror, OSError):
            return None
        return None

    host = urlparse(base_url).hostname
    if not host:
        return None
    fe_ip = _resolve(host)
    if not fe_ip:
        return None
    rules = [f"MAP {host}:443 {fe_ip}:443", f"MAP {host}:80 {fe_ip}:80"]
    candidates = [f"backend.{host}", f"api.{host}", *[
        h.strip() for h in (os.environ.get("ARTA_R183_EXTRA_API_HOSTS") or "").split(",")
        if h.strip()
    ]]
    mapped = []
    for ch in candidates:
        if ch == host:
            continue
        ip = _resolve(ch)
        if ip:
            rules.append(f"MAP {ch}:443 {ip}:443")
            rules.append(f"MAP {ch}:80 {ip}:80")
            mapped.append(f"{ch}->{ip}")
    if mapped:
        log.info("R185: discovery-probe chromium bridge maps %d API host(s): %s",
                 len(mapped), ", ".join(mapped))
    return ",".join(rules)


def _find_storage_state(spec_dir: Path) -> Path | None:
    """Find the most recent Playwright storage-state file the operator has
    curated for the current SUT. Returns the first existing file from a
    short candidate list, or None.

    The pattern: operators keep `<env>-storage.json` siblings in
    `.arta/environments/`. Production playwright runs (`_run_playwright`)
    pick the env-specific file from a TARGET_ENVIRONMENT lookup; for
    discovery we don't have that context, so we just take the newest
    *-storage.json.
    """
    envs_dir = Path(os.environ.get("ARTA_ENVIRONMENTS_DIR",
                                    str(_DEFAULT_ENVS_DIR))).resolve()
    if not envs_dir.is_dir():
        return None
    candidates = sorted(envs_dir.glob("*-storage.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _build_har_out_path(workflow_id: str) -> Path:
    """Per-run HAR output path. Orchestrator passes this via ARTA_HAR_OUT."""
    p = _DEFAULT_HAR_DIR / workflow_id
    p.mkdir(parents=True, exist_ok=True)
    return p / "discovery.har"


def _resolve_spec_dir(ctx: Any) -> Path:
    """Where the freshly-generated specs live. The Phase 4 generator writes
    to src/automation/playwright/{req}.spec.ts; we run with that as test-dir.
    """
    return Path(os.environ.get("ARTA_PLAYWRIGHT_DIR", "src/automation/playwright"))


async def _spawn_playwright_discovery(
    har_path: Path,
    spec_dir: Path,
    *,
    base_url: str | None = None,
    test_match: str | None = None,
    api_base_url: str | None = None,
    auth_cookie_name: str | None = None,
    auth_cookie_value: str | None = None,
    auth_bearer: str | None = None,
    auth_refresh_fulfill: dict | None = None,
    post_read_allowlist: list | None = None,
    skip_routes: list | None = None,
    app_entry_routes: list | None = None,
    fallback_route_guesses: list | None = None,
    seeded_envvars: dict[str, str] | None = None,
    storage_state_path: str | None = None,
    project_id: str | None = None,
    host_resolver_rules: str | None = None,
    frontend_routes: str | None = None,
    route_cap: int | None = None,
    bfs_depth: int | None = None,
    hardcoded_probes_disable: bool | None = None,
    auth_liveness_path: str | None = None,
    replay_keyword_filter: list | str | None = None,
) -> dict[str, Any]:
    """Spawn `npx playwright test --project=discovery`. Returns a dict
    summarising the run.

    Failures here are SOFT — we never raise. The harvest pipeline gracefully
    degrades when the HAR file is empty.
    """
    env = os.environ.copy()
    # ARTA_HAR_OUT must be ABSOLUTE: the spawned playwright runs with
    # cwd=src/automation/playwright; a relative path lands in the wrong
    # directory and the harvest reads an empty file from the API's cwd.
    env["ARTA_HAR_OUT"] = str(har_path.resolve())
    if base_url:
        env["TARGET_BASE_URL"] = base_url
    # R8 — propagate API_BASE_URL so the probe can hit the actual backend.
    # Without this, hitting `${BASE_URL}/api/v1/...` on a SPA-shipped SUT
    # 200 OK responses but no JSON IDs → no env-vars promoted.
    if api_base_url:
        env["API_BASE_URL"] = api_base_url
        env["TARGET_API_BASE_URL"] = api_base_url
    # R8 — propagate auth so the probe gets authenticated responses.
    # Without auth, the SPA serves the login page (no API calls fired).
    if auth_cookie_name and auth_cookie_value:
        env["TARGET_AUTH_COOKIE_NAME"] = auth_cookie_name
        env["TARGET_AUTH_COOKIE_VALUE"] = auth_cookie_value
    if auth_bearer:
        env["TARGET_AUTH_BEARER_TOKEN"] = auth_bearer
    # R313.C (AuthAdapter / C11) — per-SUT auth-liveness path for the R37.5
    # pre-flight. Keeps the probe's "whoami" GET SUT-agnostic instead of the old
    if auth_liveness_path:
        env["TARGET_AUTH_LIVENESS_PATH"] = auth_liveness_path
    # R265 — auth-refresh fulfilled LOCALLY (the SUT never receives the request).
    # R154.A Layer 1 aborts every non-GET, which included the SPA's own token
    # refresh → the SPA self-logged-out → every route after the first few
    # rendered Sign In → the DOM catalog was login chrome → PW specs had no real
    # selectors to ground on. Rather than relax the non-mutation guarantee, the
    # probe answers the refresh from the tokens ARTA already holds. Per-project
    # config keeps this SUT-agnostic; absent config → strict abort (fails CLOSED).
    if auth_refresh_fulfill:
        _match = (auth_refresh_fulfill.get("url_contains") or "").strip()
        _tmpl = auth_refresh_fulfill.get("response_template")
        if _match and _tmpl:
            env["TARGET_AUTH_REFRESH_MATCH"] = _match
            env["TARGET_AUTH_REFRESH_RESPONSE"] = (
                _tmpl if isinstance(_tmpl, str) else json.dumps(_tmpl)
            )
    # R266 — OPERATOR-NAMED read-POST allowlist. Some SUTs expose reads as POST;
    # R154.A's method-only abort left every feature route on a permanent spinner
    # → R180 hydration timeout → route skipped → nothing cataloged → the LLM had
    # no selectors to ground on. This is an EXPLICIT operator-maintained list of
    # exact paths, never a name heuristic (a `get*` rule would allow
    # `getOrCreateX`; the SUT's contract also declares real mutations). Only
    # `/`-prefixed strings are forwarded; absent config → probe allows nothing.
    if post_read_allowlist:
        _paths = [p for p in post_read_allowlist
                  if isinstance(p, str) and p.startswith("/")]
        if _paths:
            env["TARGET_POST_READ_ALLOWLIST"] = json.dumps(_paths)
    # R272 — PER-PROJECT walk skip-list, unioned with the global env var.
    #
    # R150.L's `ARTA_R150_SKIP_ROUTES` is read from process env ONLY, but one
    # arta-api process serves EVERY project — so one SUT's skip-list silently
    # applied to all of them (the same trap R264 avoids for hash routing).
    # POISONS every portal remote visited afterwards —
    #     with it:    /portal=36 forgetPassword=8 FEPretrip=5  Detention=5
    #     without it: /portal=36 FEPretrip=36 Reefer=36 Detention=36
    # — so ONE bad hop cost 17 feature routes their DOM. Remote->remote chains
    # are fine, so this is not walk decay; it is one poisoning navigation.
    # Union (never replace): the global env may carry another SUT's crash list.
    if skip_routes:
        _skip = [s.strip() for s in skip_routes
                 if isinstance(s, str) and s.strip().startswith("/")]
        if _skip:
            _existing = [s.strip() for s in (env.get("ARTA_R150_SKIP_ROUTES") or "").split(",")
                         if s.strip()]
            env["ARTA_R150_SKIP_ROUTES"] = ",".join(dict.fromkeys(_existing + _skip))
    # R292 — PER-PROJECT app-entry + fallback route seeds (GENERICITY fix).
    #
    # (`/ai-apps,/dashboard/insight,…`) + shell-guess fallbacks in the probe
    # SOURCE — a SUT-specific bleed in platform code. The R227 INTENT is right
    # routes?" belong in per-project config, not a string-match + literals in
    # the shared probe. Now each SUT declares its own seeds; the probe reads
    # them generically and name-detection is gone. Absent config → no seeds
    # (cold-start fallback), identical to a non-configured SUT today.
    if isinstance(app_entry_routes, list) and app_entry_routes:
        env["ARTA_R182_APP_ENTRY"] = ",".join(
            s.strip() for s in app_entry_routes if isinstance(s, str) and s.strip().startswith("/"))
    if isinstance(fallback_route_guesses, list) and fallback_route_guesses:
        env["ARTA_FALLBACK_ROUTE_GUESSES"] = ",".join(
            s.strip() for s in fallback_route_guesses
            if isinstance(s, str) and s.strip().startswith("/"))
    # vocabulary out of the platform probe into per-SUT discovery_settings. Absent
    # config → the probe's built-in structural rule + legacy keyword fallback apply.
    if replay_keyword_filter:
        _rkw = (replay_keyword_filter if isinstance(replay_keyword_filter, list)
                else str(replay_keyword_filter).split(","))
        _rkw = [s.strip() for s in _rkw if isinstance(s, str) and s.strip()]
        if _rkw:
            env["ARTA_R151B_REPLAY_KEYWORDS"] = ",".join(_rkw)
    # R8 — point the probe at the operator-curated Playwright storage
    # state file when present (`.arta/environments/<env>-storage.json`).
    # Pre-fix the probe ran with empty cookies → SPA served the login
    # page → SPA's own JS made zero API calls → HAR captured static
    # assets only → harvest empty.
    # R39.5 — caller-supplied `storage_state_path` (env-specific, resolved
    # via env_name in execute()) takes precedence over the spec_dir
    # heuristic so the probe loads the RIGHT env's storage state.
    if storage_state_path:
        env["TARGET_AUTH_STATE_PATH"] = str(Path(storage_state_path).resolve())
    else:
        storage_path = _find_storage_state(spec_dir)
        if storage_path:
            env["TARGET_AUTH_STATE_PATH"] = str(storage_path.resolve())
    # R8 — seed concrete IDs the operator already declared (e.g.
    # `organization_id`) so the probe can hit `/orgs/{id}` detail pages
    # and the harvester can canonicalise the {id}-templated endpoint shape.
    if seeded_envvars:
        for k, v in seeded_envvars.items():
            if v and v != "REPLACE_ME":
                # Set as TARGET_ENV_<KEY> so the probe can pick them up
                # without polluting the global env-var namespace.
                env[f"TARGET_ENV_{k.upper()}"] = str(v)
    if test_match:
        env["TARGET_TEST_MATCH"] = test_match
    # R86.0 — pass project_id so the probe can load captured_endpoints/
    # <pid>.json for principled SPA route discovery (real paths the SUT
    # has served before, not hardcoded guesses).
    if project_id:
        env["TARGET_PROJECT_ID"] = project_id
    # R185 — arm the chromium host-resolver bridge for the discovery probe so
    # its SPA bootstrap XHRs to backend.<host>/api.<host> resolve instead of
    # net::ERR_FAILED → /login → empty DOM catalog. playwright.config.ts
    # forwards TARGET_CHROMIUM_HOST_RESOLVER_RULES into chromium launch args.
    if host_resolver_rules:
        env["TARGET_CHROMIUM_HOST_RESOLVER_RULES"] = host_resolver_rules
    # R186 — the probe loads `.arta/frontend_routes/<pid>.json` (primary) but this
    # env var is a belt-and-suspenders channel for the resolved real routes. When
    # we feed real routes, cap the BFS to a small set of high-value routes so the
    # probe COMPLETES + flushes the HAR within the per-test timeout on slow SPAs
    # (~35s/route). Both env-overridable. Default cap 8 / timeout 420s (subprocess
    # cap is 540s, no retry for discovery).
    if frontend_routes:
        env["TARGET_FRONTEND_ROUTES"] = frontend_routes
        # Cap 8 (PROVEN reliable: nav-first → 28 testids + 82 role+names + 55
        # endpoints). Env-overridable (ARTA_R186_PROBE_ROUTE_CAP) for broader walks
        # under a stable SUT — but the default stays at the proven value (higher
        # caps were unreliable under the SUT's intermittent egress; see note above).
        # role+name catalog). Explicit route_cap/bfs_depth win over the env
        # default; crash-prone routes still bounded by ARTA_R150_SKIP_ROUTES.
        if route_cap:
            env["ARTA_R140_ROUTE_CAP"] = str(route_cap)
        if bfs_depth:
            env["ARTA_R140_BFS_DEPTH"] = str(bfs_depth)

    # 404-prone pollution into the grounding surface). UNCONDITIONAL (not gated
    # on frontend_routes — the API_PROBES loop fires regardless of BFS scope).
    # discovery_settings.hardcoded_probes_disable.
    if hardcoded_probes_disable:
        env["ARTA_R221_HARDCODED_PROBES_DISABLE"] = "1"
        env.setdefault("ARTA_R140_ROUTE_CAP", os.environ.get("ARTA_R186_PROBE_ROUTE_CAP", "8"))
        env.setdefault("ARTA_R186_PROBE_TIMEOUT_MS",
                       os.environ.get("ARTA_R186_PROBE_TIMEOUT_MS", "420000"))
    # R8 fix — do NOT set TARGET_TEST_DIR. The pre-R8 path was to set
    # `TARGET_TEST_DIR=src/automation/playwright` AND cwd to the same
    # directory, producing a double-resolved path
    # (`<cwd>/src/automation/playwright`) that contains no specs → "No
    # tests found" → empty HAR → entire harvest pipeline degrades. The
    # playwright config defaults `testDir` to `.` (the config file's own
    # directory) which is the correct location, no override needed.

    cwd = Path(spec_dir).resolve() if Path(spec_dir).is_absolute() else (
        Path.cwd() / spec_dir
    ).resolve()
    # R213.F — hardened-image compat: the security-hardened image has NO
    # /bin/sh, and `npx`/npm internally `spawn('sh')` → `spawn sh ENOENT` →
    # the discovery probe NEVER launches → empty HAR → empty harvest → the
    # project env_block resource ids (collection_id/container_name/fieldset_id
    # …) stay `REPLACE_ME` → ~215 Newman items BLOCK at dispatch on unresolved
    # path params. The PW/Newman EXECUTION paths were already node-direct
    # (execution._pw_cli_argv / _newman_cmd_prefix) but discovery still used
    # raw npx (run-598ab9: `npm error spawn sh ENOENT` → "HAR not produced").
    # Invoke `node <abs cli.js> test` instead — no shell, image stays hardened.
    # cli.js lives at the repo-root node_modules (NOT under the spec cwd), so
    # resolve it absolutely. Killswitch ARTA_PW_NODE_DIRECT_DISABLE=1 → npx.
    import shutil as _sh_disc
    _pw_argv = ["npx", "playwright", "test"]
    if os.environ.get("ARTA_PW_NODE_DIRECT_DISABLE") != "1":
        _cli_roots = [Path.cwd(), cwd, cwd.parent, cwd.parent.parent, cwd.parent.parent.parent, Path("/app")]
        for _root in _cli_roots:
            for _rel in ("node_modules/@playwright/test/cli.js", "node_modules/playwright/cli.js"):
                _cli_abs = (_root / _rel)
                if _cli_abs.is_file():
                    _pw_argv = [_sh_disc.which("node") or "node", str(_cli_abs.resolve()), "test"]
                    break
            if _pw_argv[0] != "npx":
                break
    cmd = _pw_argv + ["--project=discovery", "--reporter=list"]
    log.info("discovery_executor.spawn cmd=%s cwd=%s har=%s", shlex.join(cmd), cwd, env["ARTA_HAR_OUT"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Isolate into its OWN session/process-group. The K1 pre-flight caller
            # wraps this whole coroutine in a 60s asyncio.wait_for; when that fires
            # it CANCELS us mid-communicate() (CancelledError — NOT the TimeoutError
            # handled below), which used to abandon the node+chromium subprocess.
            # crash took uvicorn (PID 1) down with it → on-failure container restart
            # mid-run (every run died ~60s in). Own session + the finally-kill below
            # sever that link: a discovery timeout/cancel can never signal the parent.
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=540)
        except asyncio.TimeoutError:
            # R91.E — graceful HAR flush. SIGKILL would skip Playwright's
            # `finally { await context.close() }` block in
            # discovery_probe.spec.ts, leaving HAR truncated /
            # missing-recordHar-finalize and DOM sidecars unwritten.
            # SIGTERM + 10s grace gives Playwright time to flush before
            # the SIGKILL fallback. Even when the probe hits the 540s
            # cap on slow SUTs, the partial HAR is then ingestable by
            # ingest_dom_snapshots and dom_catalog updates correctly.
            proc.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=10
                )
                log.info(
                    "discovery_executor.r91_e: graceful SIGTERM flush completed (har_exists=%s)",
                    har_path.is_file(),
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                log.warning(
                    "discovery_executor.r91_e: SIGKILL after 10s grace expired (har_exists=%s)",
                    har_path.is_file(),
                )
            return {
                "exit_code": -1,
                "stdout": stdout.decode("utf-8", errors="replace")[-2000:] if stdout else "",
                "stderr": (stderr.decode("utf-8", errors="replace")[-2000:] if stderr
                           else "playwright timed out after 540s (R91.E graceful flush)"),
                "har_exists": har_path.is_file(),
            }
        finally:
            # Never leave the discovery subprocess (node + chromium) running when
            # we exit for ANY reason. Normal completion sets returncode → no-op;
            # a CANCEL from the caller's outer wait_for (or any exception) kills
            # the whole isolated group. Killing the group is safe precisely because
            # start_new_session put it in its own session — it can't reach uvicorn.
            if proc.returncode is None:
                import signal as _sig
                try:
                    os.killpg(os.getpgid(proc.pid), _sig.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
    except FileNotFoundError as exc:
        # `npx` not installed — this is a deploy-time problem, not a per-run
        # problem. Return a structured error instead of crashing the pipeline.
        log.warning("discovery_executor.npx_missing: %s", exc)
        return {"exit_code": -1, "stdout": "", "stderr": str(exc), "har_exists": False}

    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace")[-2000:],
        "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
        "har_exists": har_path.is_file(),
    }


def _detect_auth_failed_from_records(records: list[dict]) -> dict | None:
    """R39.1 — return a diagnosis dict when the HAR shows no authenticated
    API traffic, else None.

    R37.5 catches probe-side `users/me` 401 responses but misses the
    common case where the SPA serves its login page on every route
    with HTTP 200 + HTML. The probe walks all configured routes, all
    JSON endpoints return HTML (login redirect), the HAR has no
    authenticated calls, and the harvester writes envvar_values={}.
    Operator never sees the auth gap; they see "22 vars need filling".

    Detection is fast + deterministic — no SUT round-trip required:
      - Empty HAR (no records ingested) → har_empty
      - Records exist but NONE are JSON → no_json_responses
      - JSON records exist but ALL failed (4xx/5xx) → all_api_calls_failed
      - Else: harvest is plausibly real → return None.

    Returns `{reason: str, sample: str, json_count: int, total_count: int}`
    when authentication looks broken, else None.
    """
    if not records:
        return {
            "reason": "har_empty",
            "sample": "0 records ingested from HAR",
            "json_count": 0,
            "total_count": 0,
        }
    json_records = [
        r for r in records
        if isinstance(r, dict)
        and "json" in (r.get("content_type") or "").lower()
    ]
    if not json_records:
        return {
            "reason": "no_json_responses",
            "sample": (
                f"all {len(records)} HAR records were HTML/static — "
                f"likely the SPA served the login page on every route"
            ),
            "json_count": 0,
            "total_count": len(records),
        }
    auth_records = [
        r for r in json_records
        if isinstance(r.get("status"), int) and 200 <= r["status"] < 400
    ]
    if not auth_records:
        statuses = sorted({
            r.get("status") for r in json_records
            if isinstance(r.get("status"), int)
        })
        return {
            "reason": "all_api_calls_failed",
            "sample": f"all {len(json_records)} JSON responses 4xx/5xx; statuses={statuses}",
            "json_count": len(json_records),
            "total_count": len(records),
        }
    return None


def _diagnose_session_cookie(
    storage_state_path: "Path | str | None",
    cookie_name: str | None,
) -> dict | None:
    """R157 — decode the storage-state session cookie's JWT `exp` so discovery
    can report the TRUE auth blocker ("session cookie expired 45m ago") instead of
    the misleading post-probe `no_json_responses` ("SPA served the login page").

    Live evidence (run a9f772de): the probe walked one route, the SPA
    served HTML for `/api/v1/users/me`, and R39.1 stamped `no_json_responses`.
    The real cause was a cookie that had expired 45 minutes earlier — invisible
    until the operator hand-decoded the JWT. This helper surfaces it up front.

    Returns a diagnosis dict when the session cookie is an *expired* JWT, else
    None (cookie fresh, not a JWT, or no cookie found). Reuses the
    single-source `auth_refresher` JWT helpers — no duplicate decode logic.
    """
    if not storage_state_path or not Path(storage_state_path).is_file():
        return None
    try:
        from .auth_refresher import (
            _read_storage_state,
            _decode_jwt_payload,
            _is_session_expired,
            _extract_refresh_token,
        )
    except Exception:  # pragma: no cover — import guard
        return None
    storage = _read_storage_state(Path(storage_state_path))
    if not storage:
        return None
    cookie_val = None
    found_name = None
    for c in storage.get("cookies") or []:
        nm = c.get("name") or ""
        if cookie_name and nm == cookie_name:
            cookie_val, found_name = c.get("value"), nm
            break
        if not cookie_name and "token" in nm.lower():
            cookie_val, found_name = c.get("value"), nm
    if not cookie_val:
        return None
    # leeway_s=0: report only genuinely-expired cookies (don't bail on a
    # cookie with seconds left — the probe may still complete in time).
    # the wrapper; checking only the wrapper let a stale session probe (and 400).
    if not _is_session_expired(cookie_val, leeway_s=0):
        return None
    claims = _decode_jwt_payload(cookie_val) or {}
    exp = claims.get("exp")
    now = int(time.time())
    refresh_token = _extract_refresh_token(storage, None)
    ago = (now - exp) if isinstance(exp, int) else None
    return {
        "reason": "cookie_expired",
        "cookie_name": found_name,
        "expired_at": exp,
        "expired_ago_s": ago,
        "refresh_token_present": bool(refresh_token),
        "sample": (
            f"session cookie '{found_name}' is an expired JWT"
            + (f" — expired {ago}s ago (exp={exp})" if isinstance(ago, int) else "")
            + (
                "; a refresh token is on hand"
                if refresh_token
                else "; no refresh token available — operator must re-paste"
            )
        ),
    }


def _select_env_block(project: dict, environment: str | None) -> tuple[str | None, dict]:
    """REVIEW-V1: pick the env block that matches the run's `environment`.

    Resolution order:
      1. Exact match on the project's environments dict key.
      2. Suffix match (body.environment="acme-staging" → key "staging").
      3. First env block (legacy fallback — preserves pre-fix behavior).

    Returns (env_name_or_None, env_block_dict). When no env exists, returns
    (None, {}).
    """
    envs = project.get("environments") or {}
    if not isinstance(envs, dict) or not envs:
        return None, {}
    if environment and environment in envs:
        return environment, envs[environment] or {}
    if environment:
        for env_name, env_block in envs.items():
            if environment.endswith(env_name) or env_name.endswith(environment):
                return env_name, env_block or {}
    # Legacy fallback: take the first dict-shaped block. Usually correct for
    # single-env projects.
    for env_name, env_block in envs.items():
        if isinstance(env_block, dict):
            return env_name, env_block
    return None, {}


async def execute(ctx: Any, project: dict, environment: str | None = None) -> dict[str, Any]:
    """Phase B1 entrypoint. Called by `ARTAOrchestrator._run_ui_discovery`.

    `environment` is the run-level env name (e.g. "acme-staging" or
    "staging"). When provided, REVIEW-V1 uses it to pick the right env
    block from `project.environments`. When None (legacy callers), falls
    back to picking the first env — preserves prior behavior.

    Returns the harvest dict (envvar_values, endpoints, shape_catalog,
    multi_value_warnings, plus run telemetry under `_run`). Empty harvest is
    a valid return — the orchestrator stamps it as-is on `ctx.discovery_result`.
    """
    # R151.A KEYSTONE — 3-level fallback chain for workflow_id → HAR path.
    # Pre-R151.A: `getattr(ctx, "workflow_id", "no_id")` returned `None`
    # when the attribute existed but was None (e.g., non-orchestrator
    # callers like `Ctx().workflow_id=None`); `str(None)="None"` →
    # HAR landed at `.arta/discovery/None/discovery.har` instead of the
    # canonical project-scoped path. Fresh probes never updated
    # `discovered_endpoints/<pid>.json` → shape coverage stuck at 6/500.
    #
    # Post-R151.A: explicit `or` chain — ctx.workflow_id (orchestrator
    # path, R90.2 synthetic-ctx path), then project["id"] (any caller
    # that supplies a project dict), then legacy "no_id" sentinel.
    # No regression: callers passing a real workflow_id still win.
    workflow_id = str(
        getattr(ctx, "workflow_id", None)
        or (project or {}).get("id")
        or "no_id"
    )
    har_path = _build_har_out_path(workflow_id)
    spec_dir = _resolve_spec_dir(ctx)

    # REVIEW-V1: pick the matching env block instead of merging across all.
    # Pre-fix iterated `for env_block in envs.values()` and built the
    # config from the FIRST-seen value of each field — values from `local`
    # could leak into a `staging` run.
    env_name, env_block = _select_env_block(project, environment) if isinstance(project, dict) else (None, {})
    # R86.0 — define project_id EARLY so it can be propagated to the
    # Playwright discovery probe via spawn_kwargs (needed for captured-
    # endpoints-driven SPA route discovery). Pre-fix `project_id` was
    # only assigned at line ~552 after harvest completed; my R86.0
    # spawn_kwargs edit at line ~458 referenced an unbound name and
    # crashed R45.3 with "cannot access local variable 'project_id'".
    project_id = str(project.get("id", "unknown")) if isinstance(project, dict) else "unknown"

    # R313.C — reconcile the dual project store (file registry ↔ DB) at this single
    # chokepoint so operator per-SUT probe config (auth_liveness_path, app_entry_routes,
    # skip_routes, …) reaches the probe for EVERY caller, not just refresh_discovery.
    if isinstance(project, dict) and project_id != "unknown":
        project = _merge_file_discovery_settings(project, project_id)

    # R8 — pull base URLs and auth from the project's environments map.
    # The probe needs (a) the SPA homepage URL (for browser nav), (b) the
    # actual API host (most SaaS deploy backend on a separate subdomain
    # like `backend.<sut>`), (c) auth cookie/bearer so the SPA loads
    # authenticated views.
    base_url = None
    api_base_url = None
    cookie_name = None
    cookie_value = None
    bearer = None
    seeded_envvars: dict[str, str] = {}
    if isinstance(project, dict):
        # Legacy single-source: integrations.sut_base_url
        base_url = (project.get("integrations") or {}).get("sut_base_url")
    if env_block:
        base_url = base_url or env_block.get("base_url")
        api_base_url = env_block.get("api_base_url")
        auth = env_block.get("auth") or {}
        creds = auth.get("credentials") or {}
        cookie_name = creds.get("cookie_name")
        # REVIEW-V2: skip propagating redacted placeholders. ARTA stores
        # `cookie_value: "***"` in projects.json so the file can be
        # committed safely; the real cookie lives in the storage-state
        # file. Pre-fix we sent `Cookie: <name>=***` to the probe's
        # extraHTTPHeaders, which the SUT rejected on every API call.
        raw_cookie_value = creds.get("cookie_value")
        if isinstance(raw_cookie_value, str) and raw_cookie_value not in ("***", "REDACTED", "REPLACE_ME"):
            cookie_value = raw_cookie_value
        raw_bearer = creds.get("bearer_token")
        if isinstance(raw_bearer, str) and raw_bearer not in ("***", "REDACTED", "REPLACE_ME"):
            bearer = raw_bearer
        # Seed already-set IDs (skip REPLACE_ME placeholders) so
        # the probe can resolve {id} URLs.
        for k, v in (env_block.get("variables") or {}).items():
            if isinstance(v, str) and v and v != "REPLACE_ME":
                seeded_envvars[k] = v

    log.info("discovery_executor.execute env=%s base=%s api_base=%s cookie=%s seeded=%d",
             env_name, base_url, api_base_url, cookie_name, len(seeded_envvars))

    # R39.5 — pre-spawn auth state check. The probe costs ~60s wallclock;
    # producing an auth_failed.flag deterministically when we can prove
    # no usable credential exists is faster than letting the probe walk
    # SPA routes that all serve the login page. Storage state file is
    # the *primary* signal — when present, the probe authenticates via
    # cookies + localStorage from the file even if projects.json holds
    # the redacted placeholder.
    # R52 — env-name resolution bug fix. `_select_env_block` (line 211
    # `staging`. R39.5 then looked for `.arta/environments/staging-
    # storage.json` (resolved name) — but the operator's paste-auth
    # (selected name). Mismatch → "storage_state=missing" → discovery
    # refused for 3+ days running, dom_catalog stays at 0 testids,
    # Playwright stays blocked.
    #
    # R78.1 KEYSTONE — symmetric resolution using the same helper the
    # writer uses (`auth_refresher._find_storage_state_path`). The
    # helper's glob-fallback returns the newest `*-storage.json` when
    # the canonical-named file is absent, so writer↔reader naming
    # drift is recovered automatically. Pre-R78.1's two-candidate walk
    # missed the dominant case where `env_name == environment` (both
    # "staging") but the actual file lived at the alias path
    # is triggered with the resolved name AND the operator's last
    # run since R52 shipped.
    candidate_paths: list[Path] = []
    if env_name:
        candidate_paths.append(Path(f".arta/environments/{env_name}-storage.json"))
    if environment and environment != env_name:
        candidate_paths.append(Path(f".arta/environments/{environment}-storage.json"))
    storage_state_path: Path | None = None
    for p in candidate_paths:
        if p.is_file():
            storage_state_path = p
            break
    # R78.1 — when both canonical-named candidates are missing, fall
    # back to the writer's helper which globs the envs dir + picks the
    # newest matching file. This guarantees writer↔reader agree even
    # when the file landed under an alias name (operator typed
    # resolved name → reader without R78.1 misses it).
    if storage_state_path is None:
        try:
            from .auth_refresher import _find_storage_state_path as _r78_1_find
            # Try resolved env_name first (matches the writer's
            # preference). On miss, try the typed name.
            storage_state_path = _r78_1_find(env_name)
            if storage_state_path is None and environment and environment != env_name:
                storage_state_path = _r78_1_find(environment)
            if storage_state_path is not None:
                expected = f"{env_name}-storage.json"
                if env_name and expected not in storage_state_path.name:
                    log.info(
                        "R78.1: discovery probe sourced storage state from "
                        "alias path %s for env_name=%s (writer/reader naming "
                        "drift recovered via glob fallback)",
                        storage_state_path, env_name,
                    )
        except Exception as _r78_1_exc:
            log.debug("R78.1: glob-fallback skipped: %s", _r78_1_exc)
    has_storage = bool(storage_state_path and storage_state_path.is_file())
    has_cookie = bool(cookie_value)
    # SPA token into localStorage for CLIENT-SIDE auth (the empty-storageState
    # discovery context + Authorization-header-only auth left every route on the
    # login page → login-chrome-only catalog). The FRESH access_token lives in the
    # STORAGE-STATE FILE (relogin updates it there, not in projects.json
    # creds.bearer_token which is usually a stale REPLACE_ME). Extract it so the
    # probe gets a fresh bearer to inject → renders the AUTHENTICATED app.
    if not bearer and storage_state_path and Path(storage_state_path).is_file():
        try:
            _r240_ss = json.loads(Path(storage_state_path).read_text())
            for _r240_o in (_r240_ss.get("origins") or []):
                for _r240_it in (_r240_o.get("localStorage") or []):
                    _r240_v = _r240_it.get("value")
                    if (_r240_it.get("name") in ("access_token", "token", "id_token")
                            and isinstance(_r240_v, str) and _r240_v.count(".") == 2):
                        bearer = _r240_v
                        log.info("R240: sourced fresh SPA bearer from storage-state "
                                 "localStorage[%s] for discovery auth", _r240_it.get("name"))
                        break
                if bearer:
                    break
        except Exception as _r240_exc:
            log.debug("R240: storage-state bearer extraction skipped: %s", _r240_exc)
    has_bearer = bool(bearer)
    if not (has_cookie or has_bearer or has_storage):
        project_id_for_log = (
            str(project.get("id", "?")) if isinstance(project, dict) else "?"
        )
        log.warning(
            "R39.5: refusing to spawn discovery probe for project=%s env=%s — "
            "no usable auth credential (cookie=%s, bearer=%s, storage_state=%s). "
            "Operator must paste a fresh cookie via the Refresh Auth modal "
            "or upload a Playwright storage state file at %s.",
            project_id_for_log, env_name,
            "redacted" if creds.get("cookie_value") in ("***", "REDACTED", "REPLACE_ME") else "missing",
            "redacted" if creds.get("bearer_token") in ("***", "REDACTED", "REPLACE_ME") else "missing",
            "missing",
            storage_state_path or "(env unknown)",
        )
        try:
            har_dir = Path(har_path).parent
            har_dir.mkdir(parents=True, exist_ok=True)
            (har_dir / "auth_failed.flag").write_text(json.dumps({
                "reason": "no_auth_credential",
                "sample": (
                    f"cookie={creds.get('cookie_value')!r}, "
                    f"bearer={creds.get('bearer_token')!r}, "
                    f"storage_state={'present' if has_storage else 'missing'}"
                ),
                "checked_at": datetime.utcnow().isoformat() + "Z",
                "trigger": "discovery_executor_pre_spawn",
            }, indent=2))
        except Exception as flag_exc:
            log.debug("R39.5: pre-spawn auth_failed.flag write failed: %s", flag_exc)
        return {
            "envvar_values": {},
            "endpoints": [],
            "shape_catalog": {},
            "multi_value_warnings": [],
            "_run": {"exit_code": 0, "har_exists": False, "skipped": True},
            "_har_path": str(har_path),
            "_degraded": True,
            "_degraded_reason": "no_auth_credential",
        }

    # R157 KEYSTONE — pre-spawn cookie-expiry gate + discovery-time auto-refresh.
    # Pre-R157: the discovery path NEVER attempted a token refresh (only the
    # execution K1 hook did). When the storage-state cookie was expired, the
    # probe spawned anyway, walked routes that all served the SPA login shell,
    # and R39.1 stamped the misleading `no_json_responses` ("SPA served login
    # the probe ran; operator saw "no authenticated traffic" with no hint that
    # the real fix was a fresh paste.
    #
    # R157 decodes the cookie up front. If expired, it (a) attempts the SAME
    # `refresh_if_expired` hook execution uses — wiring the refresh token into
    # discovery too (operator's standing ask); (b) on refresh success, proceeds
    # to spawn with the freshened storage state; (c) on refresh failure, writes
    # a TRUTHFUL `cookie_expired` flag (exp + age + refresh-token presence +
    # the auto-refresh outcome) and skips the ~60s doomed probe.
    #
    # Killswitch: ARTA_R157_DISCOVERY_REFRESH_DISABLE=1 reverts to pre-R157
    # (spawn regardless of cookie expiry; rely on post-probe R39.1).
    if (
        has_storage
        and os.environ.get("ARTA_R157_DISCOVERY_REFRESH_DISABLE") != "1"
    ):
        cookie_diag = _diagnose_session_cookie(storage_state_path, cookie_name)
        if cookie_diag is not None:
            # SUT-AGNOSTIC auto-refresh: when no auth.refresh is configured,
            # derive one from the SUT's OWN backend source — the refresh
            # route's path-param names match the expired cookie's JWT claim
            # names, so ARTA templates it automatically (no operator wiring).
            try:
                _auth = (env_block or {}).get("auth") or {} if isinstance(env_block, dict) else {}
                if isinstance(env_block, dict) and not _auth.get("refresh") and not _auth_profile_locked(env_block):
                    from .github_context import discover_auth_refresh_config
                    derived = await discover_auth_refresh_config(project, env_block=env_block)
                    if derived:
                        env_block.setdefault("auth", {})["refresh"] = derived
                        log.info(
                            "R157: injected source-derived auth.refresh (%s %s) "
                            "before refresh attempt.",
                            derived.get("method"), derived.get("endpoint"),
                        )
            except Exception as _disc_exc:
                log.debug("R157: auth-refresh source discovery skipped: %s", _disc_exc)
            refresh_outcome = "not_attempted"
            refresh_diags: list[str] = []
            try:
                from .auth_refresher import refresh_if_expired as _r157_refresh
                rr = _r157_refresh(project, environment=env_name)
                refresh_outcome = "succeeded" if getattr(rr, "refreshed", False) else "failed"
                refresh_diags = list(getattr(rr, "diagnostic_lines", []) or [])
                if getattr(rr, "reason", None):
                    refresh_diags.append(str(rr.reason))
            except Exception as _r157_exc:
                refresh_outcome = "errored"
                refresh_diags = [f"refresh_if_expired raised: {_r157_exc}"]
            if refresh_outcome == "succeeded":
                log.info(
                    "R157: storage-state cookie was expired (%ss ago) — "
                    "auto-refresh SUCCEEDED via refresh token; proceeding to spawn "
                    "with freshened credentials.",
                    cookie_diag.get("expired_ago_s"),
                )
                # Fresh cookie value lives in the rewritten storage state; the
                # probe reads it from the file via storage_state_path.
            else:
                log.warning(
                    "R157: storage-state cookie EXPIRED (%ss ago, refresh_token=%s) "
                    "and auto-refresh %s — skipping doomed ~60s probe. "
                    "Operator must paste a fresh cookie via the Refresh Auth modal. "
                    "diagnostics=%s",
                    cookie_diag.get("expired_ago_s"),
                    cookie_diag.get("refresh_token_present"),
                    refresh_outcome,
                    "; ".join(refresh_diags)[:400],
                )
                try:
                    har_dir = Path(har_path).parent
                    har_dir.mkdir(parents=True, exist_ok=True)
                    (har_dir / "auth_failed.flag").write_text(json.dumps({
                        **cookie_diag,
                        "auto_refresh_outcome": refresh_outcome,
                        "auto_refresh_diagnostics": refresh_diags,
                        "har_path": str(har_path),
                        "checked_at": datetime.utcnow().isoformat() + "Z",
                        "trigger": "discovery_executor_pre_spawn_r157",
                    }, indent=2))
                except Exception as flag_exc:
                    log.debug("R157: pre-spawn cookie_expired flag write failed: %s", flag_exc)
                return {
                    "envvar_values": {},
                    "endpoints": [],
                    "shape_catalog": {},
                    "multi_value_warnings": [],
                    "_run": {"exit_code": 0, "har_exists": False, "skipped": True},
                    "_har_path": str(har_path),
                    "_degraded": True,
                    "_degraded_reason": "cookie_expired",
                }

    # R186 — feed the REAL React-Router routes (from source, :params resolved from
    # the SAME storage state the probe loads) to the probe as its PRIMARY navigation
    # targets. Pre-R186 the probe guessed routes from a broken captured-endpoint
    # heuristic (treating `/<account_id>/api` as an SPA route) → React-Router 404 →
    # /login → empty DOM catalog. With the real routes (e.g. /organizations), the
    # probe authenticates and captures a real catalog. The writer resolves :params
    # from `storage_state_path` so the IDs match the session the probe uses.
    # Killswitch: ARTA_R186_FRONTEND_ROUTES_DISABLE=1.
    _r186_routes_csv: str | None = None
    if os.environ.get("ARTA_R186_FRONTEND_ROUTES_DISABLE") != "1" and isinstance(project, dict):
        try:
            from .automation_engineer import resolve_frontend_routes_for_project
            _r186_resolved = await resolve_frontend_routes_for_project(project, env_name)
            # Only FULLY-resolved routes are safe nav targets (a leftover :param
            # 404s → /login). Keep EXTRACT-ORDER (org→workspace→project→services
            # nav hierarchy leads) — the PROVEN config (cap=8) that reliably builds
            # the catalog (28 testids / 82 role+names / 55 endpoints).
            #
            # R191/R192 investigation outcome (kept here so it isn't re-attempted):
            #   A direct 3-route headless probe with the SUT up showed the top-level
            #   feature pages (/ai-apps, /data-sets) DO authenticate + render. BUT a
            #   FULL discovery walk reordered shallow-first (cap=14) repeatedly
            #   produced an EMPTY catalog (0 snapshots/endpoints) — even with the
            #     (a) intermittent SUT egress dropping during the ~4-7min walk, and
            #     (b) the probe-side R180 hydration gate's `hasSignIn` heuristic
            #         (discovery_probe.spec.ts) login_wall-skipping pages that have
            #         a 'Sign in' control anywhere (the R143.G false-positive class,
            #         fixed for the SPEC-side isAuthLoggedIn but NOT the probe gate).
            #   Net: shallow-first did NOT reliably improve coverage and often gave
            #   an EMPTY catalog — strictly worse than the proven nav-first. So we
            #   ship the proven config. Broader feature-page coverage is deferred
            #   until (b) is fixed (URL-only probe gate) + a stable SUT window — NOT
            #   a route reorder. (R174/R175/R191: speculative remaps hurt.)
            _r186_nav = [r["resolved_route"] for r in _r186_resolved if r.get("fully_resolved")]
            if _r186_nav:
                _r186_payload = [
                    {"route": r["route"], "resolved_route": r["resolved_route"],
                     "test_ids": r.get("test_ids") or [], "buttons": r.get("buttons") or [],
                     "form_fields": r.get("form_fields") or []}
                    for r in _r186_resolved if r.get("fully_resolved")
                ]
                _r186_dir = Path(".arta/frontend_routes")
                _r186_dir.mkdir(parents=True, exist_ok=True)
                (_r186_dir / f"{project_id}.json").write_text(json.dumps(_r186_payload, indent=2))
                _r186_routes_csv = ",".join(dict.fromkeys(_r186_nav))  # de-dup, keep order
                log.info("R186: fed %d real frontend route(s) to the discovery probe "
                         "for project=%s (e.g. %s)", len(_r186_nav), project_id,
                         ", ".join(_r186_nav[:4]))
        except Exception as _r186_exc:
            log.debug("R186: frontend-route feed skipped: %s", _r186_exc)

    # 2. Spawn Playwright discovery (with one retry on transient launch failure).
    spawn_kwargs = {
        "base_url": base_url,
        "api_base_url": api_base_url,
        "auth_cookie_name": cookie_name,
        "auth_cookie_value": cookie_value,
        "auth_bearer": bearer,
        # R265 — per-project auth-refresh fulfill config (SUT-agnostic; absent
        # → the probe keeps the strict R154.A abort).
        "auth_refresh_fulfill": (project.get("discovery_settings") or {}).get(
            "auth_refresh_fulfill"),
        # R266 — per-project operator-named read-POST allowlist (exact paths).
        "post_read_allowlist": (project.get("discovery_settings") or {}).get(
            "post_read_allowlist"),
        # R272 — per-project walk skip-list, unioned with the global env var.
        "skip_routes": (project.get("discovery_settings") or {}).get("skip_routes"),
        # the probe). Each SUT declares its own; the probe reads them generically.
        "app_entry_routes": (project.get("discovery_settings") or {}).get("app_entry_routes"),
        "fallback_route_guesses": (project.get("discovery_settings") or {}).get("fallback_route_guesses"),
        # R313.C — per-SUT auth-liveness path for the R37.5 pre-flight (AuthAdapter;
        "auth_liveness_path": (project.get("discovery_settings") or {}).get("auth_liveness_path"),
        "seeded_envvars": seeded_envvars,
        # R39.5 — propagate storage state path so the probe can authenticate
        # via cookies + localStorage from the file even when projects.json
        # holds the redacted placeholder cookie value.
        "storage_state_path": str(storage_state_path) if has_storage else None,
        # R86.0 — propagate project_id so the probe can load
        # `.arta/captured_endpoints/<pid>.json` for real-route discovery.
        "project_id": project_id,
        # R185 — arm the chromium host-resolver bridge (frontend + backend.* +
        # api.* SUT hosts) so the probe authenticates instead of hitting the
        # login wall and emitting an empty DOM catalog.
        "host_resolver_rules": _r185_build_host_resolver_rules(base_url),
        # R186 — the real frontend routes (resolved) as the probe's primary nav.
        "frontend_routes": _r186_routes_csv,
    }
    try:
        _disc = (project.get("discovery_settings") or {}) if isinstance(project, dict) else {}
        if _disc.get("route_cap"):
            spawn_kwargs["route_cap"] = int(_disc["route_cap"])
        if _disc.get("bfs_depth"):
            spawn_kwargs["bfs_depth"] = int(_disc["bfs_depth"])
        if _disc.get("hardcoded_probes_disable"):
            spawn_kwargs["hardcoded_probes_disable"] = True
        # vocabulary out of the platform probe into discovery_settings).
        if _disc.get("replay_keyword_filter"):
            spawn_kwargs["replay_keyword_filter"] = _disc["replay_keyword_filter"]
    except Exception:
        pass
    run_summary = await _spawn_playwright_discovery(har_path, spec_dir, **spawn_kwargs)
    if run_summary["exit_code"] != 0 and not run_summary["har_exists"]:
        log.warning("discovery_executor: launch failed, retrying once. stderr=%r", run_summary.get("stderr"))
        run_summary = await _spawn_playwright_discovery(har_path, spec_dir, **spawn_kwargs)

    if not run_summary["har_exists"]:
        log.warning("discovery_executor: HAR not produced; harvest will be empty")
        return {
            "envvar_values": {},
            "endpoints": [],
            "shape_catalog": {},
            "multi_value_warnings": [],
            "_run": run_summary,
            "_har_path": str(har_path),
            "_degraded": True,
            "_degraded_reason": "har_not_produced",
        }

    # 3. Parse + redact (I1) + cap (I2).
    try:
        from . import sut_onboarding
    except ImportError as exc:
        log.error("discovery_executor: sut_onboarding import failed: %s", exc)
        return {
            "envvar_values": {}, "endpoints": [], "shape_catalog": {},
            "multi_value_warnings": [], "_run": run_summary,
            "_degraded": True, "_degraded_reason": f"ingest_import_error:{exc}",
        }

    project_settings = project.get("discovery_settings") if isinstance(project, dict) else None
    records = sut_onboarding._ingest_har(har_path, project_settings=project_settings)

    # R39.1 KEYSTONE — synthesize auth_failed.flag when the HAR shows no
    # authenticated traffic. R37.5 only catches explicit `/api/v1/users/me`
    # 401 responses; real-world SPAs serve the login page on every route
    # with HTTP 200 + HTML, so the probe completes "successfully" with an
    # empty harvest and the operator never sees the actual problem
    # (cookie/storage state missing). Detecting the gap at the records
    # layer surfaces a single deterministic signal regardless of the
    # specific failure shape (login page, 404 HTML, all-failed JSON).
    auth_diagnosis = _detect_auth_failed_from_records(records)
    if auth_diagnosis is not None:
        try:
            har_dir = Path(har_path).parent
            har_dir.mkdir(parents=True, exist_ok=True)
            (har_dir / "auth_failed.flag").write_text(json.dumps({
                **auth_diagnosis,
                "har_path": str(har_path),
                "checked_at": datetime.utcnow().isoformat() + "Z",
                "trigger": "discovery_executor_post_har",
            }, indent=2))
        except Exception as flag_exc:
            log.debug("R39.1: auth_failed.flag write failed: %s", flag_exc)
        log.warning(
            "R39.1: discovery probe captured no authenticated traffic for "
            "project=%s reason=%s — operator must paste a fresh auth "
            "cookie via the Refresh Auth modal. Harvest will be empty.",
            project.get("id", "?") if isinstance(project, dict) else "?",
            auth_diagnosis.get("reason"),
        )
        return {
            "envvar_values": {},
            "endpoints": [],
            "shape_catalog": {},
            "multi_value_warnings": [],
            "_run": run_summary,
            "_har_path": str(har_path),
            "_degraded": True,
            "_degraded_reason": f"auth_failed:{auth_diagnosis['reason']}",
            "_auth_diagnosis": auth_diagnosis,
        }

    # 4. Harvest. Pull `known_envvar_names` from the project's existing env
    # vars so we never invent names — only fill ones operators declared.
    try:
        from . import api_discovery
    except ImportError as exc:
        log.error("discovery_executor: api_discovery import failed: %s", exc)
        return {
            "envvar_values": {}, "endpoints": [], "shape_catalog": {},
            "multi_value_warnings": [], "_run": run_summary,
            "_degraded": True, "_degraded_reason": f"discovery_import_error:{exc}",
        }

    # project_id already defined early (line ~311 per R86.0) so it can
    # be passed to the Playwright discovery probe spawn_kwargs.
    known_names = _resolve_known_envvar_names(project)

    harvest = api_discovery.harvest_envvars_from_har(
        records, project_id,
        known_envvar_names=known_names,
        source_har=str(har_path),
    )

    # 5. Persist captured endpoints (Phase B4) AND extract+persist chains (Phase C).
    # First clean up any sentinel pollution from prior runs so harvested
    # entries don't get diluted by `__ARTA_UNSET_*` artifacts.
    try:
        purged = api_discovery.purge_polluted_endpoints(project_id)
        if purged:
            log.info("discovery_executor: purged %d polluted endpoints before harvest", purged)
    except Exception as exc:
        log.debug("discovery_executor: purge skipped: %s", exc)
    try:
        api_discovery.save_captured_endpoints(project_id, harvest.get("endpoints") or [])
    except Exception as exc:   # persistence is best-effort
        log.warning("discovery_executor: save_captured_endpoints failed: %s", exc)

    # E-OpenAPI — the PRIMARY endpoint-grounding lever. Fetch the SUT's OWN
    # published OpenAPI/Swagger contract and persist it as authoritative
    # grounding. The probe walks only a shell subset (~51 endpoints), so real
    # routes (GetLeaseManagerReport, whole Lease/Account families) are BLOCKED.
    # The contract carries the correct deployment prefixes (verified: the
    # GET; zero fabrication (it's the SUT's declaration); no-op when the SUT
    if os.environ.get("ARTA_OPENAPI_FETCH_DISABLE") != "1" and base_url:
        try:
            # Auth: bearer preferred; else the run's cookie from storage-state.
            _oa_headers: dict = {}
            if bearer:
                _oa_headers["Authorization"] = f"Bearer {bearer}"
            elif has_storage:
                try:
                    _ss = json.loads(Path(storage_state_path).read_text())
                    _cookie = "; ".join(
                        f"{c.get('name')}={c.get('value')}"
                        for c in (_ss.get("cookies") or [])
                        if c.get("name") and c.get("value"))
                    if _cookie:
                        _oa_headers["Cookie"] = _cookie
                except Exception:
                    pass
            # EO-1 — service names to probe per-service docs, from the UNION of:
            #  (a) first path segment of already-captured endpoints (observed +
            #      github-route prefixes — the services the SUT was seen serving),
            #  (b) discovery_settings.openapi_services config — the full service
            #      inventory (from the OTP architecture walkthrough), so a service
            #      the probe NEVER touched (e.g. ResourceManagement, 43 paths) is
            #      still probed. SUT-agnostic: each SUT names its own services.
            _svc_names: list[str] = []
            for _e in (api_discovery._load_captured_endpoints(project_id) or []):
                if not isinstance(_e, dict):
                    continue
                _p = _e.get("path") or _e.get("url") or ""
                _segs = [s for s in str(_p).split("/") if s]
                if _segs and _segs[0].lower() not in ("api", "v1", "v2", "v3"):
                    if _segs[0] not in _svc_names:
                        _svc_names.append(_segs[0])
            try:
                _cfg_svcs = (project.get("discovery_settings") or {}).get("openapi_services") or []
                for _cs in _cfg_svcs:
                    _cs = str(_cs).strip("/")
                    if _cs and _cs not in _svc_names:
                        _svc_names.append(_cs)
            except Exception:
                pass
            _oa_eps = await api_discovery.fetch_openapi_contracts(
                project_id, base_url, _oa_headers, service_names=_svc_names[:40])
            if _oa_eps:
                api_discovery.save_captured_endpoints(project_id, _oa_eps)
                api_discovery.persist_openapi_doc(project_id, _oa_eps)
                log.info("E-OpenAPI: grounded %d endpoint(s) from the SUT's own "
                         "published contract for %s", len(_oa_eps), project_id)
        except Exception as _oa_exc:
            log.debug("E-OpenAPI: skipped for %s: %s", project_id, _oa_exc)

    # R250 — harvest REAL entity ids from the HAR's response bodies.
    #
    # `records` already carry `response_body_sample` (sut_onboarding:321), but
    # pre-R250 only `_infer_shape` (types) was ever persisted and the VALUES
    # were dropped on the floor. That is why test-data grounding never existed:
    # ARTA ground endpoints/selectors/auth against the real SUT and then let
    # the LLM invent the ids. Same `records`, same pass — keep the values too.
    # Credential-shaped fields are excluded inside the store (fail-closed).
    try:
        from . import real_id_store as _ris
        _r250_ids = _ris.extract_real_ids(records)
        if _r250_ids:
            _ris.persist_real_ids(project_id, _r250_ids)
            log.info(
                "R250: harvested %d real-id slot(s) from %d HAR records for %s "
                "— entities: %s",
                len(_r250_ids), len(records), project_id,
                ", ".join(sorted({s.get("entity", "") for s in _r250_ids.values()} - {""})) or "none",
            )
        else:
            log.info("R250: no real ids found in %d HAR records for %s "
                     "(gen will emit {{var}} and dispatch will resolve-or-BLOCK)",
                     len(records), project_id)
    except Exception as exc:
        log.debug("R250: real-id harvest skipped: %s", exc)

    # R313.D — SSR-compatible value-domain source. The discovery PROBE captures
    # response VALUES from client-side XHRs, but server-rendered SUTs (SSR, e.g.
    # fabricated-enum assertions can't be validated. This complementary source does
    # a BOUNDED, strictly read-only (GET-only, R154-safe) sweep of the SUT's OWN
    # concrete captured GET endpoints, extracting enum-like field values onto
    # discovered_endpoints. Generic (no SUT literal); killswitch inside the probe.
    try:
        _r313d_eps = api_discovery._load_captured_endpoints(project_id)
        _r313d_base = api_base_url or base_url
        if _r313d_eps and _r313d_base:
            _enriched = await api_discovery.probe_response_value_samples(
                project_id, _r313d_eps,
                base_url=_r313d_base,
                auth_state_path=str(storage_state_path) if storage_state_path else None,
                bearer_token=bearer,
            )
            if _enriched:
                log.info("R313.D: enriched %d endpoint(s) with enum-like value "
                         "domains for %s (SSR-compatible value source)",
                         _enriched, project_id)
    except Exception as exc:
        log.debug("R313.D: value-domain probe skipped for %s: %s", project_id, exc)

    # Proactive real-id seeding from discovered LIST endpoints (SUT-agnostic).
    # Complements the R250 HAR harvest above: the HAR only carries what the crawl
    # itself fetched, so an entity whose list the SPA never auto-loaded (e.g.
    # then fabricates its id and the placeholder-driven R230 probe never fires
    # for it. This structurally GETs every discovered collection endpoint (GET-
    # only, R154-safe) so real ids land BEFORE gen and R251/R336 surface them.
    # Killswitch ARTA_LIST_ID_SEED_DISABLE=1.
    try:
        _seed_eps = api_discovery._load_captured_endpoints(project_id)
        _seed_base = api_base_url or base_url
        if _seed_eps and _seed_base:
            _seeded = await api_discovery.seed_real_ids_from_list_endpoints(
                project_id, _seed_eps,
                base_url=_seed_base,
                auth_state_path=str(storage_state_path) if storage_state_path else None,
                bearer_token=bearer,
            )
            if _seeded:
                log.info("list_seed: seeded %d real-id slot(s) for %s from "
                         "discovered list endpoints", _seeded, project_id)
    except Exception as exc:
        log.debug("list_seed: real-id list-endpoint seed skipped for %s: %s", project_id, exc)

    # R213.J Part 2 — AUTO-DERIVE the per-family auth chain + host_map from the
    # OBSERVED HAR when the env_block has none, so a NEW SUT needs ZERO
    # hand-written chain. Order-independent downstream (best_rule specificity).
    # Only fills when absent (operator/existing chain always wins). Best-effort;
    # killswitch ARTA_AUTH_CHAIN_AUTODERIVE_DISABLE=1.
    if (os.environ.get("ARTA_AUTH_CHAIN_AUTODERIVE_DISABLE") != "1"
            and isinstance(env_block, dict)
            and not _auth_profile_locked(env_block)):   # M4 — locked profile is authoritative
        try:
            _existing = (env_block.get("auth") or {}).get("chain")
            # A2 (R218) — SOURCE-derive the chain FIRST. The SUT's frontend
            # api-client is its DECLARED auth intent, so it outranks observed HAR.
            # Only-when-absent; SUT-agnostic; killswitch inside discover_*.
            if not _existing:
                try:
                    from .github_context import discover_auth_chain_from_source as _a2_disc
                    _a2 = await _a2_disc(project, env_block=env_block)
                except Exception as _a2e:
                    log.debug("A2: source auth-chain discovery skipped: %s", _a2e)
                    _a2 = None
                if _a2 and _a2.get("chain"):
                    _auth = env_block.setdefault("auth", {})
                    _auth["chain"] = _a2["chain"]
                    _auth.setdefault("host_map", {}).update(_a2.get("host_map") or {})
                    if _a2.get("body_manifest"):
                        _auth["body_manifest"] = _a2["body_manifest"]
                    # A7.5 — persist the SOURCE-discovered token-mint route so the
                    # runtime mint (api_discovery._a7_mint_bound_agent_token) is
                    if _a2.get("mint_manifest"):
                        _auth["mint"] = _a2["mint_manifest"]
                    # route, OVERRIDING any backend source-grep (which finds the wrong
                    # Google-token refresh). Frontend RefreshTokenApi is what the SPA
                    # corrected endpoint (_source_corrected) so a manual fix survives.
                    _rm = _a2.get("refresh_manifest") or {}
                    if _rm.get("endpoint_template") and not (
                            (_auth.get("refresh") or {}).get("_source_corrected")):
                        _rf = dict(_auth.get("refresh") or {})
                        # Normalize {api_base}→{api_base_url} (the refresh subst key).
                        _ep = _rm["endpoint_template"].replace("{api_base_url}", "{api_base}") \
                                                      .replace("{api_base}", "{api_base_url}")
                        # If the query is inline (?refresh_token=…) the subst fills it in
                        # place — don't ALSO add a separate query dict (would duplicate).
                        _ep_path, _sep, _ep_q = _ep.partition("?")
                        _rf["endpoint"] = _ep_path
                        _rf["method"] = (_rm.get("method") or "GET").upper()
                        _tp = _rm.get("token_path") or "token"
                        _rf["access_token_paths"] = [
                            f"$.{_tp}" if not str(_tp).startswith("$") else str(_tp),
                            "$.token", "$.session_token", "$.access_token", "$.id_token"]
                        if _ep_q:  # preserve the inline query as a resolvable query dict
                            from urllib.parse import parse_qsl
                            _rf["query"] = dict(parse_qsl(_ep_q)) or {"refresh_token": "{refresh_token}"}
                        else:
                            _rf.setdefault("query", {"refresh_token": "{refresh_token}"})
                        _rf["_derived_from"] = "A2 frontend refresh_manifest (RefreshTokenApi)"
                        _auth["refresh"] = _rf
                    try:
                        from ..api.routers.projects import _save_projects as _sp_save
                        _sp_save()
                    except Exception as _spx:
                        log.debug("A2: _save_projects after source-derive failed (in-run only): %s", _spx)
                    log.info("A2: installed SOURCE-derived auth.chain (%d rule(s)) + "
                             "host_map (%d) for %s/%s — learned from the SUT's frontend "
                             "api-client, not hand-coded",
                             len(_a2["chain"]), len(_a2.get("host_map") or {}),
                             project_id, env_name)
                    _existing = _a2["chain"]  # A2 filled it → skip the HAR-derive below
            if not _existing:
                from .auth_chain import (
                    derive_auth_chain_from_har as _derive_chain,
                    harvest_session_ids_from_storage as _harvest_sids,
                )
                _known: dict = {}
                _known.update(harvest.get("envvar_values") or {})
                for _k, _v in (seeded_envvars or {}).items():
                    _known.setdefault(_k, _v)
                try:
                    _sp = _find_storage_state(spec_dir)
                    if _sp and _sp.is_file():
                        _ss = json.loads(Path(_sp).read_text(encoding="utf-8"))
                        for _k, _v in (_harvest_sids(_ss) or {}).items():
                            if isinstance(_v, str) and _v:
                                _known.setdefault(_k, _v)
                except Exception:
                    pass
                _derived = _derive_chain(records, _known)
                if _derived.get("chain"):
                    _auth = env_block.setdefault("auth", {})
                    _auth["chain"] = _derived["chain"]
                    _auth.setdefault("host_map", {}).update(_derived.get("host_map") or {})
                    try:
                        from ..api.routers.projects import _save_projects as _sp_save
                        _sp_save()
                    except Exception as _spx:
                        log.debug("R213.J: _save_projects after autoderive failed (in-run only): %s", _spx)
                    log.info(
                        "R213.J: auto-derived auth.chain (%d rule(s)) + host_map (%d host(s)) "
                        "from observed HAR for %s/%s — no hand-written chain needed",
                        len(_derived["chain"]), len(_derived.get("host_map") or {}),
                        project_id, env_name,
                    )
        except Exception as _ade:
            log.debug("R213.J: auth-chain autoderive skipped: %s", _ade)

    # R244 (R218 sibling) — SOURCE-derive the SUT's LOGIN FLOW(s) from its frontend
    # authentication module (SUT-agnostic), so auth-flow test gen uses the REAL login
    # request shape (endpoint + body fields) instead of guessing. Persists to
    # env_block.auth.login_flows for R243 (gen injection). Only-when-absent (operator
    # login_flows win); best-effort; killswitch inside discover_* + here.
    if (os.environ.get("ARTA_R244_LOGIN_DISCOVER_DISABLE") != "1"
            and isinstance(env_block, dict)
            and not _auth_profile_locked(env_block)):   # M4 — locked profile is authoritative
        try:
            _auth_lf = env_block.setdefault("auth", {})
            if not _auth_lf.get("login_flows"):
                from .github_context import discover_login_flow_from_source as _lf_disc
                _lf = await _lf_disc(project)
                if _lf and _lf.get("login_flows"):
                    _auth_lf["login_flows"] = _lf["login_flows"]
                    try:
                        from ..api.routers.projects import _save_projects as _sp_save
                        _sp_save()
                    except Exception as _spx:
                        log.debug("R244: _save_projects after login-flow discovery "
                                  "failed (in-run only): %s", _spx)
                    log.info("R244: installed SOURCE-derived auth.login_flows (%d flow(s)) "
                             "for %s/%s — learned from the SUT's frontend auth module, "
                             "not hand-coded", len(_lf["login_flows"]), project_id, env_name)
        except Exception as _lfe:
            log.debug("R244: login-flow discovery skipped: %s", _lfe)

    # Fix 2 (P8) — SOURCE-derive backend ROUTES (Java Spring / .NET / Angular / Express)
    # from the SUT's repos and persist into captured_endpoints (source="github",
    # authoritative per R206 keep-rule). Closes the hallucinated-path 404 gap: no live
    # extractor parsed the SUT's Java/Angular stack, so its entire backend surface was
    # invisible (the 162 github seed was a one-off, not a pipeline step). The Gherkin
    # relevance filter (R98.3 / R77.1.γ) caps per-req injection so the fuller surface
    # doesn't bloat prompts. Killswitch ARTA_GH_ROUTE_EXTRACT_DISABLE.
    if os.environ.get("ARTA_GH_ROUTE_EXTRACT_DISABLE") != "1":
        try:
            from .github_context import extract_backend_routes_from_source as _gh_routes
            _routes = await _gh_routes(project)
            if _routes:
                # R261 (WS4) — corroborate against OBSERVED traffic before
                # persisting. Fix 2 shipped without this and its Java-Spring
                # routes (missing the per-service deployment prefix, e.g.
                # became "authoritative" grounding -> ~190 Newman 404s ->
                # Newman 58.9% -> 44.0%.
                _routes = api_discovery._r261_validate_extracted_routes(
                    _routes, api_discovery._load_captured_endpoints(project_id) or [],
                    project_id=project_id,
                )
            if _routes:
                api_discovery.save_captured_endpoints(project_id, _routes)
                log.info("Fix 2 (P8) + R261: persisted %d VALIDATED source-derived backend "
                         "route(s) into captured_endpoints (source=github) for %s — learned "
                         "from the SUT's Java/Angular source and corroborated by observed "
                         "traffic", len(_routes), project_id)
        except Exception as _ghre:
            log.debug("Fix 2 (P8): backend route extraction skipped: %s", _ghre)

    # R297 — SOURCE-derive REQUEST BODIES for bodyless POST/PUT/PATCH endpoints whose
    # DTO the AC names (e.g. `(CommandCenterRequest)`) but for which no OpenAPI
    # requestBody / captured body / probe body exists (the .NET/Reefer blocked
    # families). Resolves the DTO type from the requirement prose, finds+parses the
    # class from SUT source via the agent-owned GitHub MCP (R104.B), and persists a
    # synthesized example body to `.arta/dto_request_bodies/<pid>.json` — build_request_bodies
    # reads it as source (4). Runs here (post endpoint-capture) so the bodyless-POST set
    # + requirement→endpoint map are both available. SUT-agnostic; killswitch inside.
    if os.environ.get("ARTA_R297_DTO_EXTRACT_DISABLE") != "1":
        try:
            from .dto_extractor import refresh_dto_request_bodies as _r297_dto
            _dto_bodies = await _r297_dto(project)
            if _dto_bodies:
                log.info("R297: source-derived %d request body/-ies for bodyless POST "
                         "endpoint(s) from SUT DTO classes for %s — the AC named the DTO, "
                         "the class was parsed from source (build_request_bodies source 4)",
                         len(_dto_bodies), project_id)
        except Exception as _r297e:
            log.debug("R297: DTO request-body extraction skipped: %s", _r297e)

    # G1 (R218) — SOURCE-derive the full analytics WORKFLOW MANIFEST (dataset MODES +
    # engine routing + endpoint templates + the ingestion job-create TRIGGER) and install
    # it into env_block.analytics, so the runtime ingestion is driven by DATA discovered
    # from THIS SUT's code (not a hardcoded default). Operator directive: "ARTA should go
    # through the SUT code and understand — generic." SUT-agnostic; killswitch inside disc.
    if os.environ.get("ARTA_AN_WORKFLOW_DISCOVER_DISABLE") != "1":
        try:
            from .github_context import discover_analytics_workflow_from_source as _g1_disc
            _wf = await _g1_disc(project)
            if _wf and _wf.get("dataset_modes"):
                env_block["analytics"] = _wf
                try:
                    from ..api.routers.projects import _save_projects as _sp_save
                    _sp_save()
                except Exception as _spx:
                    log.debug("G1: _save_projects after workflow-derive failed (in-run only): %s", _spx)
                log.info("G1: installed SOURCE-derived analytics workflow manifest "
                         "(%d mode(s), %d endpoint(s)) for %s/%s — learned from the SUT's "
                         "code, not hand-coded", len(_wf.get("dataset_modes", {})),
                         len(_wf.get("endpoints", {})), project_id, env_name)
        except Exception as _g1e:
            log.debug("G1: analytics workflow discovery skipped: %s", _g1e)

    # R160.F — augment the grounding surface with OpenAPI-discovered endpoints
    # for families whose paths runtime-capture can't reach (analytics/extraction
    # fire only on interactive query exec). These services publish their full
    # contract at /openapi.json — the authoritative real paths + required query
    # params. Live-proven: analytics 47 + extraction 66 paths, agent token →
    # past-auth. Killswitch ARTA_R160_OPENAPI_DISCOVERY=0.
    if os.environ.get("ARTA_R160_OPENAPI_DISCOVERY") != "0":
        try:
            from .sut_topology import fetch_openapi_endpoints
            _auth_cfg = (env_block.get("auth") or {}) if isinstance(env_block, dict) else {}
            _host_map = _auth_cfg.get("host_map") or {}
            _oa_eps: list[dict] = []
            for _fam in ("analytics", "extraction", "monitoring"):
                _host = _host_map.get(_fam)
                if not _host:
                    continue
                for _e in (await fetch_openapi_endpoints(_host)):
                    _oa_eps.append({
                        "method": _e["method"], "path": _e["path"], "source": "openapi",
                        "query_params": [{"name": q["name"], "value": "", "type": q.get("type")}
                                         for q in _e.get("query_params", []) if q.get("required")],
                    })
            if _oa_eps:
                api_discovery.save_captured_endpoints(project_id, _oa_eps)
                log.info("R160.F: added %d OpenAPI endpoints (analytics/extraction) "
                         "to grounding surface for %s", len(_oa_eps), project_id)
        except Exception as exc:
            log.debug("R160.F: OpenAPI discovery skipped: %s", exc)

    # R160.E — emit a GROUNDED contract collection from the freshly-discovered
    # real surface (real paths + required query params), ADDITIVE alongside the
    # LLM collections. Dispatch (R159) supplies per-family auth/host. Read-only
    # (GET) → R154-safe. Killswitch: ARTA_R160_GROUNDED_GEN=0.
    if os.environ.get("ARTA_R160_GROUNDED_GEN") != "0":
        try:
            from . import endpoint_grounding as _eg
            _eps = api_discovery._load_captured_endpoints(project_id)
            _known = _eg.collect_known_id_values(project, env_block)
            _prefix = ""
            try:
                from ..api.routers.tests import _get_project_req_ids
                _rids = _get_project_req_ids(project_id)
                if _rids:
                    _parts = next(iter(_rids)).split("-")
                    if len(_parts) >= 2:
                        _prefix = "_".join(_parts[:2]).lower() + "_"
            except Exception:
                pass
            _gp = _eg.write_grounded_collection(
                "src/automation/newman", _prefix or "grounded_", _eps, _known)
            if _gp:
                log.info("R160.E: grounded contract collection written: %s", _gp)
        except Exception as exc:
            log.debug("R160.E: grounded collection emit skipped: %s", exc)

    # R19b — ingest DOM snapshots written by discovery_probe.spec.ts as
    # `dom*.json` sidecars beside the HAR. The catalog is what
    # `atdd_designer` reads (R19c) to constrain the LLM to real testids
    # and what `_validate_pw_selectors_grounded` (R19d) checks against.
    try:
        dom_catalog = api_discovery.ingest_dom_snapshots(project_id, har_path)
        harvest["dom_catalog"] = {
            "testid_count": dom_catalog.get("testid_count", 0),
            "route_count": len(dom_catalog.get("routes") or {}),
        }
    except Exception as exc:
        log.warning("discovery_executor: ingest_dom_snapshots failed: %s", exc)
        harvest["dom_catalog"] = {"testid_count": 0, "route_count": 0}

    # Phase C extraction + persistence. Run independently from the env-var
    # harvest so chain failures don't block harvest persistence (and vice
    # versa — chain extraction needs the full record set, not the harvest).
    try:
        from . import call_chain
        # B1 — build a {concrete_id_value: var_name} map from the project's
        # known env-var ids (organization_id=424e744f…, account_id, subscriber_id,
        # subscription_id, …) + the fresh harvest, so the path templater names
        # each org-scoped segment with the CORRECT var instead of the preceding
        # path word. Fixes the wrong-id-in-path 500s.
        _known_ids: dict[str, str] = {}
        try:
            import re as _re_b1
            _uuidish = _re_b1.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{5,}$")
            _env_vars: dict = {}
            for _eb in (project.get("environments") or {}).values():
                _env_vars.update((_eb or {}).get("variables") or {})
            _env_vars.update(harvest.get("envvar_values") or {})
            for _vn, _vv in _env_vars.items():
                _vs = str(_vv or "")
                if (str(_vn).endswith("_id") and _vs
                        and _vs not in ("REPLACE_ME", "***")
                        and _uuidish.match(_vs)):
                    _known_ids.setdefault(_vs, str(_vn))
        except Exception:
            _known_ids = {}
        chains = call_chain.extract_chains_from_har(
            records,
            project_id=project_id,
            source_test_id=getattr(ctx, "_current_test_id", None),
            source_har=str(har_path),
            known_ids=_known_ids or None,
        )
        if chains:
            api_discovery.save_chains(project_id, chains)
            harvest["chains"] = [c.to_dict() for c in chains]
        else:
            harvest["chains"] = []
    except Exception as exc:
        log.warning("discovery_executor: chain extraction failed: %s", exc)
        harvest["chains"] = []

    # Phase D1 + D3 — emit chain-aware Newman + k6 scripts alongside the
    # LLM-generated outputs. These deterministic scripts are guaranteed to
    # consume harvested env vars correctly, so even if Phase 4 LLM
    # generation regresses, the chain-aware artifacts always work.
    try:
        if harvest.get("chains"):
            await _emit_chain_aware_artifacts(
                ctx, project_id,
                chains_json=harvest["chains"],
                project_vars=harvest.get("envvar_values") or {},
            )
    except Exception as exc:
        log.warning("discovery_executor: chain-aware artifact emission failed: %s", exc)

    # Phase AD — Architecture Discovery aggregation (deterministic, zero-LLM).
    # Consolidates the captured endpoints + chains + source routes + token
    # chains into 6 first-class graph artifacts under
    # .arta/architecture_discovery/<pid>/ (+ optional Neo4j). Reads only
    # existing loaders — introduces no new discovery. Killswitch:
    # ARTA_ARCH_DISCOVERY_DISABLE=1.
    if os.environ.get("ARTA_ARCH_DISCOVERY_DISABLE", "").lower() not in ("1", "true", "yes"):
        try:
            from . import architecture_discovery as _ad_phase
            # R215 — resolve the Neo4j driver: prefer an explicit ctx.neo4j_driver,
            # else fall back to the process-wide registry (set at app startup) so
            # this works even when invoked from a background task / ad-hoc ctx that
            # has no driver field. SUT-agnostic; no-ops gracefully when Neo4j is off.
            _ad_driver = getattr(ctx, "neo4j_driver", None)
            if _ad_driver is None:
                try:
                    from ..graph.writer import get_driver as _get_graph_driver
                    _ad_driver = _get_graph_driver()
                except Exception:
                    _ad_driver = None
            ad_summary = await _ad_phase.run(
                project=project, project_id=project_id,
                neo4j_driver=_ad_driver,
                gherkin=getattr(ctx, "gherkin_scenarios", None),
            )
            harvest["architecture_discovery"] = ad_summary
        except Exception as exc:
            log.warning("discovery_executor: architecture_discovery phase failed: %s", exc)

    # 6. Auto-populate env vars (only those that are currently empty).
    try:
        await _bulk_set_envvars(project_id, harvest.get("envvar_values") or {}, har_path=har_path, env_name=env_name)
    except Exception as exc:
        log.warning("discovery_executor: env-var bulk-set failed: %s", exc)

    # 6b. Phase J post-review: write a latest_harvest sidecar so Newman's
    # runtime fallback (execution.py:1915) has a place to recover values
    # from when bulk-set didn't reach the project env table for any reason.
    try:
        sidecar_dir = Path(".arta/discovery")
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        sidecar_path = sidecar_dir / "latest_harvest.json"
        sidecar_payload = {
            "project_id": project_id,
            "envvar_values": harvest.get("envvar_values") or {},
            "harvested_at": datetime.now(timezone.utc).isoformat(),
            "har_path": str(har_path),
        }
        sidecar_path.write_text(json.dumps(sidecar_payload, indent=2))
    except Exception as exc:
        log.debug("discovery_executor: latest_harvest sidecar write failed: %s", exc)

    # 7. Bookkeeping on discovery_settings.
    try:
        await _stamp_last_discovery(project_id, run_id=workflow_id,
                                    envvars_harvested=len(harvest.get("envvar_values") or {}))
    except Exception as exc:
        log.warning("discovery_executor: stamp_last_discovery failed: %s", exc)

    # R67.B — refresh the OpenAPI cache opportunistically. When the SUT
    # serves a swagger/OpenAPI endpoint, populate `.arta/openapi/<pid>.json`
    # so the next Newman gen cycle has the signal R18b/c + R67.A need.
    # Without this, the cache stays empty on cold-start projects → R67.A
    # refuses every Newman gen → operator stuck in a refusal loop.
    # Best-effort: failure to fetch doesn't degrade discovery.
    if api_base_url and project_id:
        try:
            from .openapi_cache import fetch_openapi as _r67_b_fetch_oa
            _r67_b_spec = await _r67_b_fetch_oa(api_base_url, project_id)
            if _r67_b_spec is not None:
                log.info(
                    "R67.B: OpenAPI cache populated for project=%s base=%s "
                    "(%d paths) — R18b/c + R67.A can now validate Newman gen",
                    project_id, api_base_url,
                    len(_r67_b_spec.get("paths") or {}),
                )
                # Authz-model ingestion (route-catalog half of the RBAC oracle):
                # derive scope/visibility/auth-gated/success per operation from
                # the REAL fetched spec (persist_openapi_doc's merged doc carries
                # stubs only — no responses/x-visibility). Fail-open. Feeds RBAC
                # gen so it asserts against the real authz contract instead of
                # LLM-guessing 'operator sees all'.
                try:
                    from .authz_discovery import build_authz_model
                    build_authz_model(project_id, openapi_doc=_r67_b_spec)
                except Exception as _az_exc:
                    log.debug("authz_discovery: build skipped for %s: %s",
                              project_id, _az_exc)
            else:
                log.info(
                    "R67.B: OpenAPI cache fetch returned None for project=%s "
                    "base=%s — SUT may not expose swagger/openapi JSON. "
                    "Newman gen will refuse via R67.A until cache populated.",
                    project_id, api_base_url,
                )
        except Exception as _r67_b_exc:
            log.debug(
                "R67.B: OpenAPI cache refresh skipped for project=%s: %s",
                project_id, _r67_b_exc,
            )

    harvest["_run"] = run_summary
    harvest["_har_path"] = str(har_path)
    harvest["_records_count"] = len(records)
    return harvest


def _resolve_known_envvar_names(project: dict) -> set[str]:
    """Pull the project's currently-declared env-var names from EVERY
    place names live in the project record:

      1. `environment_variables` (legacy flat dict/list, some projects)
      2. `env_vars` (alternative legacy key)
      3. `environments.<env>.variables` (CURRENT ARTA shape — all
         current projects use this; pre-fix the resolver returned
         an empty set for them so harvest never wrote anything)

    Discovery NEVER invents names — if a project has no env-var named
    `dataset_id`, we won't create one even if the captured HAR shows
    a value for it. But it MUST find every name the project actually
    declared, including REPLACE_ME placeholders the operator wants the
    harvester to fill.
    """
    if not isinstance(project, dict):
        return set()
    names: set[str] = set()

    # Legacy flat shapes
    flat = project.get("environment_variables") or project.get("env_vars") or []
    if isinstance(flat, dict):
        names.update(str(k) for k in flat.keys())
    elif isinstance(flat, list):
        for item in flat:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))

    # Current ARTA shape: project.environments.<env>.variables (per-env dicts)
    environments = project.get("environments")
    if isinstance(environments, dict):
        for env_name, env_block in environments.items():
            if not isinstance(env_block, dict):
                continue
            variables = env_block.get("variables")
            if isinstance(variables, dict):
                names.update(str(k) for k in variables.keys())
            elif isinstance(variables, list):
                for item in variables:
                    if isinstance(item, dict) and item.get("name"):
                        names.add(str(item["name"]))

    return names


async def _bulk_set_envvars(
    project_id: str, values: dict[str, str], *, har_path: Path,
    env_name: str | None = None,
) -> None:
    """Write harvested env-var values into the project env_block, overwriting
    REPLACE_ME / unset placeholders (never real operator-set values).

    R213.G — the persistence target `bulk_add_environment_variables`
    (projects.py) takes `(project_id, env_name, body: BulkAddVariablesBody)`
    and overwrites placeholder values from `body.values`. The pre-R213.G call
    here passed `(project_id=…, body=<dict>)` (missing `env_name`, body a dict
    not the model) → `TypeError: missing 1 required positional argument` on
    BOTH the keyword and positional fallbacks → the harvest→env_block sync
    SILENTLY FAILED for every discovery run. Net effect: discovery harvested
    real ids but the env_block stayed `REPLACE_ME`, so every Newman/k6 item
    referencing those path params BLOCKED at dispatch (unresolved_path_param).
    This restores the canonical signature + a real BulkAddVariablesBody.
    Killswitch ARTA_R213_G_ENVVAR_SYNC_DISABLE=1.
    """
    if not values:
        return
    if os.environ.get("ARTA_R213_G_ENVVAR_SYNC_DISABLE") == "1":
        return
    try:
        from ..api.routers import projects as projects_router
        from ..api.routers.projects import BulkAddVariablesBody
    except ImportError as exc:
        log.warning("discovery_executor: projects router import failed: %s", exc)
        return
    fn = getattr(projects_router, "bulk_add_environment_variables", None)
    if fn is None:
        log.warning("discovery_executor: bulk_add_environment_variables not exported")
        return
    _env = env_name or "staging"
    body = BulkAddVariablesBody(values=values, env_name=_env)
    try:
        await fn(project_id, _env, body)   # canonical (project_id, env_name, body)
        log.info(
            "discovery_executor: R213.G synced %d harvested env-var(s) to %s/%s",
            len(values), project_id, _env,
        )
    except Exception as exc:
        log.warning("discovery_executor: R213.G env-var sync failed: %s", exc)


async def _emit_chain_aware_artifacts(
    ctx: Any,
    project_id: str,
    *,
    chains_json: list[dict],
    project_vars: dict[str, str],
) -> None:
    """Phase D1 + D3 — write chain-aware Newman + k6 to disk so Phase 4
    Automation Generation picks them up.

    The orchestrator's automation_engineer is LLM-driven and runs in
    Stage 3; these chain-aware artifacts land before Stage 3 starts so
    the engineer can reference them as scaffolds. They also serve as a
    deterministic fallback when the LLM generation fails.

    Output paths:
      src/automation/newman/{req}_chain.postman_collection.json (happy)
      src/automation/newman/{req}_chain_adv.postman_collection.json (adversarial)
      src/automation/k6/{req}_chain.js
    """
    try:
        from . import chain_aware_newman, chain_aware_k6
    except ImportError as exc:
        log.warning("discovery_executor: chain-aware emitters unavailable: %s", exc)
        return

    # Try to extract a usable requirement_id; fall back to the project's
    # canonical prefix so chain artifacts get filenames the dispatch filter
    # can pick up. Pre-fix this fell back to "REQ-UNKNOWN", producing
    # `req_unknown_chain*.js` files that the k6 runner's project_prefix
    # run despite chain artifacts existing.
    req_id = "REQ-UNKNOWN"
    try:
        if isinstance(ctx.requirements, list) and ctx.requirements:
            req_id = str(ctx.requirements[0].get("id") or req_id)
    except (AttributeError, IndexError, KeyError):
        pass
    # R-K6ProjectFilter — when no concrete requirement_id is available
    # (K1 hook creates a synthetic _PreflightCtx with requirements=[{"project": ...}]),
    # derive the project's canonical prefix from any project requirement
    # so output filenames match the k6/Newman dispatch's project_prefix
    # filter. The function already receives `project_id` as a positional
    # argument (string) — use it directly.
    if req_id == "REQ-UNKNOWN" and project_id:
        try:
            from ..api.routers.tests_helpers import _get_project_req_ids
            project_req_ids = _get_project_req_ids(str(project_id))
            if project_req_ids:
                req_id = sorted(project_req_ids)[0]
                log.info(
                    "discovery_executor: derived req_id=%s from project %s "
                    "(was REQ-UNKNOWN); chain artifacts will match the "
                    "k6/Newman project_prefix filter",
                    req_id, project_id,
                )
        except Exception as _exc:
            log.debug("discovery_executor: req_id derivation skipped: %s", _exc)

    base_url = project_vars.get("base_url") or project_vars.get("BASE_URL")

    out_newman = Path("src/automation/newman")
    out_k6 = Path("src/automation/k6")
    out_newman.mkdir(parents=True, exist_ok=True)
    out_k6.mkdir(parents=True, exist_ok=True)

    for idx, chain in enumerate(chains_json):
        suffix = "" if idx == 0 else f"_{idx}"
        # Newman happy + adversarial
        try:
            collections = chain_aware_newman.build_chain_aware_newman(
                chain, requirement_id=req_id, base_url=base_url, emit_adversarial=True,
                project_id=project_id,  # R217 — surface-ground out dead `/api/v1/*` probe-self-guess nodes
            )
            happy, adv = collections
            # R217.B — do NOT emit an all-dead chain. When the R217 surface-
            # grounding skip (passed via project_id above) removes every node
            # because the chain was built purely from probe-self-guess
            # `/api/v1/*` paths the SUT 404s, `happy` comes back with zero
            # items. Writing it ships a guaranteed-0%-pass Newman collection
            # that drags the measured pass rate (run-51ec92: 42 stale chain
            # files carried 144 dead `/api/v1/*` steps → Newman raw 9.49%).
            # Skip writing (and remove any stale prior emission) so only
            # chains with ≥1 real, grounded node reach dispatch. Truthful: an
            # empty chain is absent, not a fake pass. Killswitch
            # ARTA_R217_B_SKIP_EMPTY_CHAIN_DISABLE=1 restores writing empties.
            _happy_items = len(happy.get("item", [])) if isinstance(happy, dict) else 0
            _skip_empty = os.environ.get("ARTA_R217_B_SKIP_EMPTY_CHAIN_DISABLE") != "1"
            _happy_fp = out_newman / f"{sanitize_req_id(req_id)}_chain{suffix}.postman_collection.json"
            _adv_fp = out_newman / f"{sanitize_req_id(req_id)}_chain{suffix}_adv.postman_collection.json"
            if _skip_empty and _happy_items == 0:
                for _stale in (_happy_fp, _adv_fp):
                    try:
                        if _stale.is_file():
                            _stale.unlink()
                    except OSError:
                        pass
                log.info(
                    "discovery_executor: R217.B skipped all-dead chain %s%s "
                    "(0 grounded nodes after dead-endpoint skip)", req_id, suffix,
                )
                continue
            _happy_fp.write_text(json.dumps(happy, indent=2))
            _adv_fp.write_text(json.dumps(adv, indent=2))
        except Exception as exc:
            log.warning("discovery_executor: Newman emission failed for %s: %s", req_id, exc)

        # k6 chain
        try:
            script = chain_aware_k6.build_chain_aware_k6(
                chain, requirement_id=req_id, project_vars=project_vars,
            )
            _k6_fp = out_k6 / f"{sanitize_req_id(req_id)}_chain{suffix}.js"
            # R213.K.5 — build_chain_aware_k6 returns "" when the chain has no
            # SUT-contract node (only third-party/static/SPA noise). Skip emitting
            # such a spec + remove any stale noise-only file from a prior run.
            if not script:
                if _k6_fp.is_file():
                    try:
                        _k6_fp.unlink()
                    except OSError:
                        pass
            else:
                _k6_fp.write_text(script)
        except Exception as exc:
            log.warning("discovery_executor: k6 emission failed for %s: %s", req_id, exc)

    log.info(
        "discovery_executor: emitted chain-aware artifacts for %s (chains=%d, base_url=%s)",
        req_id, len(chains_json), base_url,
    )


async def _stamp_last_discovery(project_id: str, *, run_id: str, envvars_harvested: int) -> None:
    """Update `projects.discovery_settings` with last-discovery bookkeeping.

    Best-effort: failures are logged, not raised. The orchestrator's stage
    completion is independent of this write.
    """
    try:
        from ..db.db_adapter import try_db
        from ..db import models
        from . import discovery_settings as ds
        import sqlalchemy as sa
    except ImportError as exc:
        log.debug("discovery_executor: db imports unavailable (%s)", exc)
        return

    async for session in try_db():   # type: ignore
        try:
            stmt = sa.select(models.Project).where(models.Project.id == project_id)
            result = await session.execute(stmt)
            project = result.scalar_one_or_none()
            if project is None:
                return
            project.discovery_settings = ds.stamp_last_discovery(
                project.discovery_settings,
                run_id=run_id,
                envvars_harvested=envvars_harvested,
            )
            await session.commit()
        except Exception as exc:
            log.debug("discovery_executor: stamp commit failed: %s", exc)
        return


__all__ = ["execute"]
