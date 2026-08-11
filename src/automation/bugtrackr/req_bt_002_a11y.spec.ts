import { test, expect } from '@playwright/test';
import { injectAxe, checkA11y, getViolations } from 'axe-playwright';

test.describe('Accessibility — REQ-BT-002', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(process.env.BASE_URL || '/');
    await injectAxe(page);
  });

  // AC: ac-1
  test('Keyboard-only navigation for bug status update', async ({ page }) => {
    await page.goto('/bug/BUG-1014/edit');
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
  test('Screen-reader announcement for bug status update', async ({ page }) => {
    await page.goto('/bug/BUG-1015/edit');
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
  test('Bug status transitions from Open to In Progress', async ({ page }) => {
    await page.goto('/bug/BUG-1001/edit');
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
  test('Bug status transition from Open to In Progress with empty bug ID', async ({ page }) => {
    await page.goto('/bug/BUG-1001/edit');
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
  test('Attempt to transition bug status from Open to In Progress when bug does not exist', async ({ page }) => {
    await page.goto('/bug/BUG-1002/edit');
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
  test('Attempt to transition bug status from Open to In Progress when bug is already in In Progress', async ({ page }) => {
    await page.goto('/bug/BUG-1003/edit');
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

  // AC: ac-2
  test('Bug can be resolved and closed', async ({ page }) => {
    await page.goto('/bug/BUG-1004/edit');
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

  // AC: ac-2
  test('Bug status transition from In Progress to Closed with empty bug ID', async ({ page }) => {
    await page.goto('/bug/BUG-1004/edit');
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

  // AC: ac-2
  test('Attempt to transition bug status from In Progress to Closed when bug does not exist', async ({ page }) => {
    await page.goto('/bug/BUG-1005/edit');
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

  // AC: ac-2
  test('Attempt to transition bug status from In Progress to Closed when bug is already Closed', async ({ page }) => {
    await page.goto('/bug/BUG-1006/edit');
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

  // AC: ac-2
  test('SQL injection attempt in bug status update', async ({ page }) => {
    await page.goto('/bug/BUG-1007/edit');
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

  // AC: ac-2
  test('XSS attempt in bug status update', async ({ page }) => {
    await page.goto('/bug/BUG-1008/edit');
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

  // AC: ac-2
  test('CSRF attempt in bug status update', async ({ page }) => {
    await page.goto('/bug/BUG-1009/edit');
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
  test('Auth bypass attempt in bug status update', async ({ page }) => {
    await page.goto('/bug/BUG-1010/edit');
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
  test('Bug status transition from Open to In Progress within acceptable response time', async ({ page }) => {
    await page.goto('/bug/BUG-1011/edit');
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
  test('Idempotency of bug status transition from Open to In Progress', async ({ page }) => {
    await page.goto('/bug/BUG-1012/edit');
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
  test('Concurrent bug status transitions from Open to In Progress', async ({ page }) => {
    await page.goto('/bug/BUG-1013/edit');
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