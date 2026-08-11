/**
 * R156.J.2 — Playwright auth-refresh shared helper.
 *
 * Single-source-of-truth for refresh logic in generated PW tests.
 * Imported into every generated spec's beforeEach to keep AUTH_TOKEN
 * agent_token TTL).
 *
 * Reuses R96.1 token-mint pattern (refresh is a transitive extension
 * of the same chain) and R45.2 storage state (PW dispatch already
 * exports `TARGET_AUTH_REFRESH_TOKEN` env var; R156.J.3 forwards it
 * as `REFRESH_TOKEN`).
 *
 * Operator-config env vars (populated by R156.J.3 dispatcher
 * propagation; ARTA's `_fetch_sut_source_context` → R156.I.2
 * TOKEN_CHAINS.agent_token.refresh_flow source-derived):
 *   AUTH_TOKEN                          -- current agent_token
 *   REFRESH_TOKEN                       -- current refresh_token
 *   ARTA_REFRESH_REQUEST_BODY_FIELD     -- e.g., "refresh_token" (default)
 *   ARTA_REFRESH_RESPONSE_ACCESS_FIELD  -- e.g., "access_token" (default)
 *   ARTA_REFRESH_RESPONSE_REFRESH_FIELD -- e.g., "refresh_token" (rotation) or "" (no rotation)
 *   ARTA_REFRESH_THRESHOLD_SEC          -- default 60 (refresh when TTL < this)
 *
 * SUT without refresh endpoint: ARTA_REFRESH_ENDPOINT will be empty
 * → helper is a no-op (truthful: long smokes will rely on operator
 * R45.2 paste cycle; not an ARTA gen bug).
 *
 * R154 non-mutation guarantee: refresh endpoint is exempt from
 * destructive-pattern check per R154.B whitelist (R156.J.3 adds
 * `auth_refresh.ts` to the whitelist).
 */

import type { Page, APIRequestContext } from '@playwright/test';
import * as fs from 'fs';

export interface RefreshOutcome {
  refreshed: boolean;
  ttlBefore: number | null;
  ttlAfter: number | null;
  reason?: string;
}

function decodeJwtExp(token: string): number | null {
  // Pure-string JWT exp claim decoder. Node.js Buffer + JSON.parse.
  // Returns Unix-seconds or null when the token isn't a valid JWT.
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const padded = b64 + '==='.slice((b64.length + 3) % 4);
    const payloadStr = Buffer.from(padded, 'base64').toString();
    const payload = JSON.parse(payloadStr);
    return typeof payload.exp === 'number' ? payload.exp : null;
  } catch {
    return null;
  }
}

// PW-auth ROOT FIX (within-file 401 storm) — a token/refresh or login endpoint
// authenticates via the BODY credential (api_key / refresh_token /
// userName+password), NOT a valid access bearer. But Playwright's request
// fixture inherits the config's FROZEN `extraHTTPHeaders.Authorization`
// (playwright.config.ts sets it once at config-load from the storage-state
// token). Once the access token expires MID-FILE, that ambient bearer is STALE,
// so a mint POST that silently inherits it is rejected by the SUT
// refresh then never lands, `process.env.AUTH_TOKEN` stays stale, and EVERY
// subsequent `request.get(url, {Authorization: Bearer ${AUTH_TOKEN}})` 401s —
// the exact within-file 401 gradient observed (run-d5b996: 158 PW 401s across
// 46 specs, flat vs prior runs despite eager in-spec refresh). Clearing the
// ambient Authorization makes the mint POST unauthenticated-by-bearer so the
// body credential is what the endpoint checks. PROVEN: identical grant POST
// from an expired-bearer context returns 401 with the inherited header and
// 200 (mints a token) with `Authorization: ''`. Cookie is left intact (a stale
// SUT-agnostic. Killswitch ARTA_REFRESH_CLEAR_AMBIENT_AUTH_DISABLE=1.
function _mintHeaders(base: Record<string, string>): Record<string, string> {
  if (process.env.ARTA_REFRESH_CLEAR_AMBIENT_AUTH_DISABLE === '1') { return base; }
  return { ...base, Authorization: '' };
}

