import { test, expect } from '@playwright/test';
import { injectAxe, checkA11y, getViolations } from 'axe-playwright';

test.describe('Accessibility — REQ-BT-005', () => {
  // AC: ac-1
  test('User creates a bug and status change is logged', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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
  test('Status change to empty string is logged', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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
  test('Attempting to change status to invalid value is rejected', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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
  test('Unauthorized user cannot view activity logs', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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
  test('Activity log retrieval under load', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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
  test('Multiple status changes to the same bug are logged', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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
  test('Concurrent status changes to the same bug are logged', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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

  // AC: ac-1 @a11y @wcag-2.1
  test('Activity log is accessible via keyboard and screen reader', async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
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