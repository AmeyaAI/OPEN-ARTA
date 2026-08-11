import baseConfig from '../common/playwright.base.config';
import { defineConfig } from '@playwright/test';

export default defineConfig({
  ...baseConfig,
  testDir: '.',
  testMatch: '**/*.spec.ts',
  testIgnore: ['**/auth.setup.ts', '**/global-setup.ts'],
  /* Override global setup to use the common generic auth setup */
  globalSetup: require.resolve('../common/auth-setup'),
});
