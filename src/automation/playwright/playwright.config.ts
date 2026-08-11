import { defineConfig, devices } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';

// R220.PW — read the CURRENT auth token from the storage-state FILE instead of
// = 15 min) whose token is refreshed on disk mid-run by a background
// refresh-grant keeper, a frozen env-var token expires partway through a long
// Playwright run → later specs 401. `storageState` is already a file PATH that
// Playwright re-reads per browser context (i.e. per spec), so page navigation
// stays fresh automatically; this helper brings the `extraHTTPHeaders` lane
// (page.request / APIRequestContext) into lockstep with the same file.
function readTokenFromStorageState(cookieName: string): string {
  try {
    const p = process.env.TARGET_AUTH_STATE_PATH;
    if (p && fs.existsSync(p)) {
      const ss = JSON.parse(fs.readFileSync(p, 'utf-8'));
      const c = (ss.cookies || []).find((x: any) => x && x.name === cookieName);
      if (c && c.value) return c.value;
    }
  } catch { /* fall through */ }
  return '';
}

/**
 * ARTA Platform — Shared Playwright Configuration
 * Auth injected via storageState from execution pipeline.
 */
const testMatch = process.env.TARGET_TEST_MATCH
  ? new RegExp(process.env.TARGET_TEST_MATCH)
  : /\.(spec|test)\.(ts|js)$/;

// Per-test title filter for single-test execution. ARTA's runner sets this
// when the user clicks "Run this test" — Playwright's --grep narrows to
// matching test() titles within the matched specs. Suite runs leave it
// unset and run every test() in the matched files.
const titleGrep = process.env.TARGET_TEST_GREP
  ? new RegExp(process.env.TARGET_TEST_GREP)
  : undefined;

const authStatePath = process.env.TARGET_AUTH_STATE_PATH
  || path.resolve(__dirname, '../../.arta/environments/default-storage.json');

const resultsPath = process.env.TARGET_RESULTS_PATH
  || path.resolve(__dirname, '../../.arta/results/results.json');

// R76 — per-spec HAR capture for endpoint coverage rollup.
// When ARTA's dispatcher sets TARGET_HAR_PATH, every request the
// browser makes during this spec's execution is recorded to that path.
// The dispatcher parses the HAR post-subprocess via
// `_r76_extract_har_endpoints` and stamps `metadata.endpoint_keys` on
// matching result rows so R74.1's SUT-quality dashboard +
// R55.13's endpoint-coverage gate aggregate Playwright alongside
// Newman / k6 / ZAP / Axe (R55.7, R75.1).
//
// `mode: 'minimal'` skips response bodies; `content: 'omit'` discards
// content entirely → files stay at ~100-500KB per spec, deleted by
// the dispatcher after parsing.
//
// When TARGET_HAR_PATH is unset (ad-hoc local runs, dev), recordHar
// is NOT enabled — preserves existing behavior + avoids any auth-
// combined edge cases (per the discovery_probe note at line ~75).
const harPath = process.env.TARGET_HAR_PATH || null;

