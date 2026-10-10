import { execFileSync } from "node:child_process";
import path from "node:path";

import {
  test as base,
  expect,
  type Browser,
  type BrowserContext,
  type BrowserContextOptions,
  type Page,
} from "@playwright/test";

import { API_URL, BACKEND_DIR, BACKEND_PYTHON } from "./env";

const SUPPORT_SCRIPT = path.join(BACKEND_DIR, "scripts", "e2e_support.py");

export interface ApiSession {
  id: string;
  status: "CREATED" | "PAID" | "EXPIRED";
  amount: string;
  currency: string;
  checkout_url: string;
}

/** Guarded database helpers (backend/scripts/e2e_support.py). */
export const db = {
  run(...args: string[]): string {
    return execFileSync(BACKEND_PYTHON, [SUPPORT_SCRIPT, ...args], { cwd: BACKEND_DIR })
      .toString()
      .trim();
  },
  receipts(sessionId: string): number {
    return Number(db.run("receipts", sessionId));
  },
  setExpiry(sessionId: string, seconds: number): void {
    db.run("set-expiry", sessionId, String(seconds));
  },
};

interface Owned {
  sessionIds: Set<string>;
}

interface Fixtures {
  /** Sessions created by this test; deleted (with receipts and cache keys) afterwards. */
  owned: Owned;
  /** Fails the test on console errors, page errors or hydration warnings. */
  consoleProblems: string[];
  api: {
    createSession(ref: string, overrides?: Record<string, unknown>): Promise<ApiSession>;
    status(id: string): Promise<ApiSession["status"]>;
  };
  /** New browser context whose pages are tracked like the default one. */
  newContext(options?: BrowserContextOptions): Promise<BrowserContext>;
}

function watchContext(context: BrowserContext, owned: Owned, problems: string[]): void {
  const watchPage = (page: Page) => {
    page.on("console", (msg) => {
      // Chrome logs each non-2xx/aborted fetch; tests provoke those on purpose.
      if (msg.type() === "error" && /Failed to load resource/.test(msg.text())) return;
      if (msg.type() === "error" || /hydrat/i.test(msg.text())) problems.push(msg.text());
    });
    page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
  };
  context.pages().forEach(watchPage);
  context.on("page", watchPage);
  // Track sessions created through the UI as well.
  context.on("response", async (res) => {
    if (res.request().method() === "POST" && res.url() === `${API_URL}/api/sessions` && res.status() === 201) {
      try {
        owned.sessionIds.add((await res.json()).id);
      } catch {
        // body unavailable (e.g. page closed); nothing to track
      }
    }
  });
}

export const test = base.extend<Fixtures>({
  owned: async ({}, provide) => {
    const owned: Owned = { sessionIds: new Set() };
    try {
      await provide(owned);
    } finally {
      // Runs for passing and failing tests alike.
      db.run("cleanup", ...owned.sessionIds);
    }
  },
  consoleProblems: async ({}, provide) => {
    const problems: string[] = [];
    await provide(problems);
    expect(problems, "console errors / hydration warnings").toEqual([]);
  },
  context: async ({ context, owned, consoleProblems }, provide) => {
    watchContext(context, owned, consoleProblems);
    await provide(context);
  },
  newContext: async ({ browser, owned, consoleProblems }, provide) => {
    const created: BrowserContext[] = [];
    await provide(async (options) => {
      const context = await (browser as Browser).newContext(options);
      watchContext(context, owned, consoleProblems);
      created.push(context);
      return context;
    });
    await Promise.all(created.map((c) => c.close()));
  },
  api: async ({ owned }, provide) => {
    await provide({
      async createSession(ref, overrides = {}) {
        const res = await fetch(`${API_URL}/api/sessions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ merchant_reference: ref, amount: "7.25", currency: "USD", ...overrides }),
        });
        expect(res.status).toBe(201);
        const session = (await res.json()) as ApiSession;
        owned.sessionIds.add(session.id);
        return session;
      },
      async status(id) {
        return ((await (await fetch(`${API_URL}/api/sessions/${id}`)).json()) as ApiSession).status;
      },
    });
  },
});

export { expect };

/** Records POST /pay Idempotency-Keys and GET counts for one session on a page. */
export function trackRequests(page: Page, sessionId: string) {
  const t = { postKeys: [] as string[], gets: 0 };
  page.on("request", (r) => {
    if (r.url() === `${API_URL}/api/sessions/${sessionId}/pay` && r.method() === "POST") {
      t.postKeys.push(r.headers()["idempotency-key"] ?? "");
    }
    if (r.url() === `${API_URL}/api/sessions/${sessionId}` && r.method() === "GET") t.gets += 1;
  });
  return t;
}

export function payButton(page: Page) {
  return page.getByRole("button", { name: /^Pay / });
}

export async function openCheckout(page: Page, sessionId: string) {
  await page.goto(`/pay/${sessionId}`);
  await expect(payButton(page)).toBeEnabled();
}

/** Inject a simulated provider outage into the next pay POST only. */
export async function failNextPay(page: Page, sessionId: string) {
  let inject = true;
  await page.route(`${API_URL}/api/sessions/${sessionId}/pay`, (route) => {
    if (route.request().method() === "POST" && inject) {
      inject = false;
      return route.continue({
        headers: { ...route.request().headers(), "x-mock-provider-outcome": "fail" },
      });
    }
    return route.continue();
  });
}

export async function storedKey(page: Page, storageKey: string): Promise<string | null> {
  return page.evaluate((k) => {
    try {
      return sessionStorage.getItem(k);
    } catch {
      return null;
    }
  }, storageKey);
}

export function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

export async function noHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
}
