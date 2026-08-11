/**
 * ARTA Platform — Smarter Locators (R116.A)
 *
 * Deterministic runtime-adaptive locator chain. When the LLM-generated
 * `page.getByRole(role, { name })` fails because the SUT's ARIA name has
 * shifted (e.g., "EXTRACT" → "EXTRACT DATA"), locale changed, or the
 * component re-rendered with a different aria-label, this helper falls
 * back through a deterministic chain:
 *
 *   Tier 1 (1500ms):  getByRole(role, { name })            — original
 *   Tier 2 (1500ms):  getByText(name)                      — visible text
 *   Tier 3 (1000ms):  getByTestId(kebab(name))             — testid guess
 *   Tier 3 (1000ms):  getByTestId(kebab(name) + '-' + role)
 *   Tier 4 (1000ms):  locator(`[aria-label*="${name}"]`)   — partial aria
 *
 * Cost discipline: ZERO LLM cost (pure DOM probing). Tier 1 mimics the
 * original locator; tiers 2-4 cap at 5500ms total worst-case. This is
 * complementary to R115.C vision-assist:
 *   - R116.A: always-on, deterministic, no operator config
 *   - R115.C: opt-in, LLM-vision, last-resort when DOM is fully empty
 *
 * Both can compose: smartLocate FIRST, then R115.C's vision fallback if
 * smartLocate still returns null. Together they cover the spectrum from
 * "DOM has the element under a different name/testid" (R116.A) to
 * "DOM has nothing but pixels show it" (R115.C).
 */
import { Page, Locator } from '@playwright/test';

/** Convert "EXTRACT DATA" → "extract-data" (kebab-case, alphanumeric only). */
function _kebab(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * R116.A — find an element via deterministic locator chain.
 *
 * Returns the first Locator that becomes visible within its tier budget,
 * OR null when all tiers exhaust. Caller decides next action (typically
 * fall through to R115.C vision-assist OR `expect.fail`).
 */
export async function smartLocate(
  page: Page,
  role: string,
  name: string,
  options: { timeout?: number } = {},
): Promise<Locator | null> {
  // Hard ceiling — total wall-clock budget for the chain. Operator
  // can pass a tighter timeout for time-sensitive paths.
  const overall = options.timeout ?? 5500;
  const _isVisible = async (loc: Locator, ms: number): Promise<boolean> => {
    try {
      return await loc.isVisible({ timeout: ms });
    } catch {
      return false;
    }
  };

  // Tier 1: original ARIA role + exact name (the LLM-generated path)
  const t1 = page.getByRole(role as any, { name });
  if (await _isVisible(t1, Math.min(overall, 1500))) {
    return t1;
  }

  // Tier 2: visible text content match (handles ARIA-label drift while
  // the text label remained intact — common in SPA i18n + a/b tests)
  const t2 = page.getByText(name).first();
  if (await _isVisible(t2, 1500)) {
    return t2;
  }

  // Tier 3: testid guesses (kebab and kebab-role)
  const kebab = _kebab(name);
  if (kebab) {
    const t3a = page.getByTestId(kebab);
    if (await _isVisible(t3a, 1000)) {
      return t3a;
    }
    const t3b = page.getByTestId(`${kebab}-${role.toLowerCase()}`);
    if (await _isVisible(t3b, 1000)) {
      return t3b;
    }
  }

  // Tier 4: partial aria-label match (catches "EXTRACT" → "EXTRACT DATA"
  // where the original substring still appears in the new aria-label)
  if (name && name.length >= 3) {
    // Escape for CSS attribute selector
    const safeName = name.replace(/"/g, '\\"').replace(/[\\]/g, '\\\\');
    const t4 = page.locator(`[aria-label*="${safeName}"]`).first();
    if (await _isVisible(t4, 1000)) {
      return t4;
    }
  }

  return null;
}

/**
 * R116.A — click an element via the smart-locator chain.
 *
 * Returns true when clicked, false when chain exhausts without a hit.
 * Caller decides next action (typically `expect(clicked).toBe(true)`).
 */
export async function smartClick(
  page: Page,
  role: string,
  name: string,
  options: { timeout?: number } = {},
): Promise<boolean> {
  const loc = await smartLocate(page, role, name, options);
  if (!loc) return false;
  try {
    await loc.click({ timeout: 2000 });
    return true;
  } catch {
    return false;
  }
}

/**
 * R116.A — assert an element is visible via the smart-locator chain.
 *
 * Returns true when ANY tier resolves visible, false otherwise.
 * Used inside R115.C wrappers as the deterministic tier between
 * DOM fast-path and vision-assist fallback.
 */
export async function smartVisible(
  page: Page,
  role: string,
  name: string,
  options: { timeout?: number } = {},
): Promise<boolean> {
  const loc = await smartLocate(page, role, name, options);
  return loc !== null;
}
