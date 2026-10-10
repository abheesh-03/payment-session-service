import { defineConfig } from "@playwright/test";

// Run through `npm run test:e2e`, which starts and stops the servers.
// Browser: installed Google Chrome by default; set E2E_BROWSER_CHANNEL=chromium
// to use Playwright's bundled Chromium (`npx playwright install chromium`).
const channel = process.env.E2E_BROWSER_CHANNEL ?? "chrome";

export default defineConfig({
  testDir: "./e2e",
  // One shared database, Redis and server pair: run serially for determinism.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: Boolean(process.env.CI),
  outputDir: "./test-results",
  reporter: [["list"], ["html", { open: "never", outputFolder: "./playwright-report" }]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: "http://localhost:3020",
    channel: channel === "chromium" ? undefined : channel,
    headless: true,
    viewport: { width: 1280, height: 900 },
    actionTimeout: 10_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
