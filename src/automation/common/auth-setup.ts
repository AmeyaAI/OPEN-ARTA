/**
 * ARTA Platform — Generic Auth Setup
 *
 * Playwright global setup that works with ANY auth method by reading
 * from environment variables. Supports cookie, bearer, basic, and
 * localStorage injection. Multi-role support via TARGET_AUTH_ROLES.
 */
import { chromium } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';
import * as crypto from 'crypto';
import * as dns from 'dns';
import { promisify } from 'util';

// R145.E Layer 2 — independent DNS pre-resolution via Node.js so chromium
// can use the resolved IP even when TARGET_CHROMIUM_HOST_RESOLVER_RULES is
// absent (env var dropped between dispatcher + subprocess) OR when arta-api
// detected asymmetry but the env propagation chain breaks. Node.js uses
// Docker's /etc/resolv.conf — SAME as arta-api — so the result IP is the
// SAME path that arta-api proved works.
const _r145_e_resolve = promisify(dns.lookup);
async function _r145_e_derive_resolver_rule(): Promise<string | null> {
  if (process.env.ARTA_R145_E_LAYER2_DNS_DISABLE === '1') return null;
  try {
    const base = process.env.TARGET_BASE_URL;
    if (!base) return null;
    const host = new URL(base).hostname;
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) return null;   // already an IP
    const { address } = await _r145_e_resolve(host);
    if (!address) return null;
    return `MAP ${host}:443 ${address}:443,MAP ${host}:80 ${address}:80`;
  } catch {
    return null;
  }
}

const BASE_URL = process.env.TARGET_BASE_URL || 'http://localhost:3000';
const AUTH_METHOD = process.env.TARGET_AUTH_METHOD || 'none';
const AUTH_STATE_PATH = process.env.TARGET_AUTH_STATE_PATH || path.resolve(__dirname, '../../.arta/auth-state.json');

// Cookie auth
const COOKIE_NAME = process.env.TARGET_AUTH_COOKIE_NAME || '';
const COOKIE_VALUE = process.env.TARGET_AUTH_COOKIE_VALUE || '';

// Bearer auth
const BEARER_TOKEN = process.env.TARGET_AUTH_BEARER_TOKEN || '';

// Basic auth
const BASIC_USERNAME = process.env.TARGET_AUTH_USERNAME || '';
const BASIC_PASSWORD = process.env.TARGET_AUTH_PASSWORD || '';

// LocalStorage injection
const LOCAL_STORAGE = process.env.TARGET_AUTH_LOCALSTORAGE || '{}';

// Multi-role support
const ROLES_JSON = process.env.TARGET_AUTH_ROLES || '';

