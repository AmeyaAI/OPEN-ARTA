/**
 * E3b (R262) — per-test HTTP response recorder for attribution.
 *
 * Playwright's JSON reporter carries no per-request HTTP data, so a PW failure
 * (assertion/timeout) lands in the DB with no status_code — and the defect
 * classifier can only shrug (`not_assessed`, confidence 0.0). Measured: 0 of
 * 542 PW FAIL rows in run-0c19e6 were attributable, so Playwright contributed
 * nothing to the SUT-quality report.
 *
 * This installs a `page.on('response')` listener that remembers the last
 * NON-2xx response, and — only when the test FAILS — writes it into
 * `testInfo.annotations` as a structured marker the exec-side parser
 * (`_r262_parse_pw_status`, execution.py) reads with highest confidence:
 *
 *     [ARTA R262][response]{"status":503,"url":"/AssetManagement/api/..."}
 *
 * Why last-non-2xx: if the page 5xx'd, that IS the cause of the downstream
 * `toBeVisible` timeout. If every response was 2xx, NOTHING is written — the
 * failure is a selector/timing issue (a gen defect), not an HTTP-level SUT
 * signal, and must stay honestly unattributed rather than be pinned on a 200.
 *
 * In-memory, zero disk overhead (unlike recordHar). Opt-out:
 * ARTA_PW_RESPONSE_RECORDER_DISABLE=1.
 */
import type { Page, TestInfo } from '@playwright/test';

interface CapturedResponse {
  status: number;
  url: string;
}

/**
 * Install the recorder. Call from a `test.beforeEach(async ({ page }, testInfo) => ...)`.
 * The paired flush runs automatically via `testInfo` teardown registration.
 */
export function installResponseRecorder(page: Page, testInfo: TestInfo): void {
  if (process.env.ARTA_PW_RESPONSE_RECORDER_DISABLE === '1') return;

  let lastNon2xx: CapturedResponse | null = null;

  const onResponse = (resp: { status: () => number; url: () => string }) => {
    try {
      const status = resp.status();
      if (status >= 300) {
        // Strip host + query so the parser gets a clean request path.
        let url = resp.url() || '';
        url = url.replace(/^https?:\/\/[^/]+/i, '').split('?')[0];
        lastNon2xx = { status, url };
      }
    } catch {
      /* a response can be gone by the time we read it — ignore */
    }
  };
  page.on('response', onResponse);

  // Flush at test end: only annotate FAILED tests that saw a non-2xx.
  // `[Symbol.asyncDispose]` isn't available across all runners, so register a
  // teardown via the well-supported testInfo hook.
  const flush = () => {
    try {
      page.off('response', onResponse);
    } catch {
      /* page may already be closed */
    }
    const failed =
      testInfo.status !== undefined &&
      testInfo.status !== testInfo.expectedStatus;
    if (failed && lastNon2xx) {
      testInfo.annotations.push({
        type: 'arta-response',
        description:
          `[ARTA R262][response]{"status":${lastNon2xx.status},` +
          `"url":${JSON.stringify(lastNon2xx.url)}}`,
      });
    }
  };

  // Playwright runs registered teardown fns after the test body + afterEach.
  // `testInfo` doesn't expose an add-teardown API on all versions, so the gen
  // template pairs this with an explicit `test.afterEach` calling
  // `flushResponseRecorder`. Expose the flush on testInfo for that call.
  (testInfo as unknown as { _artaFlushResponse?: () => void })._artaFlushResponse = flush;
}

/**
 * Paired flush. Call from a `test.afterEach(async ({}, testInfo) => ...)`.
 * No-op when the recorder wasn't installed (killswitch / cookie-auth spec).
 */
export function flushResponseRecorder(testInfo: TestInfo): void {
  const f = (testInfo as unknown as { _artaFlushResponse?: () => void })._artaFlushResponse;
  if (typeof f === 'function') f();
}