/**
 * R156.J.2 — refresh AUTH_TOKEN when its JWT TTL drops below threshold.
 *
 * @param page Playwright Page (optional; used to also push the new
 *   token into the SPA's LocalStorage so subsequent in-page fetches
 *   see it). Pass `null` for API-only specs.
 * @param request Playwright APIRequestContext (optional; preferred for
 *   the refresh POST). If omitted AND `page` is supplied, uses
 *   `page.context().request`. If both are null, returns refreshed=false.
 * @param opts.thresholdSec Override the env-var threshold (default 60).
 * @returns RefreshOutcome with ttlBefore/ttlAfter and a reason when
 *   the helper skipped (e.g., no refresh endpoint configured).
 */

// R156.J.4 — resolve a possibly-dotted path ('data.authInfo.access_token')
// against a JSON object. A bare key ('access_token') is the single-segment
// case, so this is backward-compatible with the flat lookup it replaces.
function getByPath(obj: unknown, path: string): unknown {
  if (!path) { return undefined; }
  let cur: unknown = obj;
  for (const seg of path.split('.')) {
    if (cur == null || typeof cur !== 'object') { return undefined; }
    cur = (cur as Record<string, unknown>)[seg];
  }
  return cur;
}

// Fix 1 (R244 login re-mint) — module-level throttle so ~600 specs in one PW
// worker don't storm the SUT's /login. A successful re-mint yields a ~30-min
// token, so a second re-mint within 60s is always redundant.
let _lastRemintEpoch = 0;

// Fix 1 — write a freshly-minted access token back so subsequent reads see it:
// process.env (in-process specs) + the SPA's LocalStorage key (in-page fetches).
// k0r-access-token drives SSR page auth): (a) the CURRENT browser context's
// cookie jar, so this test's later page navigations carry the fresh token, and
// (b) the storage-state FILE (TARGET_AUTH_STATE_PATH), which Playwright
// re-reads at every context creation — so every SUBSEQUENT test() in this
// spec file (and any parallel worker) starts with the fresh cookie. Atomic
// tmp+rename write; with a reusable grant concurrent writers are benign
// (last-write-wins between equally-valid tokens).
// R253.AK — resolve the SUT's session-cookie name without relying on
// TARGET_AUTH_COOKIE_NAME (only exported on cookie-method SUTs / after the
// Newman Fix-AAA pre-flight — a bearer-method PW-only run has neither, yet
// cookie whose value IS the current (pre-refresh) access token; fallback to a
// single-cookie file; fallback to the declared env var.
function _resolveCookieName(oldToken: string): string {
  const declared = process.env.TARGET_AUTH_COOKIE_NAME || '';
  if (declared) { return declared; }
  try {
    const p = process.env.TARGET_AUTH_STATE_PATH || '';
    if (p && fs.existsSync(p)) {
      const cookies = (JSON.parse(fs.readFileSync(p, 'utf-8')).cookies || []) as any[];
      const byVal = cookies.find((c: any) => c && c.name && c.value && c.value === oldToken);
      if (byVal) { return byVal.name; }
      // Old env token and the file can diverge (server-side per-file top-up
      // rewrites the file between spawns) — fall back to the conventional
      // only cookie present.
      const byName = cookies.find(
        (c: any) => c && c.name && /access[-_]?token/i.test(String(c.name)),
      );
      if (byName) { return byName.name; }
      if (cookies.length === 1 && cookies[0] && cookies[0].name) { return cookies[0].name; }
    }
  } catch { /* fall through */ }
  return '';
}

