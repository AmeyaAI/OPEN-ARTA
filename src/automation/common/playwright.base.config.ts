/**
 * ARTA Platform — Generic Base Playwright Config
 *
 * Shared Playwright configuration that all project-specific configs
 * can extend. Reads target URL, auth state, and results path from
 * environment variables set by the ARTA execution pipeline.
 */
import { defineConfig } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';

const AUTH_STATE = process.env.TARGET_AUTH_STATE_PATH || path.resolve(__dirname, '../../.arta/auth-state.json');

// R220.PW — read the CURRENT bearer token from the storage-state FILE rather
// than a value frozen at dispatch. For SUTs with a short access-token TTL
// refresh-grant keeper, the frozen `TARGET_AUTH_BEARER_TOKEN` env var expires
// partway through a long Playwright run → `page.request` API specs 401. Reading
// the token from the storage-state cookie keeps `page.request` auth in lockstep
// with the file that Playwright re-reads for `storageState` per context.
// Falls back to the env var when the file has no matching cookie.
function readFreshBearerFromStorageState(): string {
  try {
    const ss = JSON.parse(fs.readFileSync(AUTH_STATE, 'utf-8'));
    const cookieName = process.env.TARGET_AUTH_COOKIE_NAME || 'k0r-access-token';
    const c = (ss.cookies || []).find((x: any) => x && x.name === cookieName);
    if (c && c.value) return c.value;
  } catch { /* fall through to env var */ }
  return process.env.TARGET_AUTH_BEARER_TOKEN || '';
}
const FRESH_BEARER = readFreshBearerFromStorageState();
const RESULTS_PATH = process.env.TARGET_RESULTS_PATH || path.resolve(__dirname, '../../.arta/results/results.json');

// R143.D.2 — chromium host-resolver-rules bridge. When arta-api detects
// reachability asymmetry (arta-api can reach SUT; chromium-in-container
// cannot resolve the SUT host), ARTA stamps the env var below with a
// chromium-compatible `MAP host:port ip:port` rule. We forward it as a
// chromium launch arg so chromium uses the IP that arta-api proved working.
// Pre-R143.D.2: chromium-side DNS failure → net::ERR_TIMED_OUT.
// Post-R143.D.2: chromium bypasses its broken DNS via the resolver-rules map.
//
// R145.E — belt-and-suspenders defensive stack. Layer 1 (this var) +
// Layer 3 (resilience flags below; --disable-features=AsyncDns,EnableHTTP3,
// DnsOverHttps + --dns-prefetch-disable + --no-pings). Layer 2 lives in
// auth-setup.ts globalSetup as an independent dns.lookup pre-resolution
// that re-derives the rule when the env var is absent — covers the case
// where R145.C reveals the env var was dropped between dispatcher and
// subprocess.
const R143_D2_RESOLVER_RULES = process.env.TARGET_CHROMIUM_HOST_RESOLVER_RULES || '';

// R145.E Layer 3 — resilience flags. Belt-and-suspenders defense:
// even when Layer 1's --host-resolver-rules flag IS present + honored,
// chromium may still time out on (a) HTTP/3 negotiation (NEW_CONNECTION_ERROR),
// (b) async DNS cache contention with the OS resolver, or (c) DoH lookups.
// These flags disable each of those paths and force chromium into the
// synchronous OS-resolver path that arta-api proves works.
// Killswitch: ARTA_R145_E_RESILIENCE_FLAGS_DISABLE=1 reverts to Layer 1 only.
const R145_E_RESILIENCE_DISABLED = process.env.ARTA_R145_E_RESILIENCE_FLAGS_DISABLE === '1';
const R145_E_LAYER3_FLAGS = (R143_D2_RESOLVER_RULES && !R145_E_RESILIENCE_DISABLED)
  ? [
      '--disable-features=AsyncDns,EnableHTTP3,DnsOverHttps',
      '--dns-prefetch-disable',
      '--no-pings',
    ]
  : [];

// R146.C.2 Layer 4 — TLS-insecure flags for cert-class chromium asymmetry.
// Activated only when R146.C.1 classifier detects cert_issue (NOT default-on,
// preserves security posture for prod-pointed runs). Adds 3 chromium flags
// + ignoreHTTPSErrors: true to test contexts. Killswitch:
// ARTA_R146_C2_TLS_INSECURE_DISABLE=1.
const R146_C2_TLS_INSECURE =
  process.env.TARGET_CHROMIUM_TLS_INSECURE === '1'
  && process.env.ARTA_R146_C2_TLS_INSECURE_DISABLE !== '1';
const R146_C2_LAYER4_FLAGS = R146_C2_TLS_INSECURE ? [
  '--ignore-certificate-errors',
  '--allow-running-insecure-content',
  '--disable-features=BlockInsecurePrivateNetworkRequests',
] : [];

// R146.C.4 Layer 5 — chromium-internal config flags for HTTP/2 / cipher /
// cache asymmetry classes. Each flag activates per-env-var so dispatcher
// can opt-in only the kinds R146.C.1 classifier identified. Killswitch:
// ARTA_R146_C4_CONFIG_FIX_DISABLE=1.
const R146_C4_DISABLED = process.env.ARTA_R146_C4_CONFIG_FIX_DISABLE === '1';
const R146_C4_HTTP2_FIX = !R146_C4_DISABLED
  && process.env.TARGET_CHROMIUM_DISABLE_HTTP2 === '1';