async function globalSetup() {
  const authDir = path.dirname(AUTH_STATE_PATH);
  if (!fs.existsSync(authDir)) fs.mkdirSync(authDir, { recursive: true });

  // R145.E Layer 2 — DNS pre-resolution. When dispatcher-side R143.D.2
  // did NOT set TARGET_CHROMIUM_HOST_RESOLVER_RULES (could happen if
  // R143.D preflight skipped OR env propagation broke), independently
  // resolve the SUT hostname via Node.js DNS + inject the rule. Chromium
  // then bypasses its own broken DNS via the resolved IP that arta-api's
  // network namespace proves works.
  if (!process.env.TARGET_CHROMIUM_HOST_RESOLVER_RULES) {
    const _r145_e_rule = await _r145_e_derive_resolver_rule();
    if (_r145_e_rule) {
      process.env.TARGET_CHROMIUM_HOST_RESOLVER_RULES = _r145_e_rule;
      console.log(`[R145.E Layer2] derived chromium resolver rule via Node DNS: ${_r145_e_rule}`);
    }
  } else {
    console.log(`[R145.E Layer2] dispatcher-side resolver rule already present (Layer 1): ${process.env.TARGET_CHROMIUM_HOST_RESOLVER_RULES.slice(0, 80)}`);
  }

  // R112.A — hash-based cache invalidation. Pre-R112.A the cache returned
  // → cookie + agent_token refreshed in env → BUT the storage state file
  // kept its STALE cookies → page.goto loaded login page → every PW spec
  // dependent on authenticated SUT state failed with TimeoutError or
  // selector-not-found (run-d7cc3b TC-AM-001 evidence: 10s waitForResponse
  // timeout because page was on login screen). R112.A computes a hash
  // of the CURRENT auth creds + compares against a sidecar meta file. On
  // mismatch, rebuild the storage state with the fresh creds.
  const COOKIE_HASH = crypto
    .createHash('sha256')
    .update(`${COOKIE_VALUE}|${BEARER_TOKEN}|${BASIC_USERNAME}|${LOCAL_STORAGE}`)
    .digest('hex')
    .slice(0, 16);
  const META_PATH = `${AUTH_STATE_PATH}.r112a.meta.json`;

  let metaCache: { cookie_hash?: string; built_at?: string; source?: string } = {};
  try {
    if (fs.existsSync(META_PATH)) {
      metaCache = JSON.parse(fs.readFileSync(META_PATH, 'utf-8'));
    }
  } catch { /* fallthrough — treat as cache miss */ }

  // R112.A.5 KEYSTONE — paste-trust override. When projects.py R45.2 paste
  // handler wrote the storage state file, it ALSO writes the meta sidecar
  // with source=r45_2_paste. This is a unilateral declaration: "operator
  // just provided fresh creds via UI — trust this file regardless of
  // subprocess env hash mismatch." Without this branch, R112.A's hash
  // comparison required the subprocess to have TARGET_AUTH_COOKIE_VALUE
  // matching exactly what projects.py wrote — but the R45.3 discovery
  // probe subprocess often runs with a stale or empty env relative to
  // the just-pasted file → cache MISS → chromium launches → about:blank
  // fallback → file wiped (run-25aabd evidence).
  if (
    fs.existsSync(AUTH_STATE_PATH)
    && metaCache.source
    && typeof (metaCache as any).source === 'string'
    && ((metaCache as any).source as string).startsWith('r45_2_paste')
  ) {
    try {
      const existing = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      const cookieCount = Array.isArray(existing.cookies) ? existing.cookies.length : 0;
      const originsCount = Array.isArray(existing.origins) ? existing.origins.length : 0;
      if (cookieCount > 0 || originsCount > 0) {
        console.log(
          `[ARTA Auth R112.A.5] Honoring paste-trust meta from projects.py R45.2 ` +
          `(source=${(metaCache as any).source}, built_at=${metaCache.built_at || '?'}, ` +
          `${cookieCount} cookies, ${originsCount} origins). Skipping chromium ` +
          `rebuild to preserve operator's paste.`
        );
        return;
      }
    } catch { /* fall through */ }
  }

  // Part 6D + R112.A: if storage state exists AND the cookie_hash matches
  // the current env, the cache is fresh — reuse it.
  if (fs.existsSync(AUTH_STATE_PATH) && metaCache.cookie_hash === COOKIE_HASH) {
    try {
      const existing = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      if (Array.isArray(existing.cookies) && Array.isArray(existing.origins)) {
        console.log(
          `[ARTA Auth R112.A] Using cached storage state ` +
          `(${existing.cookies.length} cookies, hash=${COOKIE_HASH}, built_at=${metaCache.built_at || '?'})`
        );
        return;
      }
    } catch {
      // Fall through to rebuild
    }
  } else if (fs.existsSync(AUTH_STATE_PATH)) {
    console.log(
      `[ARTA Auth R112.A] Cache MISS — cookie hash changed ` +
      `(expected=${COOKIE_HASH}, cached=${metaCache.cookie_hash || 'none'}); rebuilding storage state`
    );
  }

  if (AUTH_METHOD === 'none') {
    // R185.B KEYSTONE — NEVER overwrite a populated session with empty here.
    // AUTH_METHOD resolves to 'none' whenever no cookie/bearer env is
    // projects.json carries a placeholder cookie '***' and keeps the REAL
    // session only in the storage-state FILE. Pre-R185.B this branch ran
    // FIRST (before R112.A.4 / R181.C) and wrote {cookies:[],origins:[]} →
    // the discovery probe (and any tool whose env cookie is the placeholder)
    // read an EMPTY file → SPA login wall → empty DOM catalog → gen
    // hallucinated selectors. If a populated session already exists on disk,
    // PRESERVE it (the file IS the auth) instead of wiping. Killswitch
    // ARTA_R185_B_PRESERVE_ON_NONE=0.
    if (process.env.ARTA_R185_B_PRESERVE_ON_NONE !== '0' && fs.existsSync(AUTH_STATE_PATH)) {
      try {
        const _ex = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
        const _ck = Array.isArray(_ex.cookies) ? _ex.cookies.length : 0;
        const _ls = Array.isArray(_ex.origins)
          ? _ex.origins.reduce((n: number, o: any) => n + ((o?.localStorage || []).length), 0) : 0;
        if (_ck > 0 && _ls > 0) {
          console.log(
            `[ARTA Auth R185.B] AUTH_METHOD=none but on-disk storage state is a `
            + `full session (${_ck} cookie(s), ${_ls} localStorage key(s)) — `
            + `PRESERVING it (the file is the auth) instead of wiping to empty.`,
          );
          return;
        }
      } catch { /* unparseable — fall through to the legacy empty-write */ }
    }
    console.log('[ARTA Auth] No auth configured — running tests without authentication');
    fs.writeFileSync(AUTH_STATE_PATH, JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  // R112.A.2 — guard against destroying the operator's R45.2-pasted storage
  // state when COOKIE_VALUE env var is empty at auth-setup time. Pre-R112.A.2:
  // when the rebuild path fires (R112.A cache MISS) but COOKIE_VALUE was not
  // propagated to this subprocess, createAuthState produced 0 cookies and
  // overwrote the existing valid storage state with empty {cookies:[], origins:[]}.
  // R112.A.2: when no fresh creds available, PRESERVE the existing state
  // (operator's paste survives). Only rebuild when we have real creds to use.
  // R181 — a placeholder credential ('***', 'REPLACE_ME', all-asterisks) is NOT
  // a real cred. Pre-R181 a placeholder COOKIE_VALUE='***' (from the project
  // HAS_FRESH_CREDS=true but never matched the real cookie → R112.A.4's
  // preserve-guard was skipped → createAuthState rebuilt → about:blank wipe
  // destroyed the operator's pasted session. Treating placeholders as absent
  // routes to the R112.A.2 preserve path instead.
  const _r181_isPlaceholder = (v: string | undefined): boolean =>
    !v || v === 'REPLACE_ME' || /^\*+$/.test(v) || v.startsWith('REPLACE');
  const _COOKIE_REAL = _r181_isPlaceholder(COOKIE_VALUE) ? '' : COOKIE_VALUE;
  const _BEARER_REAL = _r181_isPlaceholder(BEARER_TOKEN) ? '' : BEARER_TOKEN;
  const HAS_FRESH_CREDS = (
    (AUTH_METHOD === 'cookie' && COOKIE_NAME && _COOKIE_REAL)
    || (AUTH_METHOD === 'bearer' && _BEARER_REAL)
    || (AUTH_METHOD === 'basic' && BASIC_USERNAME && BASIC_PASSWORD)
  );
  if (!HAS_FRESH_CREDS && fs.existsSync(AUTH_STATE_PATH)) {
    try {
      const existing = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      const hasCookies = Array.isArray(existing.cookies) && existing.cookies.length > 0;
      const hasOriginCreds = Array.isArray(existing.origins)
        && existing.origins.some((o: any) => (o.localStorage || []).length > 0);
      if (hasCookies || hasOriginCreds) {
        console.log(
          `[ARTA Auth R112.A.2] No fresh creds in env, preserving existing `
          + `storage state (${existing.cookies.length} cookies, `
          + `${existing.origins.length} origins). Cache meta WON'T update.`
        );
        return;
      }
    } catch { /* fall through to rebuild */ }
  }

  // R112.A.4 — KEYSTONE — when the existing storage state already contains
  // the SAME cookie value as the env var (operator just pasted), DO NOT
  // rebuild. Chromium can't reach the SUT from inside the container, so
  // the rebuild produces an empty storage state via the about:blank
  // fallback at line ~163 (Playwright's direct context.storageState({path})
  // bypasses my save-time guard). The rebuild's empty output destroys
  // the operator's freshly-pasted cookie. R112.A.4 catches this BEFORE
  // chromium launches.
  //
  // Live evidence (run-25aabd retry): R45.2 paste wrote cookie at
  // 01:25:04 → discovery probe at 01:25:04-19 wiped it via createAuthState
  // → storage state ends with 0 cookies → L3 auth pre-flight skips dispatch.
  if (HAS_FRESH_CREDS && fs.existsSync(AUTH_STATE_PATH)) {
    try {
      const existing = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      const cookies = Array.isArray(existing.cookies) ? existing.cookies : [];
      const matchingCookie = cookies.find(
        (c: any) => c.name === COOKIE_NAME && c.value === COOKIE_VALUE,
      );
      if (matchingCookie) {
        console.log(
          `[ARTA Auth R112.A.4] Existing storage state already has matching `
          + `cookie ${COOKIE_NAME}=<${COOKIE_VALUE.length} chars>. Skipping rebuild `
          + `to preserve operator's paste. (${cookies.length} cookies total)`
        );
        // Update meta cache so subsequent runs use the early-return path
        try {
          fs.writeFileSync(
            META_PATH,
            JSON.stringify({
              cookie_hash: COOKIE_HASH,
              built_at: new Date().toISOString(),
              source: 'r112_a_4_preserve',
            }, null, 2),
          );
        } catch (e) {
          console.warn(`[ARTA Auth R112.A.4] meta write failed: ${e}`);
        }
        return;
      }
    } catch { /* fall through to rebuild */ }
  }

  // R181.C KEYSTONE — if the on-disk storage state is ALREADY a full populated
  // session (>=1 cookie AND >=1 localStorage key), SKIP the rebuild entirely.
  // The rebuild (createAuthState → navigate to SUT → about:blank fallback) can
  // ONLY degrade a session we already hold: chromium cannot reach the SUT from
  // inside the container to perform a fresh login, so the best case is a no-op
  // and the worst case is the about:blank wipe. This guard is independent of
  // HAS_FRESH_CREDS / COOKIE_VALUE-match (R112.A.4 only fires when the env
  // cookie matches — but sibling Playwright tools like axe run globalSetup with
  // a non-matching/empty env cookie and would otherwise rebuild+wipe the file
  // that PW reads next, leaving PW with 0/0 → auth_stale on every spec).
  // Killswitch: ARTA_R181_C_SKIP_REBUILD_IF_POPULATED=0.
  if (process.env.ARTA_R181_C_SKIP_REBUILD_IF_POPULATED !== '0' && fs.existsSync(AUTH_STATE_PATH)) {
    try {
      const _full = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      const _ck = Array.isArray(_full.cookies) ? _full.cookies.length : 0;
      const _ls = Array.isArray(_full.origins)
        ? _full.origins.reduce((n: number, o: any) => n + ((o?.localStorage || []).length), 0) : 0;
      if (_ck > 0 && _ls > 0) {
        console.log(
          `[ARTA Auth R181.C] On-disk storage state is already a full session `
          + `(${_ck} cookie(s), ${_ls} localStorage key(s)) — skipping rebuild to `
          + `avoid the about:blank wipe. Reusing the populated session as-is.`,
        );
        return;
      }
    } catch { /* not parseable — fall through to rebuild */ }
  }

  console.log(`[ARTA Auth] Setting up ${AUTH_METHOD} authentication for ${BASE_URL}`);

  // R181 KEYSTONE — snapshot the existing POPULATED storage state before the
  // rebuild. createAuthState saves via context.storageState({path}); when
  // chromium can't reach the SUT (transient egress) or lands on the login wall,
  // it writes {cookies:[],origins:[]} → DESTROYS the operator's pasted session
  // (the about:blank wipe; bypasses the save-time guards). After the rebuild we
  // restore this snapshot if the rebuild produced an empty state. Bulletproof:
  // covers about:blank, placeholder creds, and login-wall causes uniformly.
  // Killswitch: ARTA_PW_PRESERVE_STATE_ON_NAV_FAIL=0.
  const _r181_pop = (s: any): boolean =>
    (Array.isArray(s?.cookies) && s.cookies.length > 0)
    || (Array.isArray(s?.origins) && s.origins.some((o: any) => (o?.localStorage || []).length > 0));
  // the localStorage (agent-user-token / selected-project) the SPA needs to
  // authenticate. A cookie-only file is "populated" but still redirects every
  // spec to /login. So track BOTH counts and restore on degradation of either.
  const _r181_counts = (s: any): [number, number] => [
    (Array.isArray(s?.cookies) ? s.cookies.length : 0),
    (Array.isArray(s?.origins) ? s.origins.reduce((n: number, o: any) => n + ((o?.localStorage || []).length), 0) : 0),
  ];
  let _r181_snapshot: string | null = null;
  let _r181_snap_counts: [number, number] = [0, 0];
  try {
    if (fs.existsSync(AUTH_STATE_PATH)) {
      const _ex = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      if (_r181_pop(_ex)) { _r181_snapshot = JSON.stringify(_ex); _r181_snap_counts = _r181_counts(_ex); }
    }
  } catch { /* no snapshot */ }

  const browser = await chromium.launch({ headless: true });

  // Create auth state for default role
  await createAuthState(browser, AUTH_STATE_PATH);

  // Multi-role: create separate auth states per role
  if (ROLES_JSON) {
    try {
      const roles = JSON.parse(ROLES_JSON) as Array<{ name: string; localStorage_overrides?: Record<string, any> }>;
      for (const role of roles) {
        const rolePath = AUTH_STATE_PATH.replace('.json', `-${role.name}.json`);
        await createAuthState(browser, rolePath, role);
        console.log(`[ARTA Auth] Created auth state for role: ${role.name}`);
      }
    } catch (e) {
      console.warn('[ARTA Auth] Failed to parse TARGET_AUTH_ROLES:', e);
    }
  }

  await browser.close();

  // R181 KEYSTONE — if the rebuild produced an EMPTY storage state but we had a
  // populated one before, RESTORE it (never let an empty rebuild destroy the
  // operator's / R162-refreshed session). This also fixes the downstream L3
  // auth pre-flight "no credentials found" skip, since the preserved file then
  // satisfies storage_state_has_creds.
  if (_r181_snapshot && process.env.ARTA_PW_PRESERVE_STATE_ON_NAV_FAIL !== '0') {
    try {
      const _new = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, 'utf-8'));
      const [_newCk, _newLs] = _r181_counts(_new);
      const [_snapCk, _snapLs] = _r181_snap_counts;
      // Restore on DEGRADATION of either dimension — a cookie-only rebuild
      // (localStorage wiped) still auth_stale-skips every spec.
      if (_newCk < _snapCk || _newLs < _snapLs) {
        fs.writeFileSync(AUTH_STATE_PATH, _r181_snapshot);
        console.warn(
          `[ARTA Auth R181] Rebuild DEGRADED the storage state `
          + `(cookies ${_snapCk}->${_newCk}, localStorage ${_snapLs}->${_newLs}; `
          + `SUT unreachable / login-wall / placeholder creds) — RESTORED the `
          + `populated session. about:blank wipe prevented.`,
        );
      }
    } catch { /* leave as-is */ }
  }

  // R112.A — persist the cookie_hash + build timestamp so the next run's
  // cache lookup can compare. The meta file lives alongside the storage
  // state file (e.g., `.../staging-storage.json.r112a.meta.json`).
  try {
    fs.writeFileSync(
      META_PATH,
      JSON.stringify({
        cookie_hash: COOKIE_HASH,
        built_at: new Date().toISOString(),
      }, null, 2),
    );
    console.log(`[ARTA Auth R112.A] Cached storage state hash=${COOKIE_HASH}`);
  } catch (e) {
    console.warn(`[ARTA Auth R112.A] Failed to write meta file: ${e}`);
  }

  console.log('[ARTA Auth] Authentication setup complete');
}