async function _writeBackAuth(page: Page | null, newAccess: string): Promise<void> {
  // Resolve the cookie name BEFORE overwriting env — derivation matches the
  // storage-state cookie against the OLD token.
  const _oldTok = process.env.AUTH_TOKEN || '';
  const cookieName = _resolveCookieName(_oldTok);
  process.env.AUTH_TOKEN = newAccess;
  // Fix 1 — generated specs read the bearer from TARGET_AUTH_BEARER_TOKEN /
  // TARGET_AUTH_AGENT_TOKEN inline per request (`Bearer ${process.env.TARGET_AUTH_BEARER_TOKEN}`),
  // so the fresh token must land in those too (mirrors the Newman-side _r219y writeback).
  process.env.TARGET_AUTH_BEARER_TOKEN = newAccess;
  process.env.TARGET_AUTH_AGENT_TOKEN = newAccess;
  process.env.TARGET_AUTH_COOKIE_VALUE = newAccess;
  // PW-auth — the R213.J chain helper `authHeaderFor` (common/arta_auth.ts) resolves
  // agent_token, …}), NOT from process.env.AUTH_TOKEN. Regenerated specs prefer
  // authHeaderFor over an inline `Bearer ${AUTH_TOKEN}`, so a within-file refresh
  // that only updates AUTH_TOKEN leaves the blob STALE → chain-resolved calls keep
  // sending the expired token and 401 mid-file (run-b80156: 34 auth-cascade 401s
  // reappeared on authHeaderFor specs after the R336 regen switched the pattern).
  // Refresh every access-token alias IN the blob — plus any key still holding the
  // OLD token — while never touching refresh_token / id_token. Killswitch
  // ARTA_REFRESH_TOKENS_BLOB_DISABLE=1.
  if (process.env.ARTA_REFRESH_TOKENS_BLOB_DISABLE !== '1') {
    try {
      const _blob = JSON.parse(process.env.ARTA_AUTH_TOKENS || '{}');
      if (_blob && typeof _blob === 'object' && !Array.isArray(_blob)) {
        const _aliases = new Set([
          'session_token', 'access_token', 'agent_token', 'bearer_token',
          'auth_token', 'token', 'cookie_value',
        ]);
        let _changed = false;
        for (const _k of Object.keys(_blob)) {
          const _kl = _k.toLowerCase();
          if (_kl.includes('refresh') || _kl.includes('id_token')) continue;
          if (_aliases.has(_kl) || (_oldTok && _blob[_k] === _oldTok)) {
            _blob[_k] = newAccess;
            _changed = true;
          }
        }
        if (_changed) process.env.ARTA_AUTH_TOKENS = JSON.stringify(_blob);
      }
    } catch { /* blob refresh is best-effort — silent */ }
  }
  // (b) storage-state file — page-independent, do it even for API-only specs.
  if (cookieName) {
    const ssPath = process.env.TARGET_AUTH_STATE_PATH || '';
    try {
      if (ssPath && fs.existsSync(ssPath)) {
        const ss = JSON.parse(fs.readFileSync(ssPath, 'utf-8'));
        const hit = (ss.cookies || []).find((c: any) => c && c.name === cookieName);
        if (hit) {
          hit.value = newAccess;
          // A stale absolute `expires` would make Playwright drop the cookie
          // at context creation — mark it a session cookie instead.
          if (typeof hit.expires === 'number' && hit.expires > 0) { hit.expires = -1; }
          const tmp = `${ssPath}.${process.pid}.tmp`;
          fs.writeFileSync(tmp, JSON.stringify(ss));
          fs.renameSync(tmp, ssPath);
        }
      }
    } catch { /* storage-state rewrite is best-effort — silent */ }
  }
  if (!page) { return; }
  // (a) current context's cookie jar.
  if (cookieName) {
    try {
      const cookieUrl = process.env.TARGET_BASE_URL || page.url();
      if (cookieUrl && cookieUrl !== 'about:blank') {
        await page.context().addCookies([{ name: cookieName, value: newAccess, url: cookieUrl }]);
      }
    } catch { /* cookie writeback is best-effort — silent */ }
  }
  const lsKey = process.env.TARGET_SPA_TOKEN_LS_KEY || 'session-token';
  try {
    await page.evaluate(
      ({ key, val }) => {
        try { window.localStorage.setItem(key, val); } catch { /* disabled storage */ }
      },
      { key: lsKey, val: newAccess },
    );
  } catch { /* page not on SUT origin yet — silent */ }
}

