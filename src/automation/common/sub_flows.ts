/**
 * ARTA Platform — Shared Sub-Flows (R116.B Behavior Caching)
 *
 * Common test orchestration helpers extracted from inline-injected
 * patterns (R112.E auth-verify, R114.C SPA hydration). Reduces per-spec
 * LOC, improves consistency, and lets ARTA evolve auth/hydration logic
 * in ONE file instead of regenerating 22+ specs.
 *
 * Mission framing (Pillar 1b — high-quality test scripts):
 *   - Pre-R116.B: each PW spec hand-rolled ~30 lines of auth+hydration
 *     setup (R112.E + R114.C inline definitions duplicated across 22 specs).
 *     Total ~660 LOC of duplicated boilerplate.
 *   - Post-R116.B: each spec imports + calls (~2 lines per pattern).
 *     Total ~44 LOC for the same functionality. ~93% reduction in
 *     boilerplate; ARTA-side updates land in ONE file.
 *
 * Architectural integrity: this file is the canonical implementation.
 * The injectors at `_r112_e_inject_auth_verify` + `_r114_c_inject_spa_hydration`
 * now produce CALLS to these helpers (not inline copies). Specs that
 * predate R116.B will pick up the import on next regen.
 */
import { Page, test } from '@playwright/test';

/**
 * R114.C — Wait for SPA bootstrap. Combines network-idle + session-token
 * presence + non-login pathname as signals that the SPA has finished
 * hydration. Pre-R114.C, page.goto returned before the SPA finished
 * mounting → selector assertions timed out at 10s with cryptic errors.
 *
 * The session-token localStorage key is SUT-specific: dispatch exports
 * TARGET_SPA_TOKEN_LS_KEY from the project's auth config
 * (auth.credentials.token_cookie_name), same override contract as
 *
 * Call after every `page.goto('/<auth-route>')` and before any
 * `expect(...).toBeVisible(...)` on a dynamic UI element.
 */
export async function waitForSPAReady(
  page: Page,
  options: { timeout?: number } = {},
): Promise<void> {
  const timeout = options.timeout ?? 15000;
  const lsKey = process.env.TARGET_SPA_TOKEN_LS_KEY || 'session-token';
  try {
    await page.waitForLoadState('networkidle', { timeout });
  } catch { /* fall through — page may still be usable */ }
  try {
    await page.waitForFunction(
      (key) => !!localStorage.getItem(key)
            && !window.location.pathname.includes('/login'),
      lsKey,
      { timeout },
    );
  } catch { /* fail-fast happens at next assertion */ }
}

/**
 * R112.E — Check whether the page is authenticated.
 *
 * R143.G fix (Iter 2 evidence run-1f2971): body-text matching on
 * HTML + menu/footer text contains those strings even when logged in.
 * 88+ tests across req_am_011/012/013 false-positive SKIPPED despite
 * not expired). Post-R143.G: check ONLY the URL pathname for canonical
 * login routes. If the SUT redirected to /login/signin/auth/*, auth
 * runs BEFORE this) — if it passed, the SPA is hydrated + logged in.
 *
 * Use `skipIfAuthStale` for the standard skip-with-operator-CTA pattern.
 */
export async function isAuthLoggedIn(page: Page): Promise<boolean> {
  return await page.evaluate(() => {
    const path = window.location.pathname || '';
    // R143.G — URL-only check. Canonical SPA redirect routes when
    // auth fails. Pre-R143.G the body-text check matched the SPA's
    // own "SIGN IN" menu/footer strings even on logged-in pages.
    return !path.startsWith('/login')
      && !path.startsWith('/signin')
      && !path.startsWith('/auth/');
  });
}

/**
 * R112.E + R144.H — Skip the current test with an operator-actionable
 * message when the auth state is stale. Use after `page.goto(...)` on
 * authenticated routes.
 *
 * R144.H — the skip-reason is suffixed with a structured cause prefix
 * (auth_stale_url_redirect | auth_stale_unknown) so the dispatcher-
 * side PW parser can bucket skips by cause + the mission-report
 * surfaces a per-cause breakdown. Pre-R144.H: generic 'auth_stale'
 * string carried no structured channel for the dashboard tile.
 *
 * Live evidence (Iter 3-v3 run-4f5f58): 131 of 198 PW tests SKIPPED
 * via this path with no per-cause attribution. Post-R144.H + R144.D:
 * mission-report 'skip_by_cause' dict carries actionable counts +
 * Pillar 4 truthfulness restored.
 */
export async function skipIfAuthStale(page: Page): Promise<void> {
  const ok = await isAuthLoggedIn(page);
  if (!ok) {
    const url = page.url();
    const cause = /\/(login|signin|auth-redirect)/i.test(url)
      ? 'auth_stale_url_redirect'
      : 'auth_stale_unknown';
    test.skip(
      true,
      `[ARTA R112.E][${cause}] Auth state stale — landed on ${url}. `
      + 'Operator: refresh auth via R45.2 OR inspect cookie domain '
      + 'scope (R84/R144.B).',
    );
  }
}