async function createAuthState(
  browser: any,
  statePath: string,
  role?: { name: string; localStorage_overrides?: Record<string, any>; role_field?: string; role_value?: string }
) {
  const context = await browser.newContext();
  const page = await context.newPage();

  // Set cookies based on auth method
  if (AUTH_METHOD === 'cookie' && COOKIE_NAME && COOKIE_VALUE) {
    const url = new URL(BASE_URL);
    await context.addCookies([{
      name: COOKIE_NAME,
      value: COOKIE_VALUE,
      domain: url.hostname,
      path: '/',
      httpOnly: false,
      secure: url.protocol === 'https:',
      sameSite: 'Lax' as const,
    }]);
  }

  // Navigate to set localStorage (Chromium in Docker may not reach external URLs)
  try {
    await page.goto(BASE_URL, { waitUntil: 'domcontentloaded', timeout: 15000 });
  } catch {
    // Try about:blank as fallback — allows cookie save + localStorage on some browsers
    try {
      await page.goto('about:blank');
      console.warn(`[ARTA Auth] Target ${BASE_URL} not reachable — using about:blank for auth state`);
    } catch {
      console.warn(`[ARTA Auth] Cannot open any page — saving cookies only`);
      await context.storageState({ path: statePath });
      await context.close();
      return;
    }
  }

  // Set localStorage values
  const lsData = JSON.parse(LOCAL_STORAGE);

  // Apply role overrides if provided
  if (role?.localStorage_overrides) {
    Object.assign(lsData, role.localStorage_overrides);
  }

  // If role has role_field/role_value, modify the user object in localStorage
  if (role?.role_field && role?.role_value) {
    for (const [key, val] of Object.entries(lsData)) {
      try {
        const parsed = typeof val === 'string' ? JSON.parse(val as string) : val;
        if (parsed && typeof parsed === 'object' && role.role_field in parsed) {
          parsed[role.role_field] = role.role_value;
          lsData[key] = JSON.stringify(parsed);
        }
      } catch { /* not JSON, skip */ }
    }
  }

  await page.evaluate((data: Record<string, string>) => {
    for (const [key, value] of Object.entries(data)) {
      localStorage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value));
    }
  }, lsData);

  // R112.A.2 — save auth state ONLY if it has real creds. Pre-R112.A.2,
  // when chromium couldn't navigate to BASE_URL (Docker container can't
  // reach external SUT), the about:blank fallback path saved an empty
  // storage state → destroyed the operator's R45.2-pasted file → L3 auth
  // pre-flight refused to dispatch on the next smoke (run-25aabd evidence:
  // PW + Newman pillars both single SKIP with auth_failure).
  const builtState = await context.storageState();
  const stateHasCookies = (builtState.cookies || []).length > 0;
  const stateHasOriginCreds = (builtState.origins || []).some(
    (o: any) => (o.localStorage || []).length > 0,
  );
  if (stateHasCookies || stateHasOriginCreds) {
    fs.writeFileSync(statePath, JSON.stringify(builtState, null, 2));

    // R144.C — post-build navigation verification. Pre-R144.C, auth-setup.ts
    // trusted that non-empty storage state meant the SUT would honor it.
    // But when the SPA rejects the cookie (R144.B cookie scope, expired
    // token, signature mismatch, IP allowlist), the file looks valid on
    // disk while every downstream spec's beforeEach lands on /login →
    // R112.E auth-stale SKIP cascades silently. Iter 3-v3 (run-4f5f58)
    // evidence: 131 of 198 PW tests SKIPPED with no operator-actionable
    // signal. R144.C closes this by re-navigating to a known-protected
    // route + throwing when the SUT redirects to /login*. Playwright's
    // globalSetup propagates the throw; dispatcher catches it and
    // persists a single BLOCKED row instead of 131 silent skips.
    //
    // Operator-tunable via TARGET_AUTH_VERIFY_ROUTE (default '/dashboard').
    // Network errors do NOT throw — preserves legacy behavior when the
    // SUT is unreachable for non-auth reasons (R143.D handles that path).
    if (process.env.TARGET_AUTH_VERIFY_DISABLE !== '1') {
      const verifyRoute = process.env.TARGET_AUTH_VERIFY_ROUTE || '/dashboard';
      try {
        await page.goto(BASE_URL.replace(/\/$/, '') + verifyRoute, {
          waitUntil: 'domcontentloaded',
          timeout: 12000,
        });
        const landed = page.url();
        const rejected = /\/(login|signin|auth-redirect)(\?|\/|$)/i.test(landed);
        if (rejected) {
          await context.close();
          throw new Error(
            `[ARTA Auth R144.C] Storage state NOT honored by SUT — ` +
            `nav to ${verifyRoute} landed on ${landed}. ` +
            `Investigate cookie domain scope (R84/R144.B), cookie expiry, ` +
            `OR SPA-side token signature mismatch. Storage state preserved ` +
            `for forensic inspection at ${statePath}.`
          );
        }
        console.log(`[ARTA Auth R144.C] Verified — landed on ${landed}`);
      } catch (e: any) {
        // Re-throw R144.C-prefixed errors (storage-not-honored signal);
        // swallow only network errors (preserves legacy unreachable-SUT
        // behavior — R143.D surfaces that path with its own truthful row).
        if (e?.message?.includes('[ARTA Auth R144.C]')) throw e;
        console.warn(`[ARTA Auth R144.C] Verification nav errored (network?): ${e?.message}`);
      }
    }
  } else if (fs.existsSync(statePath)) {
    try {
      const existing = JSON.parse(fs.readFileSync(statePath, 'utf-8'));
      if ((existing.cookies || []).length > 0
          || (existing.origins || []).some((o: any) => (o.localStorage || []).length > 0)) {
        console.log(
          `[ARTA Auth R112.A.2] Rebuild produced 0 cookies/0 localStorage `
          + `(chromium likely unreachable to ${BASE_URL}). Preserving `
          + `existing file with ${existing.cookies?.length || 0} cookies.`
        );
      } else {
        fs.writeFileSync(statePath, JSON.stringify(builtState, null, 2));
      }
    } catch {
      fs.writeFileSync(statePath, JSON.stringify(builtState, null, 2));
    }
  } else {
    // No existing file + empty new build — write the empty file so callers
    // don't error on missing file (legacy behavior preservation).
    fs.writeFileSync(statePath, JSON.stringify(builtState, null, 2));
  }
  await context.close();
}

export default globalSetup;