// Fix 1 — RE-MINT the auth token via the SUT's LOGIN contract (userName/password)
// instead of the single-use/rotating refresh token. The dispatcher (R244.LOGIN-ENV)
// exports the fully-substituted login body + endpoint + token paths. Returns the
// fresh token string, or null when login isn't configured / the POST failed.
async function _remintViaLogin(
  reqCtx: APIRequestContext | null,
  page: Page | null,
): Promise<string | null> {
  if (process.env.ARTA_LOGIN_REMINT_DISABLE === '1') { return null; }
  const loginUrl = process.env.ARTA_LOGIN_ENDPOINT || '';
  const bodyRaw = process.env.ARTA_LOGIN_BODY_TEMPLATE || '';
  if (!loginUrl || !bodyRaw) { return null; }
  let body: Record<string, unknown>;
  try { body = JSON.parse(bodyRaw); } catch { return null; }
  const ctx = reqCtx ?? (page ? page.context().request : null);
  if (!ctx) { return null; }
  let paths: string[];
  try {
    paths = JSON.parse(process.env.ARTA_LOGIN_ACCESS_TOKEN_PATHS || '[]');
  } catch { paths = []; }
  if (!paths.length) { paths = ['data.authInfo.access_token', 'access_token', 'token']; }
  try {
    const resp = await ctx.fetch(loginUrl, {
      method: (process.env.ARTA_LOGIN_METHOD || 'POST').toUpperCase(),
      data: body,
      headers: _mintHeaders({ 'Content-Type': 'application/json', Accept: 'application/json' }),
      failOnStatusCode: false,
      // Fix 1 — hard timeout so a slow/unreachable login POST can NEVER hang the
      // beforeEach (a hang there fails EVERY test in the spec). 15s > the ~2s the
      // login normally takes; on timeout we fall through to the stale token.
      timeout: 15000,
    });
    if (!resp.ok()) {
      // eslint-disable-next-line no-console
      console.warn(`[Fix1] login re-mint failed: HTTP ${resp.status()}; proceeding with stale token`);
      return null;
    }
    const json = await resp.json();
    for (const p of paths) {
      const v = getByPath(json, p);
      if (typeof v === 'string' && v) {
        _lastRemintEpoch = Math.floor(Date.now() / 1000);
        await _writeBackAuth(page, v);
        // eslint-disable-next-line no-console
        console.log('[R156.J.2] auth_token refreshed via login re-mint');
        return v;
      }
    }
    // eslint-disable-next-line no-console
    console.warn('[Fix1] login re-mint response had no access_token at known paths');
    return null;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // eslint-disable-next-line no-console
    console.warn(`[Fix1] login re-mint exception: ${msg}; proceeding with stale token`);
    return null;
  }
}

/**
 * Fix 1 — force a login re-mint NOW, bypassing the TTL gate. For an optional
 * caught-401 path in a spec (`await remintAuthNow(page, request)` after a 401).
 */
export async function remintAuthNow(
  page: Page | null,
  request: APIRequestContext | null,
): Promise<RefreshOutcome> {
  const tok = await _remintViaLogin(request, page);
  return tok
    ? { refreshed: true, ttlBefore: null, ttlAfter: decodeJwtExp(tok) ? decodeJwtExp(tok)! - Math.floor(Date.now() / 1000) : null }
    : { refreshed: false, ttlBefore: null, ttlAfter: null, reason: 'login_remint_unavailable_or_failed' };
}

