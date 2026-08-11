import { defineConfig } from 'cypress';

export default defineConfig({
  e2e: {
    baseUrl: process.env.CYPRESS_BASE_URL || 'http://localhost:3000',
    specPattern: 'src/automation/cypress/**/*.cy.ts',
    supportFile: false,
    video: true,
    screenshotOnRunFailure: true,
    reporter: 'junit',
    reporterOptions: {
      mochaFile: 'cypress-results.xml',
      toConsole: false,
    },
    retries: {
      runMode: 2,
      openMode: 0,
    },
    defaultCommandTimeout: 10_000,
    requestTimeout: 10_000,
    responseTimeout: 30_000,
  },
});
