/**
 * ARTA Platform — Vision-Assist Fallback (R115.C)
 *
 * When standard Playwright DOM queries (getByRole / getByText / getByTestId)
 * time out but the element MIGHT BE rendered visually (SPA hydration gap,
 * post-aria-label rebind, late React Suspense boundary), take a screenshot
 * and ask ARTA's LLM-vision endpoint to locate the element by description.
 *
 * Opt-in per project via `integrations.vision_assist_enabled: true`. ARTA
 * dispatch sets `TARGET_VISION_ASSIST=1` env var when enabled. Cost gate:
 * caller should ALWAYS try DOM-first (≤5s fast path) before falling back
 * to vision — vision calls are bounded to ≤1 per timeout, not per action.
 *
 * Architecture: this is a TypeScript helper for Playwright spec runtime.
 * It calls back to arta-api at /api/internal/vision-locate which routes
 * the request through the project's configured LLM (Claude Opus 4.7 with
 * vision / Gemini Pro Vision / GPT-4V). If the project's LLM doesn't
 * support vision OR the call fails, returns null gracefully and the
 * caller's existing assertion logic still runs.
 */
import { Page } from '@playwright/test';

export interface VisionBBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * R115.C — locate an element by visual description (LLM-vision fallback).
 *
 * Returns a bounding-box for the operator to click, OR null if:
 *   - TARGET_VISION_ASSIST !== '1' (opt-in gate not enabled)
 *   - arta-api unreachable / endpoint errored
 *   - LLM couldn't find the element with confidence
 *
 * Caller pattern (R115.C HARD CONSTRAINT):
 *   const btn = page.getByRole('button', { name: 'EXTRACT' });
 *   let visible = await btn.isVisible({ timeout: 5000 }).catch(() => false);
 *   if (!visible && process.env.TARGET_VISION_ASSIST === '1') {
 *     const bbox = await findByVision(page, 'EXTRACT button');
 *     if (bbox) visible = true;  // pixel-confirmed despite DOM gap
 *   }
 *   expect(visible).toBe(true);
 */
export async function findByVision(
  page: Page,
  description: string,
  options: { timeout?: number } = {},
): Promise<VisionBBox | null> {
  // Opt-in gate — never fires unless operator enabled it per project
  if (process.env.TARGET_VISION_ASSIST !== '1') {
    return null;
  }

  const timeout = options.timeout ?? 15000;
  let screenshot: Buffer;
  try {
    screenshot = await page.screenshot({ fullPage: false, timeout: 8000 });
  } catch (err) {
    // Page may be navigating / closed — graceful skip
    console.warn('[R115.C] vision-assist: screenshot failed:', err);
    return null;
  }

  const apiUrl = process.env.TARGET_ARTA_VISION_URL
    ?? 'http://arta-api:8000/api/internal/vision-locate';
  const apiKey = process.env.TARGET_ARTA_API_KEY ?? '';
  const projectId = process.env.TARGET_PROJECT_ID ?? '';

  try {
    const resp = await fetch(apiUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(apiKey ? { 'X-API-Key': apiKey } : {}),
      },
      body: JSON.stringify({
        description,
        screenshot_b64: screenshot.toString('base64'),
        project_id: projectId,
      }),
      signal: AbortSignal.timeout(timeout),
    });
    if (!resp.ok) {
      console.warn(`[R115.C] vision-assist: arta-api returned ${resp.status}`);
      return null;
    }
    const data = await resp.json() as { bbox?: VisionBBox | null };
    if (data.bbox && typeof data.bbox.x === 'number' && typeof data.bbox.y === 'number') {
      console.log(
        `[R115.C] vision-located "${description}" at`,
        `(${data.bbox.x}, ${data.bbox.y}) ${data.bbox.w}x${data.bbox.h}`,
      );
      return data.bbox;
    }
    return null;
  } catch (err) {
    console.warn('[R115.C] vision-assist: endpoint call failed:', err);
    return null;
  }
}

/**
 * R115.C — click an element via vision-assist if DOM queries fail.
 *
 * Falls back gracefully: if vision-assist is disabled OR LLM can't locate,
 * returns false and the caller decides next action (typically `expect.fail`).
 *
 * Idempotent on repeated calls; does NOT cache because page state changes.
 */
export async function visionClickFallback(
  page: Page,
  description: string,
): Promise<boolean> {
  const bbox = await findByVision(page, description);
  if (!bbox) return false;
  try {
    await page.mouse.click(bbox.x + bbox.w / 2, bbox.y + bbox.h / 2);
    return true;
  } catch (err) {
    console.warn('[R115.C] vision-assist: mouse click failed:', err);
    return false;
  }
}
