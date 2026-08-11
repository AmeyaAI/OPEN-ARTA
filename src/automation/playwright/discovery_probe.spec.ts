/**
 * R8 — Discovery Probe (Phase B2/I8)
 *
 * Generates a network-rich HAR that the env-var harvester
 * (`api_discovery.harvest_envvars_from_har`) and chain extractor
 * (`call_chain.extract_chains_from_har`) can mine for path-param env vars
 * and call-chain dependencies.
 *
 * Design rules:
 *   - **Self-contained**: NO imports beyond `@playwright/test`. The probe
 *     must compile + run even when ARTA-generated specs in the same
 *     directory are broken (missing fixtures, duplicate `test` symbols).
 *   - **Owns its own context** with explicit `recordHar`. Project-level
 *     `recordHar` proved unreliable in our Playwright version when combined
 *     with inline `storageState` — the HAR file was never written even on
 *     successful test runs. Owning the context here makes HAR production
 *     deterministic: `context.close()` flushes the HAR or throws.
 *   - **Soft-fail**: every HTTP step is in try/catch. The probe's
 *     deliverable is the HAR, not assertion correctness. A 401/403
 *     response is still useful — the URL template lands in the HAR
 *     for the harvester to template-match against future runs.
 *   - **SUT-agnostic**: parameterised via `process.env.TARGET_BASE_URL`.
 *     The path list covers canonical "list → detail" verbs every
 *     SaaS-like API exposes; harvester picks up whatever 200s come back.
 */
import { test, expect, chromium } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';

import { refreshAuthIfExpiring } from '../common/auth_refresh';

// Fix 1 — mid-run auth re-mint: keep the bearer fresh across the long run
test.beforeEach(async ({ page, request }) => {
  try { await refreshAuthIfExpiring(page, request); } catch { /* best-effort */ }
});
// R292 — GENERICITY: no SUT-specific default. The executor ALWAYS sets
// fallback silently assumed one SUT in the shared probe. Empty string when
// unset — a caller that forgets to set it hits an obvious "" URL, not a covert
const BASE_URL = process.env.TARGET_BASE_URL || '';
// R219.E — HashRouter support. Some SPAs (e.g. React HashRouter) route via
// the URL fragment: the real route for "/portal" is "<base>/#/portal", not
// "<base>/portal" (which serves the shell → router defaults to root/login →
// DOM snapshot captures the login page → empty dom_catalog). `_r219_e_hash`
// starts from the explicit config flag and is auto-upgraded to true after the
// root navigation if the SPA parks itself on a hash route. `buildRouteUrl`
// then inserts the "#" so authenticated views actually render for snapshots.
let _r219_e_hash = process.env.TARGET_SPA_HASH_ROUTING === '1';
function buildRouteUrl(route: string): string {
  if (!_r219_e_hash || route === '/') return BASE_URL + route;
  // Avoid double "#"; route already begins with "/".
  return BASE_URL.replace(/\/$/, '') + '/#' + route;
}
// R8 — most SaaS deploys put the API on a separate subdomain (e.g.
// `backend.<sut>`). When `${BASE_URL}/api/v1/...` returns the SPA
// index.html (200 OK with HTML), the harvester sees no JSON → 0 env
// vars. API_BASE_URL is the actual backend; falls back to BASE_URL when
// the SUT serves API + UI on the same host.
const API_BASE_URL = process.env.API_BASE_URL
  || process.env.TARGET_API_BASE_URL
  || BASE_URL;

const HAR_PATH = process.env.ARTA_HAR_OUT
  // R212 — was '../../.arta/...' which from src/automation/playwright resolves to
  // src/.arta (one ../ short) → ENOENT. The repo .arta is at the project root.
  || path.resolve(__dirname, '../../../.arta/discovery/discovery.har');

const COOKIE_NAME = process.env.TARGET_AUTH_COOKIE_NAME || '';
const COOKIE_VALUE = process.env.TARGET_AUTH_COOKIE_VALUE || '';
const BEARER = process.env.TARGET_AUTH_BEARER_TOKEN || '';

// R313.C (AuthAdapter / C11) — the R37.5 pre-flight auth-liveness path was
// platform code. Each SUT exposes a different authenticated "whoami"-style GET
// TARGET_AUTH_LIVENESS_PATH (sourced from .arta/projects.json discovery_settings).
// Default preserves the historical value for back-compat; the R83 bail logic below
// is tolerant of 404 anyway, so a mis-set path degrades gracefully to "continue".
const AUTH_LIVENESS_PATH = process.env.TARGET_AUTH_LIVENESS_PATH || '/api/v1/users/me';
const AUTH_LIVENESS_URL = `${API_BASE_URL}${AUTH_LIVENESS_PATH}`;

// R8 — seeded env-var values (TARGET_ENV_<KEY>) the operator declared
// in the project's environment map. Used to follow `/<resource>/{id}`
// detail pages so the harvester can promote the canonical {id} pattern.
function seededId(name: string): string | undefined {
  const v = process.env['TARGET_ENV_' + name.toUpperCase()];
  return v && v !== 'REPLACE_ME' ? v : undefined;
}

// (path, optional seeded-id env-var name to follow a detail page)
// guesses into the grounding surface → 405/404 gen failures. Gate them:
// ARTA_R221_HARDCODED_PROBES_DISABLE=1. The R151.B dynamic probes (derived
// from the SUT's OWN discovered endpoints) still fire regardless.
const _R221_HARDCODED_PROBES: Array<[string, string | undefined]> = [
  ['/api/v1/organizations',  'organization_id'],
  ['/api/v1/users/me',       undefined],
  ['/api/v1/subscriptions',  'subscription_id'],
  ['/api/v1/subscribers',    'subscriber_id'],
  ['/api/v1/projects',       'project_id'],
  ['/api/v1/datasets',       'dataset_id'],
  ['/api/v1/schemas',        'schema_id'],
  ['/api/v1/collections',    'collection_id'],
  ['/api/v1/fieldsets',      'fieldset_id'],
  ['/api/v1/accounts',       'account_id'],
  ['/api/v1/insights',       undefined],
  ['/api/v1/metrics',        undefined],
];
const API_PROBES: Array<[string, string | undefined]> =
  process.env.ARTA_R221_HARDCODED_PROBES_DISABLE === '1' ? [] : _R221_HARDCODED_PROBES;

