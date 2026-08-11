/**
 * ARTA Platform — BugTrackr Auth Setup
 *
 * Playwright setup script that authenticates with BugTrackr
 * by setting cookies and localStorage, then saves the browser
 * state for reuse by all test specs.
 *
 */
import { chromium, type FullConfig } from '@playwright/test';
import * as path from 'path';
import * as fs from 'fs';

const BUGTRACKR_URL = process.env.BUGTRACKR_URL || 'http://localhost:3005';
const AUTH_STATE_PATH = path.resolve(__dirname, '../../.arta/auth-state.json');

// BugTrackr auth credentials — sourced from environment or defaults
const SESSION_TOKEN = process.env.BUGTRACKR_SESSION_TOKEN || '';
const REFRESH_TOKEN = process.env.BUGTRACKR_REFRESH_TOKEN || '';

const BUGTRACKR_USER = process.env.BUGTRACKR_USER_JSON || JSON.stringify({
  id: '00000000-0000-4000-8000-000000000000',
  userId: '000000000000000000000',
  name: 'Test Admin',
  email: 'admin@example.com',
  role: 'admin',
  availableRoles: ['admin', 'tester', 'developer'],
  createdAt: '2025-11-05T00:00:00.000Z',
  updatedAt: '2025-11-05T00:00:00.000Z',
});

async function globalSetup(config: FullConfig) {
  // Ensure auth state directory exists
  const authDir = path.dirname(AUTH_STATE_PATH);
  if (!fs.existsSync(authDir)) {
    fs.mkdirSync(authDir, { recursive: true });
  }

  // Skip auth setup if no token provided (tests will run unauthenticated)
  if (!SESSION_TOKEN) {
    console.log('[ARTA Auth] No BUGTRACKR_SESSION_TOKEN set — skipping auth setup');
    console.log('[ARTA Auth] Tests will run without authentication');
    // Create minimal empty state so Playwright doesn't error
    fs.writeFileSync(AUTH_STATE_PATH, JSON.stringify({ cookies: [], origins: [] }));
    return;
  }

  console.log('[ARTA Auth] Setting up BugTrackr authentication...');

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  await context.addCookies([
    {
      name: 'session-token',
      value: SESSION_TOKEN,
      domain: new URL(BUGTRACKR_URL).hostname,
      path: '/',
      httpOnly: false,
      secure: false,
      sameSite: 'Lax',
    },
  ]);

  // Navigate to BugTrackr to set localStorage
  try {
    await page.goto(BUGTRACKR_URL, { waitUntil: 'domcontentloaded', timeout: 15000 });
  } catch {
    console.log('[ARTA Auth] BugTrackr not reachable at', BUGTRACKR_URL);
    console.log('[ARTA Auth] Tests will attempt to run but may fail if auth is required');
    fs.writeFileSync(AUTH_STATE_PATH, JSON.stringify({ cookies: [], origins: [] }));
    await browser.close();
    return;
  }

  // Set localStorage values
  await page.evaluate(({ user, token, refreshToken }) => {
    localStorage.setItem('bugtrackr_current_user', user);
    localStorage.setItem('session-token', token);
    if (refreshToken) {
      localStorage.setItem('refresh-token', refreshToken);
    }
    localStorage.setItem('theme', 'dark');
  }, { user: BUGTRACKR_USER, token: JSON.stringify(SESSION_TOKEN), refreshToken: JSON.stringify(REFRESH_TOKEN) });

  // Save the authenticated state
  await context.storageState({ path: AUTH_STATE_PATH });
  await browser.close();

  console.log('[ARTA Auth] Authentication state saved to', AUTH_STATE_PATH);
}

export default globalSetup;