export async function refreshAuthIfExpiring(
  page: Page | null,
  request: APIRequestContext | null,
  opts: { thresholdSec?: number } = {},
): Promise<RefreshOutcome> {
  const token = process.env.AUTH_TOKEN || '';
  const refreshToken = process.env.REFRESH_TOKEN || '';
  const refreshUrl = process.env.ARTA_REFRESH_ENDPOINT || '';

  // Fix 1 — login re-mint is the PREFERRED refresh path (works when the SUT's
  // of the refresh-token flow via the R244.LOGIN-ENV dispatch export.
  const loginConfigured = !!(process.env.ARTA_LOGIN_ENDPOINT
    && process.env.ARTA_LOGIN_BODY_TEMPLATE
    && process.env.ARTA_LOGIN_REMINT_DISABLE !== '1');
  const refreshConfigured = !!(refreshUrl && refreshToken);

  if (!loginConfigured && !refreshConfigured) {
    return { refreshed: false, ttlBefore: null, ttlAfter: null, reason: 'no_refresh_or_login_configured' };
  }
  if (!token) {
    // C1 (UPSTREAM) — BOOTSTRAP mint. The login contract POSTs userName/password
    // and needs NO existing token, so an empty AUTH_TOKEN (missing/empty
    // storage-state seed) is NOT a dead end when login is configured — mint one
    // now instead of no-op'ing. Without this the fully-configured login harness is
    // silently unused for Playwright (Newman's R219.Y already bootstraps).
    // Killswitch ARTA_C1_BOOTSTRAP_MINT_DISABLE=1.
    if (loginConfigured && process.env.ARTA_C1_BOOTSTRAP_MINT_DISABLE !== '1') {
      const bootNow = Math.floor(Date.now() / 1000);
      if (bootNow - _lastRemintEpoch >= 60) {
        const bootTok = await _remintViaLogin(request, page);
        if (bootTok) {
          const bExp = decodeJwtExp(bootTok);
          // eslint-disable-next-line no-console
          console.log('[C1] bootstrap-minted auth_token via login (no prior token)');
          return { refreshed: true, ttlBefore: null, ttlAfter: bExp ? bExp - bootNow : null };
        }
      }
    }
    return { refreshed: false, ttlBefore: null, ttlAfter: null, reason: 'no_auth_token_to_refresh' };
  }

  // Login path uses a WIDER threshold (default 300s) so a 30-min token in a
  // 40-50-min run re-mints well before expiry; the refresh path keeps its 60s.
  let thresholdSec = opts.thresholdSec
    ?? (loginConfigured
      ? Number(process.env.ARTA_LOGIN_REMINT_THRESHOLD_SEC ?? 300)
      : Number(process.env.ARTA_REFRESH_THRESHOLD_SEC ?? 60));
  // M4/PW-expiry fix — a REUSABLE grant (api_key/client-credentials; nothing rotates,
  // concurrent redemptions never collide) makes per-test refresh SAFE, which is the
  // spec file (30 tests, ~15 min) crosses the ~15-min token TTL mid-execution, so a
  // narrow threshold lets later test() cases 401. Widen the threshold so the token is
  // kept well above expiry throughout the file (proven: kui_606 27/27 with eager
  // refresh vs. the prior within-file 401 gradient). Killswitch
  // ARTA_REUSABLE_EAGER_REFRESH_DISABLE=1 reverts to the narrow threshold.
  if (process.env.ARTA_REFRESH_REUSABLE === '1'
      && process.env.ARTA_REUSABLE_EAGER_REFRESH_DISABLE !== '1'
      && opts.thresholdSec === undefined) {
    thresholdSec = Math.max(thresholdSec, 600);
  }
  const now = Math.floor(Date.now() / 1000);
  const exp = decodeJwtExp(token);
  const ttlBefore = exp ? exp - now : null;

  if (ttlBefore === null) {
    // Opaque token — cannot decide preemptively. Operator opts into
    // forced-refresh via thresholdSec=0; otherwise no-op.
    if (thresholdSec === 0) {
      // Fall through to refresh.
    } else {
      return { refreshed: false, ttlBefore: null, ttlAfter: null, reason: 'opaque_token_no_exp_claim' };
    }
  } else if (ttlBefore > thresholdSec) {
    return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: 'ttl_above_threshold' };
  }

  // Fix 1 — PREFER login re-mint (with a 60s per-worker throttle) before the
  // the only path that actually re-mints. Falls through to refresh on failure.
  if (loginConfigured) {
    if (now - _lastRemintEpoch < 60) {
      return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: 'login_remint_throttled_60s' };
    }
    const newTok = await _remintViaLogin(request, page);
    if (newTok) {
      const nExp = decodeJwtExp(newTok);
      return { refreshed: true, ttlBefore, ttlAfter: nExp ? nExp - now : null };
    }
    if (!refreshConfigured) {
      return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: 'login_remint_failed' };
    }
    // else: fall through to the refresh-token path below.
  }

  const bodyField = process.env.ARTA_REFRESH_REQUEST_BODY_FIELD || 'refresh_token';
  const accessField = process.env.ARTA_REFRESH_RESPONSE_ACCESS_FIELD || 'access_token';
  const refreshField = process.env.ARTA_REFRESH_RESPONSE_REFRESH_FIELD || '';
  const body: Record<string, unknown> = {};
  body[bodyField] = refreshToken;
  // R156.J.4 — some SUTs require extra static fields on the refresh POST
  // needs {isFrontEnd:true, hostName:...}). ARTA_REFRESH_EXTRA_BODY is a JSON
  // object merged into the request body. No-op when unset.
  if (process.env.ARTA_REFRESH_EXTRA_BODY) {
    try {
      Object.assign(body, JSON.parse(process.env.ARTA_REFRESH_EXTRA_BODY));
    } catch {
      // eslint-disable-next-line no-console
      console.warn('[R156.J.4] ARTA_REFRESH_EXTRA_BODY is not valid JSON; ignoring');
    }
  }

  const reqCtx = request ?? (page ? page.context().request : null);
  if (!reqCtx) {
    return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: 'no_request_context_available' };
  }

  try {
    const resp = await reqCtx.post(refreshUrl, {
      data: body,
      headers: _mintHeaders({ 'Content-Type': 'application/json', Accept: 'application/json' }),
      failOnStatusCode: false,
    });
    if (!resp.ok()) {
      // eslint-disable-next-line no-console
      console.warn(`[R156.J.2] refresh failed: HTTP ${resp.status()}; proceeding with stale token`);
      return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: `http_${resp.status()}` };
    }
    let json: Record<string, unknown>;
    try {
      json = await resp.json();
    } catch {
      // eslint-disable-next-line no-console
      console.warn(`[R156.J.2] refresh response not JSON; proceeding with stale token`);
      return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: 'response_not_json' };
    }
    // token at `data.authInfo.access_token`, not a top-level key). A flat
    // json[accessField] can't reach nested envelopes; getByPath walks `a.b.c`.
    const newAccess = getByPath(json, accessField);
    if (typeof newAccess !== 'string' || !newAccess) {
      // eslint-disable-next-line no-console
      console.warn(
        `[R156.J.2] refresh response missing field "${accessField}"; ` +
        `proceeding with stale token`,
      );
      return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: `missing_${accessField}` };
    }

    // R253.AK — full writeback: process.env (inline per-request bearers) +
    // SPA LocalStorage + browser-context cookie + storage-state file. The
    // cookie/file lanes are what make WITHIN-FILE refresh effective on
    // fresh contexts from the storage-state file and navigate authenticated.
    await _writeBackAuth(page, newAccess);
    // Rotation: when SUT issues a new refresh_token, persist it too.
    if (refreshField) {
      const newRefresh = getByPath(json, refreshField);
      if (typeof newRefresh === 'string' && newRefresh) {
        process.env.REFRESH_TOKEN = newRefresh;
      }
    }

    const newExp = decodeJwtExp(newAccess);
    const ttlAfter = newExp ? newExp - now : null;
    // eslint-disable-next-line no-console
    console.log(
      `[R156.J.2] auth_token refreshed; ttlBefore=${ttlBefore ?? 'opaque'}s ` +
      `ttlAfter=${ttlAfter !== null ? `${ttlAfter}s` : 'opaque'}`,
    );
    return { refreshed: true, ttlBefore, ttlAfter };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    // eslint-disable-next-line no-console
    console.warn(`[R156.J.2] refresh exception: ${msg}; proceeding with stale token`);
    return { refreshed: false, ttlBefore, ttlAfter: ttlBefore, reason: `exception_${msg.slice(0, 60)}` };
  }
}