test('discovery probe — homepage + canonical APIs', async () => {
  // Bypass the project-level fixtures entirely so we can fully control
  // recordHar lifecycle. Project-level `use.recordHar` in our Playwright
  // version doesn't reliably write the HAR; doing it manually here does.
  // R212 — docker-resilience chromium args. The probe was launching bare
  // `{ headless: true }` with NO args, so chromium used the container's limited
  // /dev/shm (shm_size: 256m). Heavy DATA-route pages (dataset/collection
  // tables, dashboards) exhaust /dev/shm → the renderer process crashes →
  // `page.evaluate: Target page, context or browser has been closed` mid DOM
  // snapshot → those routes get added to ARTA_R150_SKIP_ROUTES → the probe never
  // captures their (analytics/dataset) JSON → dataset/analytics recipes can't
  // ground. The SUT renders these routes fine in a real browser (full memory),
  // so this is a PROBE container-config defect, NOT a SUT issue.
  // `--disable-dev-shm-usage` makes chromium use /tmp instead of /dev/shm (the
  // standard docker fix); --no-sandbox/--disable-gpu are the standard
  // headless-in-docker flags. Opt-out ARTA_R212_PROBE_SHM_FIX_DISABLE=1.
  const _r212_chromium_args = process.env.ARTA_R212_PROBE_SHM_FIX_DISABLE === '1'
    ? []
    : ['--disable-dev-shm-usage', '--no-sandbox', '--disable-gpu'];
  const browser = await chromium.launch({ headless: true, args: _r212_chromium_args });
  const headers: Record<string, string> = {};
  // REVIEW-V2 — skip redacted placeholders. ARTA stores `***` (or
  // similar) in projects.json so the file can be committed; the real
  // values live elsewhere (storage state, env vars). Sending
  // API probe — visible in the HAR but destroys the auth path.
  const REDACTED = new Set(['***', 'REDACTED', 'REPLACE_ME']);
  if (COOKIE_NAME && COOKIE_VALUE && !REDACTED.has(COOKIE_VALUE)) {
    headers['Cookie'] = `${COOKIE_NAME}=${COOKIE_VALUE}`;
  }
  if (BEARER && !REDACTED.has(BEARER)) {
    headers['Authorization'] = `Bearer ${BEARER}`;
  }

  // R8 — load the operator-curated Playwright storage state when present.
  // This is the auth that lets the SPA load authenticated views (and
  // therefore make real API calls naturally). Without it, the SPA renders
  // the login page, makes ZERO API calls, and the HAR is full of static
  // assets only.
  const storagePath = process.env.TARGET_AUTH_STATE_PATH;
  let storageState: any = undefined;
  if (storagePath && fs.existsSync(storagePath)) {
    try {
      const parsed = JSON.parse(fs.readFileSync(storagePath, 'utf8'));
      if (Array.isArray(parsed.cookies) && Array.isArray(parsed.origins)) {
        storageState = parsed;
        // eslint-disable-next-line no-console
        console.log(`[probe] using storageState from ${storagePath} (${parsed.cookies.length} cookies)`);
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn(`[probe] could not parse storageState at ${storagePath}: ${e}`);
    }
  }

  // R150.A KEYSTONE — capture FULL HAR including response bodies.
  // Pre-R150.A: `mode: 'minimal'` stripped response bodies for most
  // (.arta/discovered_endpoints/<pid>.json): only 6 of 500 endpoints
  // had `response_body_shape` populated. The 494 empty shapes meant
  // R130.H grounded-hint synthesis (Pillar 1 test-case quality),
  // R111.G assertion-grounding (Pillar 1b script quality), and the
  // R55.12 recipe verifier ALL operated against empty shape sets →
  // LLM hallucinations propagated to Gherkin + scripts + recipes.
  //
  // Post-R150.A: `mode: 'full'` captures all bodies. Existing
  // `_ingest_har` at sut_onboarding.py:319 already infers
  // `response_body_shape` via `_infer_shape(response_sample)` — no
  // Python-side change needed; this is the missing upstream signal.
  //
  // pre-R150.A HAR was ~19MB; expect ~60-90MB post-R150.A. The
  // `har_redactor.DEFAULT_MAX_FILE_BYTES` cap was bumped from 50MB →
  // 150MB in R150.A.2 to absorb this. Per-body cap (64KB via
  // `DEFAULT_MAX_BODY_BYTES`) preserves shape-inferability while
  // bounding pathological large responses.
  //
  // Killswitch: `ARTA_R150_A_BODY_CAPTURE_DISABLE=1` reverts to
  // `mode: 'minimal'` for emergency rollback.
  const r150aDisabled = process.env.ARTA_R150_A_BODY_CAPTURE_DISABLE === '1';
  const harMode: 'full' | 'minimal' = r150aDisabled ? 'minimal' : 'full';
  const context = await browser.newContext({
    recordHar: { path: HAR_PATH, mode: harMode, content: 'embed' },
    extraHTTPHeaders: Object.keys(headers).length ? headers : undefined,
    storageState,
    ignoreHTTPSErrors: true,
  });

  // R240 — inject the SPA session token into localStorage so the SPA's CLIENT-SIDE
  // auth gate (which reads localStorage, NOT the Authorization HEADER set above)
  // (Auth0 SPA) reads `localStorage.access_token`; the discovery context uses an
  // EMPTY storageState (R8) and the probe only set an HTTP Authorization header
  // (works for XHR, USELESS for the SPA's client auth check) → EVERY route rendered
  // catalog + the #1 PW selector-timeout cause). addInitScript runs BEFORE the
  // SPA's scripts on every navigation. Uses TARGET_SPA_TOKEN_LS_KEY (default
  // 'access_token'). Killswitch ARTA_R240_SPA_TOKEN_INJECT_DISABLE=1.
  if (BEARER && process.env.ARTA_R240_SPA_TOKEN_INJECT_DISABLE !== '1') {
    const _r240_key = process.env.TARGET_SPA_TOKEN_LS_KEY || 'access_token';
    try {
      await context.addInitScript(([k, v]: [string, string]) => {
        try { localStorage.setItem(k, v); } catch { /* ignore */ }
      }, [_r240_key, BEARER]);
      // eslint-disable-next-line no-console
      console.log(`[probe] R240: injected SPA token into localStorage['${_r240_key}'] for client-side auth`);
    } catch (_r240_exc) {
      // eslint-disable-next-line no-console
      console.warn(`[probe] R240: localStorage token inject failed: ${(_r240_exc as Error)?.message ?? _r240_exc}`);
    }
  }

  // R154.A Layer 1 — STRUCTURAL non-mutation guarantee. Chromium-level
  // HTTP-method allowlist. Every request the probe attempts is intercepted
  // at the network layer BEFORE it reaches the SUT. Non-read methods
  // (POST/PUT/PATCH/DELETE/CONNECT/TRACE) are aborted with
  // `blockedbyclient` — the SUT NEVER sees them, even if Layer 2 (label
  // allowlist) or Layer 3 (element-type guard) miscategorize an element.
  //
  // This is the architectural fix demanded by the v2 operator directive:
  // non-mutation by construction, NOT by heuristic. The denylist approach
  // (v1) was incomplete by design — Layer 1 closes the gap regardless of
  // SUT vocabulary.
  //
  // Operator escape hatch: `ARTA_R154_A_LAYER1_DISABLE=1` reverts to
  // pass-through (probe may mutate SUT — emits loud WARN log).
  const R154_A_LAYER1_DISABLED = process.env.ARTA_R154_A_LAYER1_DISABLE === '1';
  const _R154_A_READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
  let _r154_a_blocked_count = 0;
  const _r154_a_blocked_samples: string[] = [];
  // R211 Phase F — workflow-aware capture: record the would-be REQUEST BODY of
  // each blocked non-GET BEFORE aborting. The request is aborted (the SUT never
  // sees it → R154 non-mutation guarantee intact), but ARTA learns the
  // action-endpoint request_body_shape → feeds Phase C body synthesis + Phase E
  // action chains (which are empty today: this SUT has 0 captured POST bodies).
  // Opt-out: ARTA_R211_F_BODY_CAPTURE_DISABLE=1.
  const _r211_f_capture = process.env.ARTA_R211_F_BODY_CAPTURE_DISABLE !== '1';
  const _r211_f_bodies: Array<{ method: string; url: string; postData: string }> = [];
  // R265 — auth-refresh FULFILLED LOCALLY (never forwarded to the SUT).
  //
  // The SPA proactively POSTs its token-refresh endpoint. R154.A Layer 1 aborted
  // it (POST => "mutation"), so the SPA concluded its session was dead, called
  // logout, and rendered Sign In for EVERY subsequent route -- the DOM catalog
  // became login chrome, the LLM had no real selectors to ground on, and every
  // generated PW spec timed out. Evidence (run 2026-07-17, R211 Phase F capture):
  //
  // We do NOT relax the guarantee to fix this. The refresh is answered from the
  // tokens ARTA ALREADY HOLDS and the request is ABORTED-BY-FULFILL: the SUT
  // never receives it. That is STRICTER than allowing it through, and it also
  // stops the SPA silently consuming the on-disk refresh token (which is
  // single-use/rotating -- the known G5 storage-state staleness bug).
  //
  // Generic by construction: the matcher and the response template are per-project
  // config (`discovery_settings.auth_refresh_fulfill`), never hardcoded here.
  // `${key}` placeholders resolve against the storage-state localStorage the probe
  // already loaded, plus a few computed values. No config => no fulfill => strict
  // R154.A abort (fails CLOSED). Killswitch ARTA_R265_AUTH_REFRESH_FULFILL_DISABLE=1.
  const _r265_match = process.env.ARTA_R265_AUTH_REFRESH_FULFILL_DISABLE === '1'
    ? '' : (process.env.TARGET_AUTH_REFRESH_MATCH || '');
  const _r265_tmpl = process.env.TARGET_AUTH_REFRESH_RESPONSE || '';
  let _r265_fulfilled = 0;

  // R266 — OPERATOR-NAMED read-POST allowlist.
  //
  // cannot render ANY feature page without two of them:
  //   POST /menumanagement/api/getUserMenus          -> the nav menu
  //   POST /UserManagement/api/userManagement/getUser -> the user profile
  // R154.A aborted both (POST => "mutation"), so every feature route sat on a
  // permanent spinner, R180's hydration gate timed out, the route was skipped,
  // and NOTHING was cataloged -> the LLM had no selectors -> PW timeouts.
  //
  // This is an EXPLICIT operator-maintained list of exact paths, NOT a name
  // heuristic: a `get*`-prefix rule would happily allow `getOrCreateX`, and the
  // SUT's own contract also declares real mutations (saveSchedule, editMenus).
  // Empty/absent config => nothing allowed (fails CLOSED). Matching is
  // same-origin + exact-path (query string ignored) so a substring can't widen
  // it. Killswitch ARTA_R266_POST_READ_ALLOWLIST_DISABLE=1.
  const _r266_allow: string[] = (() => {
    if (process.env.ARTA_R266_POST_READ_ALLOWLIST_DISABLE === '1') return [];
    try {
      const raw = JSON.parse(process.env.TARGET_POST_READ_ALLOWLIST || '[]');
      return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string' && x.startsWith('/')) : [];
    } catch { return []; }
  })();
  let _r266_allowed = 0;
  const _r266_allowed_paths = new Set<string>();

  /** Exact-path match against the operator's allowlist (query ignored). */
  function _r266_isAllowedRead(url: string): boolean {
    if (!_r266_allow.length || !url.startsWith(BASE_URL)) return false;
    let p: string;
    try { p = new URL(url).pathname.replace(/\/+$/, ''); } catch { return false; }
    return _r266_allow.some((a) => a.replace(/\/+$/, '') === p);
  }

  /** Resolve `${key}` from storage-state localStorage + computed session values. */
  function _r265_render(): string {
    const ls: Record<string, string> = {};
    for (const o of (storageState?.origins || [])) {
      for (const it of (o?.localStorage || [])) {
        if (it && typeof it.name === 'string') ls[it.name] = String(it.value ?? '');
      }
    }
    if (BEARER) ls['access_token'] = BEARER;   // freshest wins
    const now = new Date();
    const ttlSec = 1800;
    const computed: Record<string, string> = {
      _arta_issued: now.toUTCString(),
      _arta_expires: new Date(now.getTime() + ttlSec * 1000).toUTCString(),
      _arta_expires_in: String(ttlSec),
    };
    return _r265_tmpl.replace(/\$\{([A-Za-z0-9_.-]+)\}/g, (m, k) => {
      const v = computed[k] ?? ls[k];
      if (v === undefined) return m;            // unknown key: leave literal, surfaced below
      return v.replace(/["\\]/g, (c) => '\\' + c);   // keep the JSON template valid
    });
  }

  if (!R154_A_LAYER1_DISABLED) {
    await context.route('**/*', async (route) => {
      const req = route.request();
      const method = (req.method() || 'GET').toUpperCase();
      if (_R154_A_READ_METHODS.has(method)) {
        await route.continue();
        return;
      }
      // R265 — fulfill the configured auth-refresh locally. SAME-ORIGIN ONLY:
      // never synthesize a token response to a third-party host.
      if (_r265_match && _r265_tmpl
          && req.url().startsWith(BASE_URL) && req.url().includes(_r265_match)) {
        try {
          const _body = _r265_render();
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: _body,
          });
          _r265_fulfilled += 1;
          // eslint-disable-next-line no-console
          console.log(`[probe] R265: auth-refresh FULFILLED locally (#${_r265_fulfilled}) — `
            + `SUT never received ${method} ${req.url().split('?')[0]}`);
          return;
        } catch (_r265_exc) {
          // eslint-disable-next-line no-console
          console.warn(`[probe] R265: fulfill failed (${(_r265_exc as Error)?.message}) — `
            + `falling back to strict R154.A abort`);
        }
      }
      // R266 — operator-named read-POST: let it through so the SPA can render.
      // Named explicitly by the operator (never inferred); everything unnamed
      // still aborts below.
      if (_r266_isAllowedRead(req.url())) {
        _r266_allowed += 1;
        const _p = req.url().split('?')[0].replace(BASE_URL, '');
        if (!_r266_allowed_paths.has(_p)) {
          _r266_allowed_paths.add(_p);
          // eslint-disable-next-line no-console
          console.log(`[probe] R266: ALLOW operator-named read-POST ${method} ${_p}`);
        }
        await route.continue();
        return;
      }
      _r154_a_blocked_count += 1;
      if (_r154_a_blocked_samples.length < 8) {
        _r154_a_blocked_samples.push(`${method} ${req.url().slice(0, 120)}`);
      }
      // R211 Phase F — capture the would-be body BEFORE the abort below.
      // Only SUT-origin endpoints: third-party telemetry (Google Firestore,
      // analytics, CDNs) fires non-GETs during read-nav but is NOT a SUT action
      // body — capturing it would pollute Phase C synthesis with noise.
      if (_r211_f_capture && _r211_f_bodies.length < 200) {
        try {
          const _u = req.url();
          const _lu = _u.toLowerCase();
          const _thirdParty = (
            _lu.includes('firestore') || _lu.includes('googleapis.com') ||
            _lu.includes('google-analytics') || _lu.includes('googletagmanager') ||
            _lu.includes('doubleclick') || _lu.includes('gstatic.com') ||
            _lu.includes('sentry') || _lu.includes('segment.') ||
            _lu.includes('.amplitude.') || _lu.includes('cloudfront.net')
          );
          const _pd = req.postData();
          if (_pd && !_thirdParty) {
            _r211_f_bodies.push({
              method,
              url: _u.split('?')[0],
              postData: _pd.slice(0, 4000),
            });
          }
        } catch { /* postData unavailable for some request types — skip */ }
      }
      await route.abort('blockedbyclient');
    });
    // eslint-disable-next-line no-console
    console.log(
      `[probe] R154.A Layer 1: network-level non-mutation guard active ` +
      `(blocking non-GET/HEAD/OPTIONS at chromium)`
    );
  } else {
    // eslint-disable-next-line no-console
    console.warn(
      `[probe] R154.A Layer 1: DISABLED via ARTA_R154_A_LAYER1_DISABLE=1 — ` +
      `probe may mutate SUT state. Use only with explicit operator approval ` +
      `+ a SUT test sandbox.`
    );
  }

  // R212 — capture GET-JSON RESPONSE SHAPES directly. The HAR-based response
  // capture is sparse (~6/500), so dataset/analytics recipes can't ground to the
  // SUT response shape → recipe ladder TimeoutError / schema-validation fail →
  // for every SUT-origin GET JSON response the probe observes during nav.
  // R154-safe (GET only). Opt-out ARTA_R212_RESPONSE_CAPTURE_DISABLE=1.
  const _r212_resp_capture = process.env.ARTA_R212_RESPONSE_CAPTURE_DISABLE !== '1';
  const _r212_resp_shapes: Array<{ method: string; url: string; status: number; response_body_shape: any; response_value_samples?: Record<string, string[]> }> = [];
  const _r212_resp_seen = new Set<string>();
  function _r212_shape(v: any, depth = 0): any {
    if (depth > 4) return typeof v;
    if (Array.isArray(v)) return v.length ? [_r212_shape(v[0], depth + 1)] : [];
    if (v && typeof v === 'object') {
      const o: any = {};
      for (const k of Object.keys(v).slice(0, 40)) o[k] = _r212_shape((v as any)[k], depth + 1);
      return o;
    }
    return typeof v;
  }
  // R313 — VALUE-domain capture: collect ENUM-LIKE scalar VALUES by field key into
  // a SEPARATE map (response_body_shape stays byte-identical, so every existing
  // shape consumer is unaffected). This grounds assertion value domains in real SUT
  // behavior — the fix for fabricated enums (a generated `validStates` that omits the
  // SUT's real 'registered'). Enum-scoped (short, no-whitespace tokens) → no
  // free-text / PII / long-id capture. Opt-out ARTA_R313_VALUE_CAPTURE_DISABLE=1.
  const _r313_cap = process.env.ARTA_R313_VALUE_CAPTURE_DISABLE !== '1';
  function _r313_collect(v: any, out: Record<string, Set<string>>, depth = 0): void {
    if (depth > 4 || v == null) return;
    if (Array.isArray(v)) { for (const x of v.slice(0, 40)) _r313_collect(x, out, depth + 1); return; }
    if (typeof v === 'object') {
      for (const k of Object.keys(v).slice(0, 40)) {
        const cv = (v as any)[k];
        if (typeof cv === 'string' || typeof cv === 'number' || typeof cv === 'boolean') {
          const s = String(cv);
          if (s.length >= 1 && s.length <= 32 && /^[A-Za-z0-9_.\-]+$/.test(s)) (out[k] = out[k] || new Set()).add(s);
        } else _r313_collect(cv, out, depth + 1);
      }
    }
  }
  if (_r212_resp_capture) {
    context.on('response', async (resp) => {
      try {
        if (_r212_resp_shapes.length >= 300) return;
        const req = resp.request();
        if ((req.method() || 'GET').toUpperCase() !== 'GET') return;
        const st = resp.status();
        if (st < 200 || st >= 400) return;
        const ct = (resp.headers()['content-type'] || '').toLowerCase();
        if (!ct.includes('json')) return;
        const _u = req.url();
        const _lu = _u.toLowerCase();
        if (_lu.includes('firestore') || _lu.includes('googleapis.com') ||
            _lu.includes('google-analytics') || _lu.includes('googletagmanager') ||
            _lu.includes('gstatic.com') || _lu.includes('sentry') ||
            _lu.includes('segment.') || _lu.includes('cloudfront.net')) return;
        const key = _u.split('?')[0];
        if (_r212_resp_seen.has(key)) return;
        const body = await resp.json().catch(() => null);
        if (body == null) return;
        _r212_resp_seen.add(key);
        let _r313_vals: Record<string, string[]> | undefined;
        if (_r313_cap) {
          const _acc: Record<string, Set<string>> = {};
          _r313_collect(body, _acc);
          const _keys = Object.keys(_acc);
          if (_keys.length) { _r313_vals = {}; for (const k of _keys) _r313_vals[k] = Array.from(_acc[k]).slice(0, 12); }
        }
        _r212_resp_shapes.push({ method: 'GET', url: key, status: st, response_body_shape: _r212_shape(body), ...(_r313_vals ? { response_value_samples: _r313_vals } : {}) });
      } catch { /* response body unavailable — skip */ }
    });
  }

  try {
    // R37.5 — pre-flight auth liveness check. Before consuming a 60s
    // navigation budget on a doomed run, hit a known-authenticated API
    // endpoint with the cookie. If the response is 401 or HTML (login
    // redirect), write `auth_failed.flag` next to the HAR and exit
    // early. The orchestrator (R36.1's discovery-empty gate) reads
    // this flag to fail fast with a clear "refresh auth" CTA instead
    // of letting Playwright fail 100% downstream.
    const HAR_DIR_FOR_FLAG = path.dirname(HAR_PATH);
    try {
      fs.mkdirSync(HAR_DIR_FOR_FLAG, { recursive: true });
    } catch { /* exists */ }
    try {
      const liveResp = await context.request.get(AUTH_LIVENESS_URL, { timeout: 6000 });
      const liveCT = liveResp.headers()['content-type'] || '';
      const liveStatus = liveResp.status();
      const liveOk = liveResp.ok() && liveCT.includes('application/json');
      if (!liveOk) {
        // R83 — refine bail-out logic. Pre-R83 the probe aborted on
        // ANY non-(200+JSON) response, including 404 (endpoint not
        // deployed) and 5xx (SUT temporarily down). Live evidence
        // localStorage correctly → pre-flight GET
        // (the SPA's catch-all index.html) → probe aborted before
        // walking SPA routes → discovery harvest empty → R67.C
        // expose /api/v1/users/me — the cookie is still valid for
        // OTHER endpoints (storage, media, collection, etc.). Bail
        // only on definite auth failures: 401/403 OR 200+HTML (login
        // redirect). For 404/5xx/other, log + continue with deep
        // probe — the SPA routes may still produce authenticated XHR
        // traffic the harvester needs.
        const definiteAuthFail = (
          liveStatus === 401 || liveStatus === 403
          || (liveStatus === 200 && liveCT.includes('html'))
        );
        const reason = liveStatus === 401 || liveStatus === 403
          ? 'cookie_invalid_or_expired'
          : (liveStatus === 200 && liveCT.includes('html'))
            ? 'spa_login_redirect'
            : liveStatus === 404
              ? 'preflight_endpoint_not_deployed'
              : `unexpected_status_${liveStatus}`;
        if (definiteAuthFail) {
          const flagPath = path.join(HAR_DIR_FOR_FLAG, 'auth_failed.flag');
          fs.writeFileSync(flagPath, JSON.stringify({
            reason,
            status: liveStatus,
            content_type: liveCT,
            probe_url: AUTH_LIVENESS_URL,
            checked_at: new Date().toISOString(),
          }, null, 2));
          // eslint-disable-next-line no-console
          console.warn(
            `[probe] R37.5 auth pre-flight FAILED (${reason}, status=${liveStatus}). ` +
            `Wrote ${flagPath}; aborting deep probe — operator must refresh auth.`,
          );
          await context.close();
          await browser.close();
          expect(true).toBe(true);
          return;
        }
        // R83 — soft-failure mode. Pre-flight inconclusive but cookie
        // may still be valid for SPA routes. Log + continue.
        // eslint-disable-next-line no-console
        console.warn(
          `[probe] R83: pre-flight inconclusive (status=${liveStatus}, ` +
          `reason=${reason}). The /api/v1/users/me endpoint may not be ` +
          `deployed on this SUT. Continuing deep probe — SPA routes may ` +
          `still produce authenticated traffic.`,
        );
      } else {
        // eslint-disable-next-line no-console
        console.log(`[probe] R37.5 auth pre-flight OK (status=${liveStatus})`);
      }
    } catch (e) {
      // Soft-fail the pre-flight (network blip / SUT down). Continue
      // with the deep probe; it will produce its own failure signal.
      // eslint-disable-next-line no-console
      console.warn(`[probe] R37.5 auth pre-flight could not run: ${e}`);
    }

    // Step 1: navigate the SUT homepage AND a few canonical SPA routes
    // so the SPA's own JS fires real API calls. This is the most
    // reliable harvest signal — the SPA knows the correct API paths;
    // we don't have to guess them. networkidle waits up to 12s for
    // XHR traffic to settle.
    const page = await context.newPage();

    // R154.A Layer 3 — auto-dismiss browser dialogs. If a click
    // accidentally triggers a confirm/alert (e.g., "Are you sure you
    // want to delete?"), DISMISS it (not accept) so the destructive
    // path is declined. Combined with Layer 1's network-level abort,
    // any destructive intent is blocked even if the click leaked past
    // Layer 2's label allowlist.
    page.on('dialog', async (dialog) => {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R154.A: auto-dismissing ${dialog.type()}: ` +
        `"${dialog.message().slice(0, 80)}"`,
      );
      await dialog.dismiss().catch(() => {});
    });

    // R86.0 + R86.1 — principled SPA route discovery.
    // PRIORITY ORDER:
    //   1. captured_endpoints/<pid>.json — REAL paths the SUT has served
    //      previously (R28.6 store). Extract distinct path-prefixes (the
    //      first 2-3 segments) and walk them. This is the highest-signal
    //      source because it reflects what the SUT actually exposes.
    //   2. JWT-derived org routes — when the cookie's JWT carries an
    //      routes like /organization/{org_id}/services|settings|users.
    //      URL pattern is /organization/{org_id}/services.
    //   3. Hardcoded fallback — the original R37.5 list. For SUTs
    //      that have neither captured_endpoints nor a JWT org claim
    //      (cold-start projects), this is the only signal we have.

    // R86.0 — captured-endpoints-driven route discovery
    function loadCapturedSpaPaths(projectId: string | undefined): string[] {
      if (!projectId) return [];
      const captured: string[] = [];
      try {
        // R151.D — canonical path is `.arta/discovered_endpoints/` per the
        // Python writer at `api_discovery.py:442` (`_CAPTURED_DIR`). The
        // probe historically referenced `captured_endpoints/` which doesn't
        // exist on disk → R86.0 silently no-op'd via the ENOENT catch.
        const capturedPath = path.resolve(
          __dirname,
          `../../../.arta/discovered_endpoints/${projectId}.json`,
        );
        if (!fs.existsSync(capturedPath)) return [];
        const data = JSON.parse(fs.readFileSync(capturedPath, 'utf8'));
        // captured_endpoints store shape (R28.6): array of
        // `{endpoint_key, method, path, host, ...}` records OR a dict
        // keyed by endpoint_key. Walk both shapes defensively.
        const records = Array.isArray(data) ? data
                      : Array.isArray(data?.endpoints) ? data.endpoints
                      : Object.values(data || {});
        // Extract distinct SPA path prefixes (NOT API paths). The SPA
        // routes are usually shorter than API paths and lack /api/ prefix.
        // Heuristic: take the first 2 path segments, drop /api/* (those
        // are backend not frontend routes).
        const prefixes = new Set<string>();
        for (const r of records) {
          if (!r || typeof r !== 'object') continue;
          const p = (r as any).path || (r as any).url_path;
          if (typeof p !== 'string' || !p.startsWith('/')) continue;
          // R186.1d — drop API paths wherever `api` appears as a segment, not
          // just the literal `/api/` prefix. Pre-fix `/<account_id>/api/...`
          // (account-id-prefixed API path) slipped through and became a bogus
          // SPA nav target → React-Router 404 → /login → empty DOM catalog.
          if (/(^|\/)api(\/|$)/.test(p)) continue;   // backend API, not SPA route
          // Take the first 2 segments — e.g., `/organization/{id}/services`
          // yields `/organization/{id}` which is a real SPA route segment.
          const segs = p.split('/').filter(Boolean);
          if (segs.length === 0) { prefixes.add('/'); continue; }
          const prefix = '/' + segs.slice(0, 2).join('/');
          // R269 — this derivation invents SPA routes from CAPTURED API paths,
          // and the `/api/` filter above only catches SUTs that put `api` in the
          // and `/{id}/AssignAssetsToLease` became "routes" — 12 of the
          // catalog's 16 junk entries, each burning a slot of the ~25-route
          // budget a REAL feature page should have had, and each walking to a
          // 404 that cataloged nothing but a Yes/No modal.
          //
          //
          // Two unambiguous non-page shapes only (deliberately NOT a verb
          // heuristic — `/Account/GetX` still slips through; that needs a real
          // signal, not a guess).
          // Killswitch: ARTA_R269_NONPAGE_ROUTE_FILTER_DISABLE=1.
          if (process.env.ARTA_R269_NONPAGE_ROUTE_FILTER_DISABLE !== '1') {
            // 1. Unresolved template param IN THE PREFIX: `/{id}/Overview` is an
            //    API contract template, never a literal URL a browser can open.
            if (/\{[^}]*\}|(^|\/):[A-Za-z]/.test(prefix)) continue;
            // 2. Static assets: `/css`, `/bootstrap/4.1.3`, `/data/recordingconf`,
            //    or a prefix whose segment carries a file extension.
            if (/\.(css|js|mjs|map|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|json|txt|xml)$/i.test(prefix)
                || /(^|\/)(css|scss|assets?|static|bootstrap|fonts?|images?|img|dist|build)(\/|$)/i.test(prefix)) {
              continue;
            }
          }
          prefixes.add(prefix);
        }
        captured.push(...Array.from(prefixes));
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn(`[probe] R86.0: captured_endpoints load failed: ${e}`);
      }
      return captured.slice(0, 10);   // cap to 10 routes for perf
    }

    // R186 — load the REAL React-Router routes (resolved :params) that ARTA
    // extracted from the SUT source via `extract_frontend_routes` and wrote to
    // `.arta/frontend_routes/<pid>.json` (or passed via TARGET_FRONTEND_ROUTES).
    // These are the AUTHORITATIVE SPA nav targets — they take priority over the
    // captured-endpoint heuristic so the probe lands on real authenticated pages
    // (e.g. /organizations) instead of guessing API paths → login wall.
    function loadFrontendRoutes(projectId: string | undefined): string[] {
      const routes: string[] = [];
      // Primary: the per-project JSON file.
      try {
        if (projectId) {
          const fp = path.resolve(
            __dirname, `../../../.arta/frontend_routes/${projectId}.json`);
          if (fs.existsSync(fp)) {
            const data = JSON.parse(fs.readFileSync(fp, 'utf8'));
            const recs = Array.isArray(data) ? data : Object.values(data || {});
            for (const r of recs) {
              const rr = (r as any)?.resolved_route;
              if (typeof rr === 'string' && rr.startsWith('/') && !rr.includes('/:')) {
                routes.push(rr);
              }
            }
          }
        }
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn(`[probe] R186: frontend_routes load failed: ${e}`);
      }
      // Fallback: TARGET_FRONTEND_ROUTES env (comma-separated resolved routes).
      if (routes.length === 0 && process.env.TARGET_FRONTEND_ROUTES) {
        for (const rr of process.env.TARGET_FRONTEND_ROUTES.split(',')) {
          const t = rr.trim();
          if (t.startsWith('/') && !t.includes('/:')) routes.push(t);
        }
      }
      if (routes.length) {
        // eslint-disable-next-line no-console
        console.log(`[probe] loadFrontendRoutes: ${routes.length} route(s) prioritized `
          + `(e.g. ${routes.slice(0, 4).join(', ')})`);
      }
      return routes;
    }

    // R150.I — derive analytics SPA routes from captured API paths.
    // Pre-R150.I: `loadCapturedSpaPaths` above SKIPS `/api/*` paths because
    // they're backend endpoints not SPA frontend routes. But many SaaS
    // naming convention where backend `/api/v1/insights/123` corresponds
    // to frontend `/insights/123`. Pre-R150.I the BFS never reached
    // these analytics SPA routes → DOM catalog lacked
    // insights/dashboards/analytics testids + aria-labels → LLM
    // hallucinated selectors → 164 PW locator timeouts in Iter 9.
    //
    // Post-R150.I: read captured_endpoints, filter for analytics-domain
    // keywords (insights/dashboards/analytics/metrics/queries/reports/
    // visualizations), strip `/api/v{N}` prefix, derive 2-segment SPA
    // route. Cap at 20 routes for perf.
    //
    // Killswitch: ARTA_R150_I_API_ROUTE_MAP_DISABLE=1 reverts to Iter 9
    // behavior (no derived analytics routes).
    function loadCapturedSpaRoutesFromApiPaths(
      projectId: string | undefined,
    ): string[] {
      if (!projectId) return [];
      if (process.env.ARTA_R150_I_API_ROUTE_MAP_DISABLE === '1') return [];
      const derived: string[] = [];
      try {
        // R151.D — canonical path is `discovered_endpoints/` (single source
        // of truth: Python writer at api_discovery.py:442). Pre-R151.D the
        // probe's R150.I helper read the wrong dir → analytics-route
        // derivation silently failed → BFS never reached deeper routes.
        const capturedPath = path.resolve(
          __dirname,
          `../../../.arta/discovered_endpoints/${projectId}.json`,
        );
        if (!fs.existsSync(capturedPath)) return [];
        const data = JSON.parse(fs.readFileSync(capturedPath, 'utf8'));
        const records = Array.isArray(data) ? data
                      : Array.isArray(data?.endpoints) ? data.endpoints
                      : Object.values(data || {});

        // Match `/api/v{N}/segment/...` or `/api/segment/...`
        const apiPrefixRe = /^\/api\/(?:v\d+\/)?/;
        // Analytics-domain first-segment keywords (lower-case match)
        const analyticsKeywords = new Set([
          'insights', 'insight',
          'dashboards', 'dashboard',
          'analytics',
          'metrics', 'metric',
          'queries', 'query',
          'reports', 'report',
          'visualizations', 'visualization',
        ]);
        const candidates = new Set<string>();

        for (const r of records) {
          if (!r || typeof r !== 'object') continue;
          const p = (r as any).path || (r as any).url_path;
          if (typeof p !== 'string' || !p.startsWith('/api/')) continue;

          const stripped = p.replace(apiPrefixRe, '/');
          const segs = stripped.split('/').filter(Boolean);
          if (segs.length === 0) continue;
          if (!analyticsKeywords.has(segs[0].toLowerCase())) continue;

          // Derive SPA route: keep first 2 segments to capture e.g.
          // `/insights/sales-2024` → `/insights/sales-2024`, or
          // `/insights` alone for single-segment paths.
          const derivedRoute = segs.length >= 2
            ? '/' + segs.slice(0, 2).join('/')
            : '/' + segs[0];
          candidates.add(derivedRoute);
        }

        derived.push(...Array.from(candidates));
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn(
          `[probe] R150.I: derive analytics routes failed: ${e}`,
        );
      }
      return derived.slice(0, 20);   // cap to 20 routes
    }

    // R151.B KEYSTONE — dynamic API_PROBES expansion. Pre-R151.B the
    // probe fired direct authenticated XHRs against only the 12
    // hardcoded API_PROBES paths (none of them analytics-class). With
    // R150.A `mode:'full'` HAR capture active, the SPA navigation
    // captured XHRs for whatever the SPA happened to fire during route
    // Post-R151.B: read `.arta/discovered_endpoints/<pid>.json`, filter
    // for analytics-domain paths (insight|pipeline|dashboard|dataset|
    // query|metric|analytic|result|nl), dedupe against hardcoded
    // API_PROBES, cap at 30 dynamic entries. Each dynamic path joins
    // the existing API_PROBES loop body → direct authenticated XHR →
    // body lands in HAR via R150.A → ingest infers response_body_shape.
    //
    // Killswitch: ARTA_R151_B_DYNAMIC_PROBES_DISABLE=1 reverts to
    // hardcoded 12 endpoints only.
    function loadCapturedEndpointPaths(
      projectId: string | undefined,
    ): Array<[string, string | undefined]> {
      if (!projectId) return [];
      if (process.env.ARTA_R151_B_DYNAMIC_PROBES_DISABLE === '1') return [];
      const out: Array<[string, string | undefined]> = [];
      try {
        // R151.D — canonical path is `discovered_endpoints/` (matches the
        // Python writer at api_discovery.py:442).
        const capturedPath = path.resolve(
          __dirname,
          `../../../.arta/discovered_endpoints/${projectId}.json`,
        );
        if (!fs.existsSync(capturedPath)) return [];
        const data = JSON.parse(fs.readFileSync(capturedPath, 'utf8'));
        const records = Array.isArray(data) ? data
                      : Array.isArray(data?.endpoints) ? data.endpoints
                      : Object.values(data || {});
        // Analytics-domain filter — matches the dominant Iter-9 PW
        // failure cluster (164 locator timeouts traced to analytics
        // endpoints the LLM hallucinated against because catalog was
        // sparse for these routes).
        const analyticsRe =
          /^\/(?:api\/)?v\d+\/(insight|pipeline|dashboard|dataset|query|metric|analytic|result|nl)/i;
        // A6 platform/SUT-separation: the DEFAULT replay match is now a
        // SUT-AGNOSTIC STRUCTURAL rule — a collection path whose last segment is a
        // plural noun — so a NEW SUT's list endpoints are replayed WITHOUT its
        // vocabulary being hardcoded here (the leak). The legacy analytics/family
        // via ARTA_R151B_REPLAY_KEYWORDS (comma-separated, fed from discovery_settings)
        // — moving the SUT vocabulary from platform code to per-SUT config.
        const _structuralList = (p: string) => {
          const seg = (p.split('?')[0] || '').replace(/\/+$/, '').split('/').pop() || '';
          return /s$/i.test(seg) && !/ss$/i.test(seg);
        };
        const _cfgKw = (process.env.ARTA_R151B_REPLAY_KEYWORDS || '')
          .split(',').map(s => s.trim()).filter(Boolean);
        // Killswitch ARTA_R151B_FILTER_BROADEN_DISABLE=1. Config override wins.
        const familyRe = _cfgKw.length
          ? new RegExp('(' + _cfgKw.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'i')
          : /(lease|geofence|command|asset|device|report|account|billing)/i;
        const broaden = process.env.ARTA_R151B_FILTER_BROADEN_DISABLE !== '1';
        const _matches = (p: string) =>
          _structuralList(p) || analyticsRe.test(p) || (broaden && familyRe.test(p));
        const seen = new Set<string>();
        for (const r of records) {
          if (!r || typeof r !== 'object') continue;
          const method = (((r as any).method) || 'GET').toUpperCase();
          if (method !== 'GET') continue;
          const p = (r as any).path || (r as any).url_path;
          if (typeof p !== 'string' || !_matches(p)) continue;
          // Normalize to canonical `/api/v1/...` form (some captured paths
          // already include /api/ prefix, others don't). Trim to max 4
          // path segments to keep URLs probeable + avoid template ids.
          const norm = p.startsWith('/api/') ? p : '/api' + p;
          const segs = norm.split('/').filter(Boolean);
          const trimmed = '/' + segs.slice(0, 4).join('/');
          if (seen.has(trimmed)) continue;
          seen.add(trimmed);
          out.push([trimmed, undefined]);
          if (out.length >= 30) break;
        }
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn(`[probe] R151.B: dynamic probe load failed: ${e}`);
      }
      return out;
    }

    // R86.1 — JWT-derived org routes (fallback when captured_endpoints
    // is empty/cold-start). Decodes the JWT inline (no external dep)
    // and extracts `organizations[]` claim.
    function extractOrgIdsFromJWT(jwt: string): string[] {
      if (!jwt || jwt === '***' || !jwt.includes('.')) return [];
      try {
        const parts = jwt.split('.');
        if (parts.length < 2) return [];
        // base64url → base64 (replace -/_ with +/=)
        let payload = parts[1].replace(/-/g, '+').replace(/_/g, '/');
        while (payload.length % 4) payload += '=';
        const decoded = JSON.parse(
          Buffer.from(payload, 'base64').toString('utf-8'),
        );
        const orgs = decoded.organizations;
        if (Array.isArray(orgs)) {
          return orgs.filter((o): o is string => typeof o === 'string').slice(0, 3);
        }
        return [];
      } catch {
        return [];
      }
    }

    const PROJECT_ID = process.env.TARGET_PROJECT_ID || process.env.ARTA_PROJECT_ID || '';
    const frontendRoutes = loadFrontendRoutes(PROJECT_ID);   // R186 — authoritative real routes
    const capturedRoutes = loadCapturedSpaPaths(PROJECT_ID);
    const r150iAnalyticsRoutes = loadCapturedSpaRoutesFromApiPaths(PROJECT_ID);
    const jwtOrgIds = extractOrgIdsFromJWT(COOKIE_VALUE);
    const orgRoutes: string[] = [];
    for (const oid of jwtOrgIds) {
      orgRoutes.push(
        `/organization/${oid}`,
        `/organization/${oid}/services`,
        `/organization/${oid}/settings`,
        `/organization/${oid}/users`,
      );
    }
    if (capturedRoutes.length > 0) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R86.0: ${capturedRoutes.length} captured-endpoint route(s) prioritized`,
      );
    }
    if (r150iAnalyticsRoutes.length > 0) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R150.I: ${r150iAnalyticsRoutes.length} analytics SPA route(s) derived from /api/* paths`,
      );
    }
    if (orgRoutes.length > 0) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R86.1: ${orgRoutes.length} JWT-derived org-scoped route(s) (org_ids=${jwtOrgIds.length})`,
      );
    }
    // R37.5 — hardcoded fallback list (preserved for cold-start projects
    // without captured_endpoints or org-claim JWTs).
    // configures the app, clicks "launch app", and lands on
    // NOT the app — walking only `/` captured shell chrome. Seed `/ai-apps`
    // (+ a few known feature routes) so the probe loads the real app entry.
    // Operator can override via ARTA_R182_APP_ENTRY (comma-separated paths).
    // each renders a generic confirm modal → 26/29 catalog routes captured only a
    // "Yes/No" modal (shell-only catalog, starving PW/Axe/ZAP grounding). Gate the
    // discovered routes (frontendRoutes/captured/analytics/org) and only fall back
    // to the generic shell guesses at TRUE cold-start (no real route discovered).
    // Operator override ARTA_R182_APP_ENTRY still wins for any SUT (config-driven).
    // R292 — GENERICITY: per-project route seeds, no SUT-name detection.
    // its own `discovery_settings.app_entry_routes` + `fallback_route_guesses`
    // (executor → ARTA_R182_APP_ENTRY / ARTA_FALLBACK_ROUTE_GUESSES). A SUT that
    // fallbacks); a SUT with none seeds from its OWN discovered routes and only
    // falls back at true cold-start. `_hasConfiguredEntry` replaces the name check.
    const R182_APP_ENTRY = (process.env.ARTA_R182_APP_ENTRY || '')
      .split(',').map((s) => s.trim()).filter(Boolean);
    const _hasConfiguredEntry = R182_APP_ENTRY.length > 0;
    // Shell-route guesses: per-project config, else a generic default list.
    const HARDCODED_FALLBACK_GUESSES = (process.env.ARTA_FALLBACK_ROUTE_GUESSES
      || '/dashboard,/projects,/settings,/users,/accounts,/subscriptions,/datasets,/collections,/insights')
      .split(',').map((s) => s.trim()).filter(Boolean);
    const HARDCODED_ROUTES = [
      '/', ...R182_APP_ENTRY,
      ...(_hasConfiguredEntry ? HARDCODED_FALLBACK_GUESSES : []),
    ];
    // De-dupe across the 3 sources while preserving priority order.
    // R87.1 KEYSTONE — bootstrap-from-root pattern: ALWAYS visit `/` FIRST
    // so React/Angular SPAs run their auth bootstrap (read localStorage,
    // decode JWT, hydrate Redux/router state) on a known mount point.
    // Deep-linking to `/organization/{id}/services` BEFORE bootstrap skips
    // the auth hydration step → SPA's client router sees empty React state
    // → redirects every route to /login → discovery probe harvests 0
    // backend XHRs. Force `/` to lead the walk regardless of what the
    // other sources contributed.
    // R150.L — env-var killswitch for routes that crash chromium mid-evaluate.
    // `/accounts` route closes the page context mid `page.evaluate()`
    // (R19a snapshot OR R140.C BFS link-extract), truncating discovery and
    // producing only 6/500 response_body_shape captures. With
    // ARTA_R150_SKIP_ROUTES="/accounts" the probe completes and captures
    // shapes for the remaining 499 endpoints.
    //
    // R152.A — semantics extended from EXACT-MATCH to PREFIX-MATCH so a
    // routes (R150.I unblocked these post-R151.D; each org-scoped route
    // independently triggers the SPA close pattern). Exact-match with `=`
    // suffix is supported for the rare case operators want exact-match
    // (e.g., `/accounts=` skips ONLY `/accounts`, leaves `/accounts/123`).
    //
    // Format: comma-separated route paths/prefixes (leading slash).
    //   `/accounts=`        → exact-match: skips ONLY /accounts (legacy form)
    //   `/users,/settings`  → prefix-match both
    //
    // Killswitch: leave env var unset (default) to preserve Iter 9 behavior.
    const R150_L_PREFIXES: string[] = [];
    const R150_L_EXACT_SET = new Set<string>();
    for (const raw of (process.env.ARTA_R150_SKIP_ROUTES || '').split(',')) {
      const entry = raw.trim();
      if (!entry) continue;
      if (entry.endsWith('=')) {
        // R152.A exact-match opt-in: trailing `=` denotes "exact match only"
        R150_L_EXACT_SET.add(entry.slice(0, -1));
      } else {
        // R152.A default: prefix-match
        R150_L_PREFIXES.push(entry);
      }
    }
    const R150_L_ACTIVE = R150_L_PREFIXES.length > 0 || R150_L_EXACT_SET.size > 0;
    // R152.A — single SSoT predicate. Returns true when the route should
    // be skipped (matches any prefix OR matches exact-set member).
    function _r150_l_should_skip(route: string): boolean {
      if (R150_L_EXACT_SET.has(route)) return true;
      for (const prefix of R150_L_PREFIXES) {
        if (route === prefix) return true;
        if (route.startsWith(prefix + '/')) return true;
      }
      return false;
    }
    if (R150_L_ACTIVE) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R150.L: ARTA_R150_SKIP_ROUTES active — ` +
        `${R150_L_PREFIXES.length} prefix-match ` +
        `(${R150_L_PREFIXES.join(', ')}), ` +
        `${R150_L_EXACT_SET.size} exact-match ` +
        `(${Array.from(R150_L_EXACT_SET).join(', ')})`
      );
    }

    const _seen = new Set<string>();
    const SPA_ROUTES: string[] = [];
    _seen.add('/');
    SPA_ROUTES.push('/');
    // R150.I — analytics-derived routes inserted between capturedRoutes
    // (priority 1) and orgRoutes (priority 2) per the priority slot in
    // the spec. Reaches analytics SPA components R150.H's hydration wait
    // would otherwise have nothing to wait FOR (catalog miss → BFS never
    // visits → no hydration → no testids in catalog).
    // TRUE cold-start (no real route discovered). When real routes exist, walking
    // the guesses just adds confirm-modal noise to the catalog.
    // R238 — SKIP static-asset / non-route paths in the discovery walk. The
    // captured-endpoint + R150.I-derived route sources include static assets
    // routes were 404-ing asset paths → the #1 PW selector-timeout cause). Mirror
    // ARTA's endpoint _is_pollution filter at the probe's route source.
    // Killswitch ARTA_R238_ASSET_ROUTE_FILTER_DISABLE=1.
    const _R238_STATIC = /\.(?:js|mjs|cjs|css|map|json|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|webp|mp4|wasm)(?:\?|$)/i;
    const _R238_ASSETSEG = /(?:^|\/)(?:static|assets|scripts|commonassets|odccomponents|fonts|images?|dist|build|releases|ajax)(?:\/|$)/i;
    const _isAssetRoute = (r: string): boolean =>
      process.env.ARTA_R238_ASSET_ROUTE_FILTER_DISABLE !== '1'
      && (_R238_STATIC.test(r) || _R238_ASSETSEG.test(r)
        || /builddata|fileloader|brandingconfig|remoteentry|gtag|gtm|pendo|datadog/i.test(r));
    const _realRouteCount = frontendRoutes.length + capturedRoutes.length
      + r150iAnalyticsRoutes.length + orgRoutes.length;
    let _r238_skipped = 0;
    for (const r of [
      ...frontendRoutes,        // R186 priority 1 — real React-Router routes (resolved)
      ...capturedRoutes,
      ...r150iAnalyticsRoutes,
      ...orgRoutes,
      ...HARDCODED_ROUTES,
      ...((!_hasConfiguredEntry && _realRouteCount === 0) ? HARDCODED_FALLBACK_GUESSES : []),
    ]) {
      if (_isAssetRoute(r)) { _r238_skipped += 1; continue; }
      if (!_seen.has(r)) {
        _seen.add(r);
        SPA_ROUTES.push(r);
      }
    }
    if (_r238_skipped > 0) {
      // eslint-disable-next-line no-console
      console.log(`[probe] R238: skipped ${_r238_skipped} static-asset/non-route path(s) from the discovery walk (404-ing asset paths starve the catalog)`);
    }
    // R152.B — maximum-safety mode. When `ARTA_R150_M_HARDCODED_ONLY=1`,
    // strip captured/R150.I/org-derived routes from SPA_ROUTES — leave
    // ONLY `/` + HARDCODED_ROUTES. Use when R150.L+R152.A prefix-skip
    // pattern of crashing chromium on org-scoped + analytics-derived
    // routes). Trade-off: less DOM catalog depth in exchange for HAR
    // finalization + shape capture for the routes that DO work.
    //
    // Killswitch sentinel: unset (default) preserves full BFS scope.
    if (process.env.ARTA_R150_M_HARDCODED_ONLY === '1') {
      const _r152_b_before = SPA_ROUTES.length;
      const _r152_b_kept = new Set<string>(['/', ...HARDCODED_ROUTES]);
      for (let i = SPA_ROUTES.length - 1; i >= 0; i--) {
        if (!_r152_b_kept.has(SPA_ROUTES[i])) {
          _seen.delete(SPA_ROUTES[i]);
          SPA_ROUTES.splice(i, 1);
        }
      }
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R152.B: ARTA_R150_M_HARDCODED_ONLY active — stripped ` +
        `${_r152_b_before - SPA_ROUTES.length} captured/derived route(s); ` +
        `walking ONLY hardcoded routes (count=${SPA_ROUTES.length})`
      );
    }
    // R150.L — filter initial SPA_ROUTES against the skip predicate so
    // crash-prone routes never enter the BFS queue. R152.A: uses the
    // prefix-match-aware `_r150_l_should_skip` instead of exact-match Set.
    if (R150_L_ACTIVE) {
      const _r150_l_before = SPA_ROUTES.length;
      for (let i = SPA_ROUTES.length - 1; i >= 0; i--) {
        if (_r150_l_should_skip(SPA_ROUTES[i])) {
          _seen.delete(SPA_ROUTES[i]);   // re-allow BFS skip-check downstream
          SPA_ROUTES.splice(i, 1);
        }
      }
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R150.L: filtered ${_r150_l_before - SPA_ROUTES.length} ` +
        `route(s) from initial SPA_ROUTES queue (total now ${SPA_ROUTES.length})`
      );
    }
    // R19a — DOM-snapshot directory: HAR file's parent dir. Each route's
    // snapshot lands beside the HAR so the harvester (R19b) can ingest
    // both in one pass.
    const HAR_DIR = path.dirname(HAR_PATH);
    try {
      fs.mkdirSync(HAR_DIR, { recursive: true });
    } catch { /* exists */ }

    // R270 — DOM sidecars are THIS RUN's scratch output; clear stale ones first.
    //
    // `ingest_dom_snapshots` globs every `dom*.json` in this dir with no notion
    // of age, and nothing ever deleted them. So when R180 skips a route (it
    // writes no sidecar), the PREVIOUS run's sidecar for that route survived and
    // was re-ingested as if freshly captured. Measured: dom_portal.json 08:41
    // (this run) next to dom_portal_remote_AlarmReeferReport.json 02:34 -- SIX
    // HOURS stale, still feeding 'Sign In' into the catalog every run.
    //
    // This also made R268 structurally UNABLE to fire: its "no fresh capture"
    // test is `route not in routes`, but the stale sidecar kept putting the
    // route IN routes. Login chrome captured once was immortal.
    //
    // Safe: the durable store is dom_catalog.json, and R203's merge still keeps
    // richer existing catalog entries, so a partial walk never clobbers good
    // history. Only the per-run scratch is cleared.
    // Killswitch: ARTA_R270_SIDECAR_CLEAN_DISABLE=1.
    if (process.env.ARTA_R270_SIDECAR_CLEAN_DISABLE !== '1') {
      try {
        const stale = fs.readdirSync(HAR_DIR)
          .filter((f) => /^dom.*\.json$/.test(f) && f !== 'dom_catalog.json');
        for (const f of stale) {
          try { fs.unlinkSync(path.join(HAR_DIR, f)); } catch { /* ignore */ }
        }
        if (stale.length) {
          // eslint-disable-next-line no-console
          console.log(`[probe] R270: cleared ${stale.length} stale DOM sidecar(s) — `
            + `only THIS run's captures will be ingested`);
        }
      } catch (_r270_exc) {
        // eslint-disable-next-line no-console
        console.warn(`[probe] R270: sidecar clean skipped: ${(_r270_exc as Error)?.message}`);
      }
    }

    // R140.C — bounded BFS navigation-graph crawl with safety caps.
    // Pre-R140.C the loop iterated a fixed SPA_ROUTES array (10
    // hardcoded routes; 7 of those returned no extractable DOM for
    // visiting each route at depth N, extract `a[href]` links + queue
    // them at depth N+1 (bounded by BFS_DEPTH). Hard cap=30 total
    // routes; per-page link cap=20.
    //
    // Safeguards:
    //   - Skip protocol-relative (//x), absolute external (https://...)
    //   - Skip mailto:/tel:/javascript:/data: schemes
    //   - Skip static asset extensions (css/js/woff/png/jpg/gif/svg/ico/map)
    //   - Strip #hash + ?query (different fragments → same route)
    //   - Relative-only (must start with /)
    //
    // Killswitch: ARTA_R140_BFS_DEPTH=0 → revert to hardcoded-only
    // (depth=0 means "no link extraction; visit only the seed routes").
    const R140_BFS_DEPTH = (() => {
      const raw = parseInt(process.env.ARTA_R140_BFS_DEPTH ?? '1', 10);
      return Number.isFinite(raw) ? Math.max(0, Math.min(3, raw)) : 1;
    })();
    // R186 — route cap is env-overridable. On a slow SPA (~35s/route incl. the
    // R180 auth-hydration wait) the default 30 can NEVER complete within any
    // sane test timeout, so the probe times out mid-API-probe → HAR never
    // flushes → harvest empty. When R186 feeds the REAL high-value routes
    // (discovery_executor sets ARTA_R140_ROUTE_CAP), a small cap (~8) lets the
    // probe FINISH + flush the HAR + write a populated catalog of the routes
    // that actually matter. Default 30 preserved for cold-start/other SUTs.
    const R140_TOTAL_ROUTE_CAP = (() => {
      const raw = parseInt(process.env.ARTA_R140_ROUTE_CAP ?? '30', 10);
      return Number.isFinite(raw) && raw > 0 ? raw : 30;
    })();
    const R140_LINKS_PER_PAGE_CAP = 20;
    const _depths = new Map<string, number>();
    for (const r of SPA_ROUTES) {
      _depths.set(r, 0);
    }
    // eslint-disable-next-line no-console
    console.log(
      `[probe] R140.C BFS: depth_cap=${R140_BFS_DEPTH} route_cap=${R140_TOTAL_ROUTE_CAP} ` +
      `seed_routes=${SPA_ROUTES.length}`,
    );

    // R150.H — strict SPA hydration wait. Pre-R150.H the probe relied on
    // Playwright's `networkidle` + R87.1 fixed timeout to decide when the
    // SPA was renderable. For SPAs with async/skeleton-heavy hydration
    // (React Suspense, dynamic-import code-splitting, post-mount data
    // fetches) the DOM snapshot landed mid-hydration: testids/aria-labels
    // for hydrated components were ABSENT. Live Iter 9 evidence: 164 PW
    // `locator.click/fill` timeouts (~57% of PW FAIL cluster) traced to
    // hallucinated UI selectors the LLM emitted because the DOM catalog
    // was sparse for analytics routes.
    //
    // Post-R150.H: after networkidle returns, additionally wait for:
    //   1. document.readyState === 'complete'
    //   2. No `[aria-busy="true"]` or `[data-loading]` placeholders
    //      remaining in the DOM tree (React Skeleton loaders + spinners)
    // Combined with the existing networkidle + R87.1 wait, this gives
    // the SPA a measurable strict-hydration signal before snapshot.
    //
    // Soft-fail: probe's deliverable is HAR; hydration check is
    // best-effort enrichment. The catch() at the call site ensures
    // probe progress even when hydration polling errors.
    //
    // Killswitch: ARTA_R150_H_HYDRATION_STRICT_DISABLE=1 reverts to
    // Iter 9 behavior (no extra strict wait).
    //
    // Reuses R114.C `waitForSPAReady` semantics from
    // src/automation/common/sub_flows.ts:33-49 INLINE (probe self-
    // containment contract — no external imports).
    const R150_H_DISABLED = process.env.ARTA_R150_H_HYDRATION_STRICT_DISABLE === '1';
    // R180 — AUTHENTICATED hydration gate. The pre-R180 gate only checked
    // readyState + aria-busy with a 6s timeout — BELOW the SUT's ~9-10s auth
    // bootstrap. The client auth-guard hadn't resolved yet, so the gate
    // accepted the LOGIN WALL as "hydrated" and the catalog captured 8 login
    // elements (SIGN IN/SIGN UP/…) across every route instead of the real
    // authenticated app → all generated PW specs grounded on the wrong DOM.
    // R180: poll up to ~22s (past the auth bootstrap) and require the page is
    // (a) hydrated AND (b) NOT the login wall — no SIGN IN/SIGN UP/LOGIN
    // control, pathname not under /login|/signin|/auth. Returns a status so
    // the caller can SKIP snapshotting login-wall routes (no poisoned catalog).
    // Killswitch ARTA_R150_H_HYDRATION_STRICT_DISABLE=1 reverts to legacy.
    const R180_AUTH_TIMEOUT_MS = parseInt(
      process.env.ARTA_R180_AUTH_HYDRATION_TIMEOUT_MS ?? '22000', 10,
    );
    // R267.B — last state the hydration gate observed, for the R180 diag line.
    // Outer-scoped so the diag can print what the gate actually decided on.
    let _r267_diag = { ready: false, busy: false, login: false, ctrls: 0, maxCtrls: 0 };
    const waitForSPAHydrationStrict = async (
      pollMs: number = 300,
      timeoutMs: number = R180_AUTH_TIMEOUT_MS,
    ): Promise<'ok' | 'login_wall' | 'timeout'> => {
      if (R150_H_DISABLED) return 'ok';
      const deadline = Date.now() + timeoutMs;
      let lastLoginWall = false;
      // R267 — did we ever see an AUTHENTICATED page that was merely still busy?
      // Pre-R267 a persistent spinner short-circuited the evaluate BEFORE the
      // login check, so the gate could only ever return 'timeout' -- and the
      // route keeps a spinner forever (its report body needs interaction), so 17
      // routes rendering the REAL authenticated nav (46 controls: Lease
      // Management, Asset Monitoring, Audit Trail, ...) were thrown away and the
      // catalog kept STALE login chrome instead. R180's purpose is to keep LOGIN
      // CHROME out of the catalog -- an authenticated page that is still loading
      // is not login chrome. Snapshot it.
      let sawAuthedButBusy = false;
      const minCtrls = Number(process.env.ARTA_R267_MIN_CONTROLS || '10');
      // R267.B — instrumentation. R267 shipped twice and fired ZERO times; both
      // times the fix was aimed at the wrong condition because the diag line
      // on. Record the last observed state so the diag prints the real numbers
      // instead of inviting another blind guess at the threshold.
      _r267_diag = { ready: false, busy: false, login: false, ctrls: 0, maxCtrls: 0 };
      const r267Off = process.env.ARTA_R267_PARTIAL_SNAPSHOT_DISABLE === '1';
      while (Date.now() < deadline) {
        try {
          const state = await page.evaluate(() => {
            const ready = document.readyState === 'complete';
            const busy = !!document.querySelector(
              '[aria-busy="true"], [data-loading], [data-loading="true"]');
            // R180 — authenticated-state assertion (is this the login wall?).
            // R267: evaluated ALWAYS, no longer short-circuited by `busy`.
            const path = (location.pathname || '').toLowerCase();
            const onAuthRoute = /(^|\/)(login|signin|sign-in|sign_in|auth)(\/|$)/.test(path);
            const ctrls = Array.from(
              document.querySelectorAll('button, a, [role="button"], input[type="submit"]'),
            );
            const hasSignIn = ctrls.some((e) => {
              const t = ((e.textContent || (e as HTMLInputElement).value || '') as string)
                .trim().toLowerCase();
              return t === 'sign in' || t === 'signin' || t === 'sign up' || t === 'signup'
                || t === 'log in' || t === 'login' || t === 'continue with google';
            });
            return { ready, busy, login: onAuthRoute || hasSignIn, ctrls: ctrls.length };
          });
          // R267.B — record what the gate saw (max ctrls across the window).
          _r267_diag = { ...state, maxCtrls: Math.max(_r267_diag.maxCtrls, state.ctrls) };
          if (state.ready && !state.busy) {
            // Fully settled: the original R180 decision.
            if (!state.login) return 'ok';
            lastLoginWall = true;
          } else if (state.login) {
            // A login wall is a login wall whether or not it finished loading.
            lastLoginWall = true;
          } else if (state.ctrls >= minCtrls) {
            // R267 — "still loading" covers BOTH a busy flag AND a readyState
            // request pending forever, so readyState stays 'interactive' and the
            // FIRST cut of this fix (which only handled `busy`) never fired:
            // 0 R267 snapshots, 21 skips, unchanged. Any not-settled state with
            // real authenticated controls and no login wall counts.
            sawAuthedButBusy = true;
          }
        } catch {
          /* page closed mid-eval or context destroyed; tolerate */
          return 'timeout';
        }
        await page.waitForTimeout(pollMs);
      }
      if (lastLoginWall) return 'login_wall';
      // R267 — authenticated, just never finished loading: a PARTIAL real DOM
      // beats no DOM (and beats the stale login chrome the merge would keep).
      if (sawAuthedButBusy && !r267Off) {
        // eslint-disable-next-line no-console
        console.log('[probe] R267: authenticated but still busy past the gate — '
          + 'snapshotting the partial DOM (>=' + minCtrls + ' controls, no login wall)');
        return 'ok';
      }
      return 'timeout';
    };

    // R154.A Layer 2+3 — read-side label allowlist + element-type guard +
    // safe-click executor + wait-idle helper. These are the heuristic
    // layers ON TOP of Layer 1's structural network-level guarantee
    // (installed at context creation). Layer 1 ALONE prevents SUT
    // mutation; Layer 2+3 minimize the number of times Layer 1 fires
    // (so SUT logs aren't polluted with `blockedbyclient` aborts on
    // misclassified clicks).
    //
    // Layer 2 — positive-match label allowlist. Click ONLY labels
    // containing known read-intent verbs. Unknown SUT-specific labels
    // (`Wipe Data`, `Purge Cache`, `Reset Org`, `Trigger Workflow`)
    // are NEVER clicked because they don't match the allowlist
    // (fail-CLOSED safety vs v1's denylist which was fail-OPEN).
    const _R154_A_READ_TOKENS: string[] = [
      'view', 'show', 'display', 'preview', 'inspect', 'see', 'browse',
      'load', 'fetch', 'refresh', 'reload', 'sync', 'pull',
      'list', 'all', 'get', 'read', 'open',
      'tab', 'nav', 'menu', 'dashboard', 'home', 'overview',
      'analytics', 'insight', 'report', 'chart', 'graph', 'metric',
      'detail', 'info', 'about', 'summary', 'status',
      // G1 — broaden the read-side surface (still fail-closed: label must
      // contain a token AND element must be a button/link — R154.A guards hold).
      'search', 'filter', 'select', 'expand', 'more', 'next', 'page',
      'history', 'log', 'activity', 'map', 'grid', 'table', 'column',
      'asset', 'device', 'geofence', 'reefer', 'account', 'user', 'command',
      // E2a — generic business-domain NOUNS (read-side; still fail-closed via
      // Layer 3 button/link guard + Layer 1 non-GET abort). NEVER add mutation
      // verbs (create/generate/submit/save/delete/update/run/execute). Opt-out:
      // ARTA_R154_TOKENS_EXTRA_DISABLE=1.
      ...(process.env.ARTA_R154_TOKENS_EXTRA_DISABLE === '1' ? [] : [
        'lease', 'manager', 'billing', 'owner', 'plan', 'invoice',
        'subscription', 'contract', 'order', 'ticket', 'record',
      ]),
    ];
    const _r154_a_is_read_label = (text: string): boolean => {
      if (!text || text.length === 0) return false;
      const lower = text.toLowerCase();
      return _R154_A_READ_TOKENS.some((tok) => lower.includes(tok));
    };

    // Layer 3 — element-type guard. Role MUST be `button` or `link`
    // (excludes checkbox, radio, switch, textbox, combobox — all of
    // which mutate state when clicked/toggled). Tag MUST NOT be
    // `<input>` (no form-submit, no text-fill triggers).
    const _r154_a_is_safe_element = (entry: any): boolean => {
      if (!entry || typeof entry !== 'object') return false;
      const role = ((entry as any).role || '').toString().toLowerCase();
      const tag = ((entry as any).tag || '').toString().toLowerCase();
      if (role !== 'button' && role !== 'link') return false;
      if (tag === 'input') return false;
      return true;
    };

    // Safe-click executor — Tier 1: getByRole, Tier 2: getByText.
    // Bounded ~2s budget per Tier. Soft-fail on any error.
    const _r154_a_safe_click = async (
      pg: any, role: string, name: string,
    ): Promise<boolean> => {
      try {
        const loc = pg.getByRole(role as any, { name, exact: false });
        const count = await loc.count().catch(() => 0);
        if (count > 0) {
          await loc.first().click({ timeout: 2000, trial: false });
          return true;
        }
      } catch { /* fall through */ }
      try {
        const loc = pg.getByText(name, { exact: false });
        const count = await loc.count().catch(() => 0);
        if (count > 0) {
          await loc.first().click({ timeout: 2000 });
          return true;
        }
      } catch { /* swallow */ }
      return false;
    };

    const _r154_a_wait_idle = async (pg: any, maxMs: number = 3000): Promise<void> => {
      await pg.waitForLoadState('networkidle', { timeout: maxMs }).catch(() => {});
    };

    // ── R211 Phase F.2 — GATED action-drive (capture-at-abort) ──────────────
    // Clicks ACTION buttons (create/upload/...) + fills form inputs so the SPA
    // BUILDS a real request body, which the R154.A Layer-1 interceptor captures
    // BEFORE aborting it (the SUT NEVER receives the request → non-mutation
    // guarantee intact). This is how ARTA learns action-endpoint body shapes for
    // Phase C synthesis / Phase E action chains. Opt-in (ARTA_R211_F_ACTION_DRIVE=1)
    // AND fail-CLOSED: refuses to run if Layer-1 is disabled (without the network
    // abort, driving action UIs WOULD mutate the SUT).
    const R211_F_ACTION_DRIVE = process.env.ARTA_R211_F_ACTION_DRIVE === '1'
      && !R154_A_LAYER1_DISABLED;
    const _R211_F_ACTION_TOKENS: string[] = [
      'create', 'add', 'new', 'upload', 'import', 'save', 'submit', 'configure',
      'generate', 'register', 'connect', 'enable', 'launch', 'publish', 'apply',
    ];
    const _r211_f_is_action_label = (text: string): boolean => {
      if (!text) return false;
      const l = text.toLowerCase();
      return _R211_F_ACTION_TOKENS.some((t) => l.includes(t));
    };
    const _r211_f_fill_forms = async (pg: any): Promise<void> => {
      try {
        const inputs = await pg.locator('input:visible, textarea:visible').all().catch(() => []);
        for (const inp of (inputs || []).slice(0, 25)) {
          try {
            const type = ((await inp.getAttribute('type').catch(() => '')) || 'text').toLowerCase();
            if (['hidden', 'file', 'submit', 'button', 'checkbox', 'radio', 'image'].includes(type)) continue;
            const val = type === 'email' ? 'arta-test@example.com'
              : type === 'number' ? '1'
              : type === 'url' ? 'https://example.com'
              : 'arta-test';
            await inp.fill(val, { timeout: 1000 }).catch(() => {});
          } catch { /* per-input soft-fail */ }
        }
      } catch { /* no forms */ }
    };

    // Replace the for-of with an index-driven while loop so the BFS can
    // extend SPA_ROUTES during iteration (dynamic queue).
    let _r140_c_route_idx = 0;
    // R180 — track authenticated-gate outcomes so we can fail fast (and not
    // emit a login-wall catalog) when the SPA never authenticates.
    let _r180_auth_attempts = 0;
    let _r180_login_wall_count = 0;
    let _r180_authed_routes = 0;
    while (_r140_c_route_idx < SPA_ROUTES.length && _r140_c_route_idx < R140_TOTAL_ROUTE_CAP) {
      const route = SPA_ROUTES[_r140_c_route_idx];
      const _r140_c_depth = _depths.get(route) ?? 0;
      _r140_c_route_idx += 1;
      try {
        await page.goto(buildRouteUrl(route), { waitUntil: 'domcontentloaded', timeout: 15000 });
        await page.waitForLoadState('networkidle', { timeout: 12000 }).catch(() => {});
        // R87.1 — give the SPA an additional 2.5s on `/` so its auth
        // bootstrap (JWT decode + localStorage hydration + first XHR
        // burst) can complete BEFORE we walk deeper routes. Other routes
        // get the default networkidle wait above.
        if (route === '/') {
          await page.waitForTimeout(2500);
          // R219.E — auto-detect HashRouter: if the SPA parked itself on a
          // fragment route (e.g. "<base>/#/" or "#/portal") after loading the
          // root, switch subsequent route navigation to hash form so authed
          // views render for DOM snapshots. No-op when already set via config.
          if (!_r219_e_hash) {
            try {
              const _h = await page.evaluate(() => window.location.hash || '');
              if (_h.startsWith('#/')) {
                _r219_e_hash = true;
                // eslint-disable-next-line no-console
                console.log(`[probe] R219.E: HashRouter detected (location.hash="${_h}") — routing subsequent walks via <base>/#<route>`);
              }
            } catch { /* ignore */ }
          }
          // eslint-disable-next-line no-console
          console.log(`[probe] R87.1: bootstrap from / completed (SPA state initialized)`);
        }
        // R180 — AUTHENTICATED hydration gate: wait (up to ~22s, past the
        // SUT's ~9-10s auth bootstrap) for the page to be hydrated AND the
        // real authenticated app (NOT the login wall). When the page is still
        // the login wall / never authenticates, SKIP this route entirely
        // (continue) so the DOM catalog is never poisoned with login chrome
        // (the pre-R180 bug: catalog = 8 login elements across all routes →
        // every generated PW spec grounded on the wrong DOM).
        try {
          _r180_auth_attempts += 1;
          const _r180_status = await waitForSPAHydrationStrict();
          // R180 diagnostic — one-shot per route: where did we land + which
          // session keys are present? Distinguishes auth-TIMING (token present,
          // just slow) from MISSING-STATE (needs selected-project/app flow).
          try {
            const _r180_diag = await page.evaluate(() => {
              const k = (n: string) => { try { return !!localStorage.getItem(n); } catch { return false; } };
              const keys = (() => { try { return Object.keys(localStorage); } catch { return []; } })();
              return {
                path: location.pathname,
                sessionTok: k('session-token'), agent: k('agent-user-token'),
                selectedish: keys.filter((x) => /select|project|org|app/i.test(x)),
              };
            });
            // eslint-disable-next-line no-console
            // R267.B — append what the hydration gate ACTUALLY decided on.
            // so pre-R267.B this line explained nothing about a skip decision.
            console.log(`[probe] R180 diag route=${route} status=${_r180_status} landed=${_r180_diag.path} session=${_r180_diag.sessionTok} agent=${_r180_diag.agent} selection_keys=${JSON.stringify(_r180_diag.selectedish)} gate=[ready=${_r267_diag.ready} busy=${_r267_diag.busy} login=${_r267_diag.login} ctrls=${_r267_diag.ctrls} maxCtrls=${_r267_diag.maxCtrls} minCtrls=${process.env.ARTA_R267_MIN_CONTROLS || '10'}]`);
          } catch { /* diag best-effort */ }
          if (_r180_status === 'ok') {
            _r180_authed_routes += 1;
          } else {
            if (_r180_status === 'login_wall') _r180_login_wall_count += 1;
            // eslint-disable-next-line no-console
            console.log(`[probe] R180: ${route} not authenticated (${_r180_status}) after auth-bootstrap wait — SKIPPING DOM snapshot (keeps login chrome out of the catalog)`);
            continue;
          }
        } catch (_r150_h_exc) {
          // eslint-disable-next-line no-console
          console.warn(`[probe] R180: hydration check errored for ${route}: ${(_r150_h_exc as Error)?.message ?? _r150_h_exc} — skipping snapshot`);
          continue;
        }
        // R237 — dismiss an in-DOM confirm/blocking MODAL before the DOM snapshot.
        // page.on('dialog') only catches BROWSER-NATIVE dialogs, so the in-DOM
        // modal overlays the real content → the catalog captured only the modal's
        // 2 buttons (26/29 routes were "Yes/No" shells → starved PW/Axe/ZAP
        // grounding, the #1 selector-timeout cause on fresh specs). Dismiss it
        // READ-SIDE (a No/Cancel/Close/Dismiss control, then Escape — NEVER
        // Yes/Confirm/Delete/Save, per R154 non-mutation) so the content behind
        // renders. Best-effort; no-op when no modal. Killswitch
        // ARTA_R237_MODAL_DISMISS_DISABLE=1.
        if (process.env.ARTA_R237_MODAL_DISMISS_DISABLE !== '1') {
          try {
            const _dismissed = await page.evaluate(() => {
              const overlaySel = '[role="dialog"],[role="alertdialog"],.modal,.modal-dialog,'
                + '.ant-modal,.MuiDialog-root,[class*="modal"i],[class*="Modal"],[class*="dialog"i]';
              const overlay = Array.from(document.querySelectorAll(overlaySel))
                .find((el) => (el as HTMLElement).offsetParent !== null);
              if (!overlay) return false;
              const SAFE = /(^|\b)(no|cancel|close|dismiss|not now|later|go back|back)(\b|$)/i;
              const UNSAFE = /(yes|confirm|delete|remove|proceed|continue|submit|save|apply|ok\b)/i;
              const btns = Array.from(
                overlay.querySelectorAll('button,a,[role="button"]')) as HTMLElement[];
              const target = btns.find((b) => {
                const t = (b.textContent || b.getAttribute('aria-label') || '').trim();
                return !!t && SAFE.test(t) && !UNSAFE.test(t);
              });
              if (target) { target.click(); return true; }
              return false;
            });
            await page.keyboard.press('Escape').catch(() => {});
            if (_dismissed) {
              await page.waitForTimeout(700);
              // eslint-disable-next-line no-console
              console.log(`[probe] R237: dismissed in-DOM modal on ${route} (read-side) — re-capturing content behind it`);
            }
          } catch { /* best-effort — never block the snapshot */ }
        }
        // R19a — capture every [data-testid] + role landmark on the
        // page. The ATDD designer (R19c) reads this catalog and
        // constrains the LLM to ONLY emit selectors that actually exist
        // in the SUT. Without this, run-23aa57's 226 Playwright failures
        // were `getByTestId('dataset-recipe')` patterns the SUT never
        // exposed — the LLM hallucinated plausible-sounding ids from
        // AC text.
        try {
          const snapshot = await page.evaluate(() => {
            // R117.E — accessibility-tree-aware name extraction (TRUE
            // upstream fix for the smushed-name bug).
            //
            // Pre-R117.E the probe captured raw `el.textContent` which
            // for landmarks like `<main>` returned the ENTIRE subtree
            // AI engine to get startedEXTRACT..."). LLM faithfully
            // emitted `getByRole('main', { name: '<smushed>' })` →
            // R101.D rejected → R102.A stamped → Fix FF quarantined.
            //
            // R117.E priority for accessible name:
            //   1. aria-label attribute (explicit accessible-name)
            //   2. aria-labelledby chain (W3C naming algorithm)
            //   3. <label for> resolution for form controls
            //   4. title attribute
            //   5. DIRECT text children only (not subtree) for
            //      interactive roles (button/link/heading)
            //   6. Empty for landmark roles when no explicit name —
            //      landmarks without aria-label aren't useful
            //      selector targets; skip rather than smush textContent
            const LANDMARK_ROLES = new Set([
              'main', 'banner', 'complementary', 'navigation',
              'contentinfo', 'region',
            ]);
            const accessibleName = (el: Element): string => {
              // Tier 1: aria-label
              const ariaLabel = el.getAttribute('aria-label');
              if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim().slice(0, 60);
              // Tier 2: aria-labelledby (resolve chain)
              const labelledby = el.getAttribute('aria-labelledby');
              if (labelledby) {
                const refs = labelledby.split(/\s+/)
                  .map((id) => document.getElementById(id))
                  .filter(Boolean) as HTMLElement[];
                if (refs.length) {
                  const joined = refs.map((r) => (r.textContent || '').trim())
                    .filter(Boolean).join(' ').trim();
                  if (joined) return joined.slice(0, 60);
                }
              }
              // Tier 3: <label for> resolution for form controls
              const tagName = el.tagName.toLowerCase();
              if (tagName === 'input' || tagName === 'select' || tagName === 'textarea') {
                const id = el.getAttribute('id');
                if (id) {
                  const lbl = document.querySelector(`label[for="${id}"]`);
                  if (lbl && lbl.textContent && lbl.textContent.trim()) {
                    return lbl.textContent.trim().slice(0, 60);
                  }
                }
              }
              // Tier 4: title attribute
              const title = el.getAttribute('title');
              if (title && title.trim()) return title.trim().slice(0, 60);
              // Tier 5: direct text children ONLY for interactive
              // roles (button/link/heading). For landmark roles, return
              // empty — they shouldn't be selector targets via name.
              const role = (el.getAttribute('role') || tagName).toLowerCase();
              if (LANDMARK_ROLES.has(role)) {
                return '';   // Landmark without aria-label → skip
              }
              // Direct text node children only (skip nested element text)
              const directText = Array.from(el.childNodes)
                .filter((n) => n.nodeType === Node.TEXT_NODE)
                .map((n) => ((n as Text).textContent || '').trim())
                .filter(Boolean)
                .join(' ')
                .trim();
              if (directText) return directText.slice(0, 60);
              // Tier 6 fallback: textContent (last resort, but only if
              // length is short — i.e., a simple `<button>Submit</button>`
              // where the button has no children, textContent === Submit).
              // Cap aggressively at 60 to filter accidental subtree captures.
              const tc = (el.textContent || '').trim();
              if (tc.length <= 60) return tc;
              return '';
            };

            const out: any[] = [];
            document.querySelectorAll('[data-testid]').forEach((el) => {
              out.push({
                testid: el.getAttribute('data-testid'),
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || null,
                // R117.E — accessibility-tree-aware name
                text: accessibleName(el),
                visible: !!(el as HTMLElement).offsetParent,
              });
            });
            // Role landmarks (no data-testid) — captured separately so
            // the prompt can suggest getByRole/getByText fallbacks.
            document
              .querySelectorAll('main, nav, article, button[name], [role], button, a[href]')
              .forEach((el) => {
                if (el.hasAttribute('data-testid')) return;
                // R227 — record the IMPLICIT ARIA role, not the raw tag name.
                // A bare `<a href>` has role `link` (not `a`), `<select>` →
                // `combobox`, submit `<input>` → `button`, etc. Storing the raw
                // tag poisoned the DOM catalog → `getByRole('a', …)` in generated
                // specs (no such ARIA role; fails only at runtime). GENERIC.
                const _tag = el.tagName.toLowerCase();
                const _implicitRole = (): string => {
                  if (el.hasAttribute('role')) return el.getAttribute('role') || _tag;
                  const _m: Record<string, string> = {
                    a: 'link', button: 'button', select: 'combobox',
                    textarea: 'textbox', nav: 'navigation', img: 'img',
                    table: 'table', ul: 'list', ol: 'list', li: 'listitem',
                    form: 'form', h1: 'heading', h2: 'heading', h3: 'heading',
                    h4: 'heading', h5: 'heading', h6: 'heading',
                  };
                  if (_tag === 'a') return el.hasAttribute('href') ? 'link' : _tag;
                  if (_tag === 'input') {
                    const it = (el.getAttribute('type') || 'text').toLowerCase();
                    if (['submit', 'button', 'reset', 'image'].includes(it)) return 'button';
                    if (it === 'checkbox') return 'checkbox';
                    if (it === 'radio') return 'radio';
                    return 'textbox';
                  }
                  return _m[_tag] || _tag;
                };
                const role = _implicitRole();
                // R117.E — accessibility-tree-aware name (not raw subtree text)
                const text = accessibleName(el);
                if (!text && !el.getAttribute('aria-label') && !el.getAttribute('name')) return;
                out.push({
                  testid: null,
                  tag: el.tagName.toLowerCase(),
                  role,
                  text,
                  ariaLabel: el.getAttribute('aria-label') || null,
                  name: el.getAttribute('name') || null,
                });
              });
            return out;
          });
          // Slug the route for a filesystem-safe filename:
          //   /            → _root
          //   /dashboard   → _dashboard
          //   /a/b         → _a_b
          const slug = route === '/' ? '_root'
            : '_' + route.replace(/^\/+/, '').replace(/[^a-zA-Z0-9_-]/g, '_');
          const sidecarPath = path.join(HAR_DIR, `dom${slug}.json`);
          // Cap snapshot size to ~50KB by trimming text if needed.
          // R264.A — stamp the routing mode R219.E detected onto the sidecar.
          // Pre-R264 this fact lived ONLY in `_r219_e_hash` and died with the
          // probe process, so GEN never learned it and emitted plain deep-links
          // (`page.goto(BASE + '/portal')`) that a no-SPA-fallback SUT answers
          // with a hard 404 → 0 elements → every selector times out. The
          // sidecar is the existing probe→python channel; carry it there.
          const payload = JSON.stringify(
            { route, captured_at: new Date().toISOString(),
              spa_hash_routing: _r219_e_hash, elements: snapshot }, null, 2);
          // R212 — ensure the snapshot dir exists before writing. The DOM
          // snapshot was failing `ENOENT ...dom_root.json` (the dir wasn't
          // created), which ALSO cascades: a failed snapshot can break the
          // R140.C BFS link-extract → fewer routes walked → less JSON captured.
          try { fs.mkdirSync(HAR_DIR, { recursive: true }); } catch { /* exists */ }
          fs.writeFileSync(sidecarPath, payload.length > 50_000 ? payload.slice(0, 50_000) : payload);
          // eslint-disable-next-line no-console
          console.log(`[probe] DOM snapshot: ${sidecarPath} (${snapshot.length} elements)`);

          // R154.A — drive read-side clicks against catalog entries for
          // this route. Triple-defense (Layer 1+2+3) guarantees
          // non-mutation:
          //   Layer 1 (already installed at context creation) blocks
          //     POST/PUT/PATCH/DELETE at chromium network layer.
          //   Layer 2 (here): label allowlist (positive-match against
          //     read-intent verbs like "view", "show", "load", "refresh").
          //   Layer 3 (here): element-type guard (button/link only;
          //     no input, no checkbox/radio/textbox/etc.).
          // Browser-dialog auto-dismiss (installed at context creation)
          // declines any confirm/alert that mis-categorized clicks
          // accidentally surface. Max clicks per route bounded; safety
          // is enforced structurally by Layer 1+2+3, not by this cap.
          const R154_A_DISABLED = process.env.ARTA_R154_A_UI_INTERACTIONS_DISABLE === '1';
          const R154_A_MAX_CLICKS_PER_ROUTE = parseInt(
            process.env.ARTA_R154_A_MAX_CLICKS_PER_ROUTE ?? '5', 10,
          );
          if (!R154_A_DISABLED && R154_A_MAX_CLICKS_PER_ROUTE > 0 && snapshot.length > 0) {
            try {
              let _r154_a_clicked = 0;
              for (const entry of snapshot) {
                if (_r154_a_clicked >= R154_A_MAX_CLICKS_PER_ROUTE) break;
                // Layer 3 — element-type guard
                if (!_r154_a_is_safe_element(entry)) continue;
                const name = ((entry as any).text || (entry as any).ariaLabel || '').toString().trim();
                if (!name) continue;
                // Layer 2 — read-side label allowlist (positive match)
                if (!_r154_a_is_read_label(name)) continue;
                const role = (entry as any).role || 'button';
                const ok = await _r154_a_safe_click(page, role, name);
                if (ok) {
                  _r154_a_clicked += 1;
                  await _r154_a_wait_idle(page, 3000);
                }
              }
              if (_r154_a_clicked > 0) {
                // eslint-disable-next-line no-console
                console.log(
                  `[probe] R154.A: drove ${_r154_a_clicked} read-side click(s) on route ${route}`,
                );
              }
            } catch (_r154_a_exc) {
              // eslint-disable-next-line no-console
              console.warn(`[probe] R154.A click loop failed for ${route}: ${_r154_a_exc}`);
            }

            // R212 layer-3 — DATA-ITEM read-drive. The R154.A loop above only
            // clicks READ-LABELED controls (view/show/load); but on data pages
            // (collections/datasets/files) the data XHRs fire when you click a
            // DATA ITEM (a collection/dataset/file row or card — which carries a
            // NAME, not a "view" label), not on navigation. Proven: clicking the
            // first few visible rows/cards on /workspace/{ws}/project/{proj}/
            // collections fired the cm data GETs (JSON 0→2) that the R212
            // response-capture then records. R154-SAFE: Layer-1 network guard
            // aborts any non-GET a click might trigger, and browser dialogs
            // auto-dismiss; bounded count. Killswitch ARTA_R212_DATA_CLICK_DISABLE=1.
            if (process.env.ARTA_R212_DATA_CLICK_DISABLE !== '1') {
              try {
                const _r212_dc = await page.evaluate(() => {
                  const DESTRUCTIVE = /delete|remove|trash|purge|reset|wipe|log\s*out|sign\s*out|revoke|disable|deactivate/i;
                  const sel = 'tr,[role="row"],[role="listitem"],[class*="card"],[class*="row"],[class*="item"],[class*="list"] a';
                  const cands = Array.from(document.querySelectorAll(sel)) as HTMLElement[];
                  const vis = cands.filter((e) => e.offsetParent !== null
                    && (e.innerText || '').trim().length > 1
                    && !DESTRUCTIVE.test(e.innerText || '')
                    && e.tagName.toLowerCase() !== 'input');
                  let n = 0;
                  for (const e of vis.slice(0, 4)) { try { e.click(); n += 1; } catch { /* skip */ } }
                  return n;
                });
                if (_r212_dc > 0) {
                  await page.waitForLoadState('networkidle', { timeout: 6000 }).catch(() => {});
                  await page.waitForTimeout(1500);
                  // eslint-disable-next-line no-console
                  console.log(`[probe] R212: data-item read-drive clicked ${_r212_dc} item(s) on ${route} (cm data XHRs → response-capture)`);
                }
              } catch (_r212_dc_exc) {
                // eslint-disable-next-line no-console
                console.warn(`[probe] R212 data-click failed for ${route}: ${_r212_dc_exc}`);
              }
            }

            // R211 Phase F.2 — GATED action-drive: open action forms, fill them,
            // submit → the SPA builds the request body → Layer-1 captures it at
            // abort (no mutation). Opt-in + fail-closed (see helper defn).
            if (R211_F_ACTION_DRIVE && snapshot.length > 0) {
              try {
                let _driven = 0;
                const _R211_F_MAX = parseInt(
                  process.env.ARTA_R211_F_MAX_ACTIONS_PER_ROUTE ?? '3', 10);
                for (const entry of snapshot) {
                  if (_driven >= _R211_F_MAX) break;
                  if (!_r154_a_is_safe_element(entry)) continue;
                  const name = ((entry as any).text || (entry as any).ariaLabel || '').toString().trim();
                  if (!name || !_r211_f_is_action_label(name)) continue;
                  const role = (entry as any).role || 'button';
                  // 1) click the action (often opens a create/upload form/modal)
                  const opened = await _r154_a_safe_click(page, role, name);
                  if (!opened) continue;
                  _driven += 1;
                  await _r154_a_wait_idle(page, 1500);
                  // 2) fill the revealed form so the request body is populated
                  await _r211_f_fill_forms(page);
                  // 3) submit → triggers the POST (captured at abort)
                  for (const sub of ['Save', 'Submit', 'Create', 'Confirm', 'Apply', 'Add', 'Upload']) {
                    if (await _r154_a_safe_click(page, 'button', sub)) {
                      await _r154_a_wait_idle(page, 1500);
                      break;
                    }
                  }
                }
                if (_driven > 0) {
                  // eslint-disable-next-line no-console
                  console.log(
                    `[probe] R211 Phase F.2: drove ${_driven} action flow(s) on route ${route} ` +
                    `(bodies captured at abort — SUT NOT mutated)`,
                  );
                }
              } catch (_r211_f_exc) {
                // eslint-disable-next-line no-console
                console.warn(`[probe] R211 F.2 action-drive failed for ${route}: ${_r211_f_exc}`);
              }
            }
            // R239 — POST-CLICK RE-CAPTURE (catalog DEPTH). The pre-click snapshot
            // (written above) has only the top-level route's chrome; a SPA's real
            // nav item, so the R154.A read-side clicks above reveal deeper content
            // that was never cataloged → the catalog stayed shell-thin (only 3
            // real routes; the #1 PW selector-timeout cause on fresh specs).
            // Re-capture the DOM now (compact role+name+testid), MERGE the NEW
            // elements into the sidecar (deduped vs the pre-click snapshot), and
            // re-write it with the union. Killswitch
            // ARTA_R239_POSTCLICK_RECAPTURE_DISABLE=1.
            if (process.env.ARTA_R239_POSTCLICK_RECAPTURE_DISABLE !== '1') {
              try {
                const _r239_new = await page.evaluate(() => {
                  const implicitRole = (el: Element): string => {
                    const explicit = el.getAttribute('role');
                    if (explicit) return explicit;
                    const t = el.tagName.toLowerCase();
                    if (t === 'a') return (el as HTMLAnchorElement).hasAttribute('href') ? 'link' : t;
                    if (t === 'button') return 'button';
                    if (t === 'select') return 'combobox';
                    if (t === 'textarea') return 'textbox';
                    if (t === 'input') {
                      const it = ((el.getAttribute('type') || 'text')).toLowerCase();
                      return ['submit', 'button', 'reset', 'image'].includes(it) ? 'button'
                        : it === 'checkbox' ? 'checkbox' : it === 'radio' ? 'radio' : 'textbox';
                    }
                    const m: Record<string, string> = {
                      nav: 'navigation', img: 'img', table: 'table', ul: 'list', ol: 'list',
                      li: 'listitem', form: 'form', h1: 'heading', h2: 'heading', h3: 'heading',
                      h4: 'heading', h5: 'heading', h6: 'heading',
                    };
                    return m[t] || t;
                  };
                  const out: any[] = [];
                  const els = Array.from(document.querySelectorAll(
                    '[data-testid],button,a[href],[role],input,select,textarea,h1,h2,h3,table'));
                  for (const el of els) {
                    if ((el as HTMLElement).offsetParent === null) continue; // visible only
                    const testid = el.getAttribute('data-testid');
                    const name = (el.getAttribute('aria-label')
                      || (el as HTMLElement).innerText || el.getAttribute('name') || '')
                      .trim().replace(/\s+/g, ' ').slice(0, 60);
                    if (!testid && !name) continue;
                    out.push({
                      testid: testid || null, tag: el.tagName.toLowerCase(),
                      role: implicitRole(el), text: name,
                      ariaLabel: el.getAttribute('aria-label') || null,
                    });
                    if (out.length >= 120) break;
                  }
                  return out;
                });
                const _keyOf = (e: any) => `${e.role || e.tag}|${e.testid || ''}|${(e.text || e.ariaLabel || '')}`;
                const _seenKey = new Set((snapshot as any[]).map(_keyOf));
                const _merged = [...(snapshot as any[])];
                let _added = 0;
                for (const e of _r239_new) {
                  const k = _keyOf(e);
                  if (!_seenKey.has(k)) { _seenKey.add(k); _merged.push(e); _added += 1; }
                }
                if (_added > 0) {
                  const _p2 = JSON.stringify(
                    { route, captured_at: new Date().toISOString(),
                      spa_hash_routing: _r219_e_hash, elements: _merged }, null, 2);
                  fs.writeFileSync(sidecarPath, _p2.length > 50_000 ? _p2.slice(0, 50_000) : _p2);
                  // eslint-disable-next-line no-console
                  console.log(`[probe] R239: post-click re-capture added ${_added} deeper element(s) on ${route} (${(snapshot as any[]).length}→${_merged.length})`);
                }
              } catch (_r239_exc) {
                // eslint-disable-next-line no-console
                console.warn(`[probe] R239 post-click re-capture failed for ${route}: ${(_r239_exc as Error)?.message ?? _r239_exc}`);
              }
            }
          }
        } catch (e) {
          // eslint-disable-next-line no-console
          console.warn(`[probe] DOM snapshot failed for ${route}: ${e}`);
        }

        // Asset Management, …) behind a "Toggle navigation" / hamburger
        // button; the feature links aren't rendered (or aren't the
        // visible top-bar `a[href]`) until the menu is expanded. R154.A's
        // 5-click budget is spent on the first read-safe elements and may
        // never reach the toggle, so link-extraction below sees only the
        // top-bar chrome → shallow catalog (dashboard shell, no features).
        // Deterministically click nav-expanders here so R140.C sees the
        // now-rendered feature links. Read-safe: Layer-1 network guard is
        // still active (any XHR a menu-expand triggers is GET/aborted),
        // and we only click button/link elements matching expander labels.
        // Killswitch: ARTA_R241_NAV_EXPAND_DISABLE=1.
        if (process.env.ARTA_R241_NAV_EXPAND_DISABLE !== '1'
            && R140_BFS_DEPTH > 0 && _r140_c_depth < R140_BFS_DEPTH
            && SPA_ROUTES.length < R140_TOTAL_ROUTE_CAP) {
          try {
            const _r241_expander_re = /(toggle\s*nav|navbar[- ]?toggl|hamburger|main\s*menu|open\s*menu|show\s*menu|expand\s*menu|menu\s*toggl|side\s*menu|primary\s*nav|nav\s*menu|open\s*navigation|show\s*navigation)/i;
            const _r241_cands = (snapshot as any[]).filter((e) => {
              if (!_r154_a_is_safe_element(e)) return false;
              const lbl = ((e.text || e.ariaLabel || '') as string);
              return _r241_expander_re.test(lbl);
            }).slice(0, 3);
            let _r241_expanded = 0;
            for (const e of _r241_cands) {
              const nm = ((e.text || e.ariaLabel || '') as string).trim();
              if (!nm) continue;
              const ok = await _r154_a_safe_click(page, (e.role || 'button'), nm);
              if (ok) { _r241_expanded += 1; await _r154_a_wait_idle(page, 1500); }
            }
            if (_r241_expanded > 0) {
              // eslint-disable-next-line no-console
              console.log(
                `[probe] R241: expanded ${_r241_expanded} nav-menu(s) on ${route} before link-extract`,
              );
            }
          } catch (_r241_exc) {
            // eslint-disable-next-line no-console
            console.warn(`[probe] R241 nav-expand failed for ${route}: ${(_r241_exc as Error)?.message ?? _r241_exc}`);
          }
        }

        // R140.C — extract a[href] links from this route + queue
        // depth-N+1 routes. Skip when at depth-cap or total-cap reached.
        if (R140_BFS_DEPTH > 0 && _r140_c_depth < R140_BFS_DEPTH
            && SPA_ROUTES.length < R140_TOTAL_ROUTE_CAP) {
          try {
            const _r140_c_links: string[] = await page.evaluate((cap: number) => {
              const out: string[] = [];
              const anchors = Array.from(document.querySelectorAll('a[href]')).slice(0, cap);
              for (const a of anchors) {
                let href = (a as HTMLAnchorElement).getAttribute('href') || '';
                if (!href || !href.startsWith('/')) continue;       // relative-only
                if (href.startsWith('//')) continue;                // protocol-relative
                if (/^(mailto|tel|javascript|data):/i.test(href)) continue;  // schemes
                if (/\.(css|js|woff2?|png|jpg|jpeg|gif|svg|ico|map|webp)$/i.test(href)) continue;
                // Strip #hash + ?query (different fragments → same route)
                href = href.split('#')[0].split('?')[0];
                if (!href) continue;
                out.push(href);
              }
              return out;
            }, R140_LINKS_PER_PAGE_CAP);
            let _r140_c_added = 0;
            for (const link of _r140_c_links) {
              if (SPA_ROUTES.length >= R140_TOTAL_ROUTE_CAP) break;
              if (_seen.has(link)) continue;
              // R150.L — BFS skip check: never enqueue routes the operator
              // marked as crash-prone via ARTA_R150_SKIP_ROUTES. Even when
              // a sibling page links to /accounts, we won't follow it.
              // R152.A — prefix-aware skip predicate (was R150_L_SKIP_SET.has)
              if (_r150_l_should_skip(link)) continue;
              _seen.add(link);
              _depths.set(link, _r140_c_depth + 1);
              SPA_ROUTES.push(link);
              _r140_c_added += 1;
            }
            if (_r140_c_added > 0) {
              // eslint-disable-next-line no-console
              console.log(
                `[probe] R140.C BFS: route=${route} depth=${_r140_c_depth} ` +
                `added=${_r140_c_added} (total_queue=${SPA_ROUTES.length})`,
              );
            }
          } catch (e) {
            // eslint-disable-next-line no-console
            console.warn(`[probe] R140.C BFS link-extract failed for ${route}: ${e}`);
          }
        }
      } catch {
        // Soft-fail — the failed nav still lands in the HAR.
      }
    }

    // R180 — fail fast when the probe NEVER reached the authenticated app on
    // any route (every route stayed on the login wall despite a valid API
    // cookie at R37.5 pre-flight — a SPA *client* auth-guard issue the API
    // pre-flight can't see). Emitting a login-only catalog would ground every
    // generated PW spec on the wrong DOM, so write auth_failed.flag and let
    // the orchestrator surface a refresh-auth CTA instead. (Killswitch:
    // ARTA_R180_AUTH_GATE_DISABLE=1 keeps legacy "snapshot whatever rendered".)
    if (process.env.ARTA_R180_AUTH_GATE_DISABLE !== '1'
        && _r180_auth_attempts > 0 && _r180_authed_routes === 0) {
      try {
        const flagPath = path.join(HAR_DIR_FOR_FLAG, 'auth_failed.flag');
        fs.writeFileSync(flagPath, JSON.stringify({
          reason: 'spa_client_login_wall',
          detail: 'SPA rendered the login wall on every route despite a valid API cookie; '
            + 'the client auth-guard did not authenticate (likely needs more session state '
            + 'or did not resolve before snapshot). DOM catalog would be login-only.',
          routes_attempted: _r180_auth_attempts,
          login_wall_routes: _r180_login_wall_count,
          authenticated_routes: 0,
          checked_at: new Date().toISOString(),
        }, null, 2));
        // eslint-disable-next-line no-console
        console.warn(
          `[probe] R180: 0/${_r180_auth_attempts} routes authenticated (login_wall=${_r180_login_wall_count}). `
          + `Wrote ${flagPath} — refusing to emit a login-only DOM catalog. Operator must refresh auth / app-state.`,
        );
      } catch (_r180_flag_exc) {
        // eslint-disable-next-line no-console
        console.warn(`[probe] R180: failed to write auth_failed.flag: ${_r180_flag_exc}`);
      }
    } else if (_r180_auth_attempts > 0) {
      // eslint-disable-next-line no-console
      console.log(`[probe] R180: authenticated DOM captured on ${_r180_authed_routes}/${_r180_auth_attempts} route(s) (login_wall skipped=${_r180_login_wall_count}).`);
    }

    // Step 2: hit canonical API endpoints. Use API_BASE_URL (backend host)
    // returns index.html for /api/* paths. Follow detail URLs using
    // operator-seeded IDs (TARGET_ENV_<KEY>) when available, falling back
    // to first-item-id from the list response.
    const ctxRequest = context.request;

    // R151.B — merge dynamic analytics endpoints from captured store with
    // hardcoded API_PROBES. Direct authenticated XHR per endpoint → JSON
    // body captured by R150.A `mode:'full'` HAR → response_body_shape
    // ingest by sut_onboarding._infer_shape. Cap=30 dynamic + 12 hardcoded.
    const _r151b_projectId = process.env.TARGET_PROJECT_ID || process.env.ARTA_PROJECT_ID;
    const _r151b_dynamic = loadCapturedEndpointPaths(_r151b_projectId);
    const _r151b_hardcodedSet = new Set(API_PROBES.map(([pp]) => pp));
    const _r151b_merged = API_PROBES.concat(
      _r151b_dynamic.filter(([pp]) => !_r151b_hardcodedSet.has(pp)),
    );
    // eslint-disable-next-line no-console
    console.log(
      `[probe] R151.B: API_PROBES hardcoded=${API_PROBES.length} ` +
      `dynamic=${_r151b_dynamic.length} merged=${_r151b_merged.length}`,
    );

    for (const [p, idVarName] of _r151b_merged) {
      try {
        const listUrl = API_BASE_URL + p;
        const resp = await ctxRequest.get(listUrl, { timeout: 6000 });
        if (resp.ok()) {
          const ct = resp.headers()['content-type'] || '';
          if (ct.includes('application/json')) {
            try {
              const body = await resp.json();
              const items = Array.isArray(body)
                ? body
                : (body?.items || body?.data || body?.results);
              const firstItemId = Array.isArray(items) && items.length > 0
                ? (items[0]?.id || items[0]?.uuid || items[0]?.slug)
                : undefined;
              const idForDetail = (idVarName && seededId(idVarName)) || firstItemId;
              if (idForDetail && typeof idForDetail === 'string') {
                await ctxRequest.get(`${API_BASE_URL}${p}/${idForDetail}`, { timeout: 6000 })
                  .catch(() => {});
              }
            } catch { /* not JSON — skip */ }
          }
        }
      } catch { /* keep probing */ }
    }
  } finally {
    // R154.A Layer 1 summary — emit before context.close so operators see
    // how many destructive requests Layer 1 blocked at the network layer
    // during this probe run. Non-zero count means: somewhere in the
    // BFS, a click slipped past Layer 2/3 + triggered a non-read XHR
    // that Layer 1 caught. Operator-visible: indicates Layer 1's
    // structural guarantee prevented a mutation that Layer 2/3 missed.
    if (_r154_a_blocked_count > 0) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R154.A Layer 1 SUMMARY: blocked ${_r154_a_blocked_count} ` +
        `non-read request(s) at network layer. Samples (first 8): ` +
        `${_r154_a_blocked_samples.join('; ')}`,
      );
    } else if (!R154_A_LAYER1_DISABLED) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R154.A Layer 1 SUMMARY: 0 non-read requests attempted ` +
        `(Layer 2/3 prevented all mutations preemptively)`,
      );
    }
    // R265 — operator-visible: how many auth-refreshes were answered locally.
    // Non-zero means the SPA asked to refresh and ARTA satisfied it WITHOUT the
    // SUT ever receiving the request (and without consuming the rotating on-disk
    // refresh token). Zero WITH a matcher configured means the SPA never asked --
    // check the matcher against the real refresh URL before assuming success.
    if (_r265_match) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R265 SUMMARY: fulfilled ${_r265_fulfilled} auth-refresh request(s) ` +
        `locally (match=${_r265_match}); SUT received none of them`,
      );
    }
    // R266 — operator-visible: exactly which non-GET requests ARTA let reach the
    // SUT, and how many. This is the ONLY path by which a non-GET reaches the SUT
    // during discovery, so it must always be printed when the allowlist is set.
    if (_r266_allow.length) {
      // eslint-disable-next-line no-console
      console.log(
        `[probe] R266 SUMMARY: allowed ${_r266_allowed} operator-named read-POST(s) ` +
        `to reach the SUT across ${_r266_allowed_paths.size} distinct path(s): ` +
        `${[..._r266_allowed_paths].join(', ') || '(none matched — check the allowlist paths)'}`,
      );
    }
    // R211 Phase F — persist the captured would-be request bodies (R154-safe;
    // the SUT never received them) so api_discovery can infer
    // request_body_shape for action endpoints → Phase C body synthesis.
    try {
      const _fpid = process.env.TARGET_PROJECT_ID || process.env.ARTA_PROJECT_ID || '';
      if (_r211_f_capture && _fpid && _r211_f_bodies.length > 0) {
        const _bdir = path.resolve(__dirname, '../../../.arta/discovered_request_bodies');
        fs.mkdirSync(_bdir, { recursive: true });
        fs.writeFileSync(
          path.join(_bdir, `${_fpid}.json`),
          JSON.stringify(_r211_f_bodies, null, 2),
        );
        // eslint-disable-next-line no-console
        console.log(
          `[probe] R211 Phase F: captured ${_r211_f_bodies.length} would-be ` +
          `request body(ies) (R154-safe, aborted) → discovered_request_bodies/${_fpid}.json`,
        );
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn(`[probe] R211 Phase F body-capture write failed: ${e}`);
    }
    // R212 — flush the captured GET response shapes for the ingest/recipe to use.
    try {
      const _rpid = process.env.TARGET_PROJECT_ID || process.env.ARTA_PROJECT_ID || '';
      if (_r212_resp_capture && _rpid && _r212_resp_shapes.length > 0) {
        const _rdir = path.resolve(__dirname, '../../../.arta/discovered_response_shapes');
        fs.mkdirSync(_rdir, { recursive: true });
        fs.writeFileSync(
          path.join(_rdir, `${_rpid}.json`),
          JSON.stringify(_r212_resp_shapes, null, 2),
        );
        // eslint-disable-next-line no-console
        console.log(
          `[probe] R212: captured ${_r212_resp_shapes.length} GET response shape(s) ` +
          `→ discovered_response_shapes/${_rpid}.json`,
        );
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn(`[probe] R212 response-shape write failed: ${e}`);
    }
    // CRUCIAL: context.close() flushes the HAR to disk.
    await context.close();
    await browser.close();
  }

  // Sanity assertion — the probe's contract is "ran without crashing".
  // The harvest pipeline is what verifies the HAR is non-empty.
  expect(true).toBe(true);
});