const R146_C4_PROXY_FIX = !R146_C4_DISABLED
  && process.env.TARGET_CHROMIUM_NO_PROXY === '1';
const R146_C4_CIPHER_FIX = !R146_C4_DISABLED
  && process.env.TARGET_CHROMIUM_RELAX_CIPHERS === '1';
const R146_C4_CACHE_FIX = !R146_C4_DISABLED
  && process.env.TARGET_CHROMIUM_DISABLE_CACHE === '1';
const R146_C4_LAYER5_FLAGS = [
  ...(R146_C4_HTTP2_FIX  ? ['--disable-http2', '--disable-features=AlternateProtocolsHttp2'] : []),
  ...(R146_C4_PROXY_FIX  ? ['--no-proxy-server', '--proxy-bypass-list=*'] : []),
  ...(R146_C4_CIPHER_FIX ? ['--ssl-version-min=tls1.2', '--ssl-version-max=tls1.3'] : []),
  ...(R146_C4_CACHE_FIX  ? ['--disable-background-networking', '--disable-dns-prefetching'] : []),
];

const R143_D2_LAUNCH_ARGS = R143_D2_RESOLVER_RULES
  ? [
      `--host-resolver-rules=${R143_D2_RESOLVER_RULES}`,
      ...R145_E_LAYER3_FLAGS,
      ...R146_C2_LAYER4_FLAGS,
      ...R146_C4_LAYER5_FLAGS,
    ]
  : [
      // Layer 4 + Layer 5 may fire even when DNS bridge inactive — e.g., a
      // SUT whose DNS resolves normally for chromium BUT trips cert chain.
      ...R146_C2_LAYER4_FLAGS,
      ...R146_C4_LAYER5_FLAGS,
    ];

// R145.C trace site 4-TS — chromium_launch_args (stderr-captured).
// Mirrors the Python-side pw_subprocess_spawn event but FROM INSIDE the
// PW subprocess so operators see whether the bridge env reached chromium.
// stderr lines are saved to .arta/results/pw-{run_id}-{spec}-stderr.log
// by execution.py:4115 — survives docker compose log rotation. Extended
// in R146.C with TLS-insecure + chromium-config layer markers.
console.log(
  `[R145.C] chromium_launch_args resolverRulesPresent=${!!R143_D2_RESOLVER_RULES} ` +
  `argCount=${R143_D2_LAUNCH_ARGS.length} ` +
  `rulePreview=${(R143_D2_RESOLVER_RULES || '').slice(0, 80)} ` +
  `r146c2TlsInsecure=${R146_C2_TLS_INSECURE} ` +
  `r146c4Layer5=${JSON.stringify({http2: R146_C4_HTTP2_FIX, proxy: R146_C4_PROXY_FIX, cipher: R146_C4_CIPHER_FIX, cache: R146_C4_CACHE_FIX})}`
);

export default defineConfig({
  testDir: process.env.TARGET_TEST_DIR || path.resolve(__dirname, '../playwright'),
  globalSetup: require.resolve('./auth-setup'),

  use: {
    baseURL: process.env.TARGET_BASE_URL || 'http://localhost:3000',
    storageState: AUTH_STATE,
    // header at the context level so direct `page.request.get()` API-test
    // calls carry the Bearer token. The SPA's own XHRs read the token from
    // localStorage (via storageState), but `page.request` does NOT — so
    // without this, every API-assertion spec hits the backend unauthenticated
    // → 401 (146 PW "auth" fails in run-7ef084). Per-request headers still
    // override this, so negative tests (`headers: { Authorization: '' }`)
    // TARGET_AUTH_METHOD !== 'bearer' → header not set. Killswitch:
    // ARTA_PW_BEARER_HEADER_DISABLE=1.
    ...(((process.env.TARGET_AUTH_METHOD || '').toLowerCase() === 'bearer')
      && FRESH_BEARER
      && process.env.ARTA_PW_BEARER_HEADER_DISABLE !== '1'
      ? { extraHTTPHeaders: { Authorization: `Bearer ${FRESH_BEARER}` } }
      : {}),
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
    headless: true,
    actionTimeout: 10000,
    // R146.C.2 — when classifier detects cert-class chromium asymmetry,
    // ignoreHTTPSErrors stops Playwright from short-circuiting on cert
    // validation. Layer 4 flags on chromium launch tell the browser the
    // same thing; this `use:` field tells Playwright's test-context
    // factories to honor it too.
    ignoreHTTPSErrors: R146_C2_TLS_INSECURE,
    // R143.D.2 — propagate the resolver-rules to chromium (only when set)
    ...(R143_D2_LAUNCH_ARGS.length > 0 && {
      launchOptions: {
        args: R143_D2_LAUNCH_ARGS,
      },
    }),
  },

  reporter: [
    ['json', { outputFile: RESULTS_PATH }],
    ['html', { open: 'never', outputFolder: process.env.TARGET_HTML_REPORT_DIR || path.resolve(__dirname, '../../.arta/results/report') }],
    ['list'],
  ],

  outputDir: process.env.TARGET_ARTIFACTS_DIR || path.resolve(__dirname, '../../.arta/results/artifacts'),
  timeout: 30000,
  retries: 1,
  workers: 2,
  fullyParallel: true,
});