export default defineConfig({
  testDir: process.env.TARGET_TEST_DIR || '.',
  testMatch,
  ...(titleGrep ? { grep: titleGrep } : {}),
  globalSetup: require.resolve('../common/auth-setup'),
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  // Phase L12 — bump CI retries to 2 (network flakes more common under load),
  // keep local at 1. Tests that fail due to assertion errors do NOT benefit
  // from retry (the assertion is wrong, retry just re-runs the wrong test);
  // tests that fail due to network/timeout do benefit. Playwright doesn't
  // distinguish, so retries: 2 is a tradeoff: more flake recovery vs more
  // wall-clock for genuinely-broken tests. K3's assertion-quality work
  // means most assertion-class failures are caught at gen time now.
  retries: process.env.CI ? 2 : 1,
  // R220.PW — env-overridable worker count. For SUTs with a short token TTL
  // token window so auth stays valid end-to-end. Default 2 (unchanged).
  workers: parseInt(process.env.PW_WORKERS || '2', 10),
  timeout: 60_000,

  // R294 — per-spec BLOB reporter (when TARGET_BLOB_OUTPUT_FILE is set) so the
  // per-spec subprocesses each write a UNIQUE blob instead of the html reporter
  // clobbering {run_id}-report/ every spec (the native "Playwright" tab then
  // only showed the LAST spec). ARTA merges the blobs into one html report
  // after the loop (`playwright merge-reports`). The json reporter is per-spec
  // unique (resultsPath) and untouched, so ARTA's own result parsing is
  // unaffected. Falls back to the html reporter when the blob env is absent
  // (discovery probe, non-ARTA invocations) — backward compatible.
  reporter: [
    ['list'],
    ['json', { outputFile: resultsPath }],
    process.env.TARGET_BLOB_OUTPUT_FILE
      ? ['blob', { outputFile: process.env.TARGET_BLOB_OUTPUT_FILE }]
      : ['html', { outputFolder: process.env.TARGET_HTML_REPORT_DIR || path.resolve(__dirname, '../../.arta/results/report'), open: 'never' }],
  ],

  use: {
    baseURL: process.env.TARGET_BASE_URL || 'http://localhost:3000',
    storageState: authStatePath,
    // Phase K2 — propagate auth Cookie to BOTH page navigation AND
    // page.request.X() API calls. Pre-K2, generated tests calling
    // `page.request.post()` got HTML login pages back (62 of 240
    // Playwright failures in run-89e80da6 were
    // `SyntaxError: Unexpected token '<', '<!DOCTYPE'…`). Playwright's
    // APIRequestContext is a separate context from the browser's
    // cookie jar — `extraHTTPHeaders` is the single config knob that
    // covers both lanes.
    // R220.PW — prefer the FRESH token read from the storage-state file (kept
    // current by the refresh-grant keeper) over the frozen env-var value, so
    // page.request API calls don't 401 once the dispatch-time token expires
    extraHTTPHeaders: (() => {
      const cookieName = process.env.TARGET_AUTH_COOKIE_NAME || 'session-token';
      const freshFromFile = readTokenFromStorageState(cookieName);
      const cookieVal = freshFromFile || process.env.TARGET_AUTH_COOKIE_VALUE || '';
      const isBearer = (process.env.TARGET_AUTH_METHOD || '').toLowerCase() === 'bearer';
      const bearerDisabled = process.env.ARTA_PW_BEARER_HEADER_DISABLE === '1';
      if (isBearer && cookieVal && !bearerDisabled) {
        // as a Bearer header for page.request AND as a Cookie for SSR routes.
        return {
          'Authorization': `Bearer ${cookieVal}`,
          'Cookie': `${cookieName}=${cookieVal}`,
        };
      }
      if (process.env.TARGET_AUTH_COOKIE_VALUE || freshFromFile) {
        return { 'Cookie': `${cookieName}=${cookieVal}` };
      }
      return undefined;
    })(),
    // Fix O: 'retain-on-failure' captures a trace on the FIRST failure
    // attempt (not just on retry). Previously 'on-first-retry' meant a
    // test that fails on attempt 1 then passes on retry produced no
    // useful trace — the only trace was from the passing retry.
    // Retain-on-failure: trace every test, keep ZIP only when the test
    // FAILS (passed-test traces auto-deleted, no extra disk).
    // BMAD Layer 6 evidence — "performance traces" deliverable.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    headless: true,
    actionTimeout: 10000,
    // R76 — conditional HAR capture. Only enabled when ARTA's dispatcher
    // sets TARGET_HAR_PATH per-spec. Captures request metadata only
    // (mode=minimal + content=omit) for the endpoint-coverage rollup.
    ...(harPath ? {
      recordHar: {
        path: harPath,
        mode: 'minimal' as const,
        content: 'omit' as const,
      },
    } : {}),
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    /**
     * Phase B2 — Discovery project. Same browser config as 'chromium' but
     * records every test's network traffic to a HAR file. The orchestrator
     * (Stage 2.5) invokes Playwright with `--project=discovery` after ATDD
     * generation; the resulting HAR feeds the Phase B4 env-var harvester +
     * Phase C chain extractor.
     *
     * `mode: 'minimal'` keeps the HAR file small (~1MB per run) — no large
     * binary content. `content: 'embed'` keeps request/response bodies
     * inline (vs. external file references) so the parser is single-file.
     *
     * ARTA_HAR_OUT is per-run; the orchestrator sets it to
     * `.arta/discovery/{run_id}/discovery.har`. Phase I1 redacts the file
     * before any persistence.
     */
    {
      name: 'discovery',
      // R186 — the discovery probe needs a MUCH longer per-test timeout than the
      // wait alone is ~22s on the first route, then the BFS walks up to 30 real
      // routes + 22 API probes. Pre-R186 the probe authenticated /organizations
      // and captured DOM but TIMED OUT at 60s before finishing the BFS / flushing
      // the HAR → "HAR not produced; harvest empty" (NOT a crash, NOT a login
      // wall — purely insufficient budget). 240s lets it complete + close the
      // context so recordHar flushes. Subprocess cap is 540s. Env-overridable.
      timeout: parseInt(process.env.ARTA_R186_PROBE_TIMEOUT_MS ?? '240000', 10),
      // R186 — no retry for discovery: a 2nd 240s attempt just doubles wall-clock
      // toward the 540s subprocess cap without changing the outcome (the probe is
      // deterministic given the same session). Fail fast on the single attempt.
      retries: 0,
      // R8 — scope discovery to the curated probe ONLY. Loading every
      // generated *.spec.ts caused TS compilation errors (broken imports,
      // duplicate `test` declarations in legacy specs) to fail the whole
      // project before any test executed → empty HAR → no env-var harvest →
      // every consumer test cascade-skipped. The probe is hand-written,
      // self-contained, and exercises real SUT URLs so the HAR is rich.
      // Operators can still override via TARGET_TEST_MATCH for ad-hoc
      // discovery scopes.
      testMatch: process.env.TARGET_TEST_MATCH
        ? new RegExp(process.env.TARGET_TEST_MATCH)
        : /discovery_probe\.spec\.ts$/,
      use: {
        ...devices['Desktop Chrome'],
        // R8 — discovery runs with an EXPLICIT EMPTY storageState. The
        // parent `use.storageState` points to a file path that may not
        // exist on first discovery (path resolves to a missing
        // `<repo>/src/.arta/environments/...storage.json`). Setting
        // `storageState: undefined` does NOT clear the parent in
        // Playwright's merge — only an explicit object overrides. An
        // inline `{cookies: [], origins: []}` makes discovery start
        // fresh, with auth coming exclusively via extraHTTPHeaders
        // (Cookie header) propagated from the env-vars store.
        storageState: { cookies: [], origins: [] } as any,
        recordHar: {
          path: process.env.ARTA_HAR_OUT
            || path.resolve(__dirname, '../../.arta/discovery/discovery.har'),
          mode: 'minimal',
          content: 'embed',
        },
      },
    },
  ],
});
