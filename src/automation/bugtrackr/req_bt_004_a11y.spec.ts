import { test, expect } from '@playwright/test';
import { injectAxe, checkA11y, getViolations } from 'axe-playwright';

test.describe('Accessibility — REQ-BT-004', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
  });

  // AC: ac-1
  test('User adds a comment using keyboard-only navigation', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'This is a valid comment');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User dismisses comment form with Escape key', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'This is a valid comment');
    await page.keyboard.press('Escape');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User adds a comment to a bug', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'This is a valid comment');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User attempts to add a comment with boundary values', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', '');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User attempts to add a comment with invalid input', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'Invalid test');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User tries to inject malicious content via comment', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', '<script>alert("XSS")</script>');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User adds a comment under high load', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'This is a valid comment');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('User attempts to post the same comment twice', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'This is a valid comment');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });

  // AC: ac-1
  test('Multiple users add comments to the same bug concurrently', async ({ page }) => {
    await page.click('a[href="/bug/BUG-123"]');
    await page.fill('textarea[placeholder="Add a comment..."]', 'User 1 comment');
    await page.click('button:has-text("Post Comment")');
    await checkA11y(page, null, { detailedReport: true, axeOptions: { runOnly: ['wcag2a', 'wcag2aa'] } });
    const violations = await getViolations(page);
    expect(violations.length, JSON.stringify(violations.map(v => v.id))).toBe(0);

    // F8-12: Fail loudly if A11Y_REPORT_PATH is unset — silently skipping
    // the write means the gate sees zero violations and passes a regression.
    // The ARTA execution router always sets this env var; a missing value
    // indicates the test was run outside the documented harness.
    if (!process.env.A11Y_REPORT_PATH) {
      throw new Error('A11Y_REPORT_PATH unset — refusing to swallow accessibility violations silently. ' +
                      'Run via the ARTA execution router (which sets this env var) or set it explicitly.');
    }
    {
      const fs = require('fs');
      let prior = [];
      try { prior = JSON.parse(fs.readFileSync(process.env.A11Y_REPORT_PATH, 'utf-8')); } catch {}
      fs.writeFileSync(process.env.A11Y_REPORT_PATH,
        JSON.stringify([...prior, ...violations.map(v => ({ id: v.id, impact: v.impact, help: v.help }))], null, 2));
    }
  });
});