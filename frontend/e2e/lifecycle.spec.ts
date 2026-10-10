import type { Page } from "@playwright/test";

import { API_URL, KEY_PREFIX } from "./support/env";
import { db, deferred, expect, openCheckout, payButton, storedKey, test, trackRequests } from "./support/fixtures";

/** Client-side navigation (no page reload): the old Checkout unmounts. */
async function navigateClientSide(page: Page, path: string) {
  await page.evaluate((p) => {
    (window as unknown as { next: { router: { push(href: string): void } } }).next.router.push(p);
  }, path);
}

/** Bounded negative check: no GET for this session within `ms`. */
async function noGetFor(page: Page, sessionId: string, ms = 1500): Promise<boolean> {
  const req = await page
    .waitForRequest((r) => r.method() === "GET" && r.url() === `${API_URL}/api/sessions/${sessionId}`, { timeout: ms })
    .catch(() => null);
  return req === null;
}

test.describe("checkout lifecycle on navigation", () => {
  test("a recovery timer queued at unmount never runs for the old session", async ({ page, api }) => {
    const a = await api.createSession("ORDER-E2E-LIFE-TIMER-A");
    const b = await api.createSession("ORDER-E2E-LIFE-TIMER-B");
    const ta = trackRequests(page, a.id);
    await page.clock.install();
    await openCheckout(page, a.id);

    // Freeze timers, queue a recovery (setTimeout 0), then leave the page.
    await page.clock.pauseAt(Date.now() + 1000);
    const getsBefore = ta.gets;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await navigateClientSide(page, `/pay/${b.id}`);
    await expect(page.getByText("ORDER-E2E-LIFE-TIMER-B")).toBeVisible();
    await expect(payButton(page)).toBeEnabled();

    await page.clock.resume(); // the queued timer would fire now
    expect(await noGetFor(page, a.id)).toBe(true);
    expect(ta.gets).toBe(getsBefore);
  });

  test("a payment pending at navigation cannot affect the next session", async ({ page, api }) => {
    const a = await api.createSession("ORDER-E2E-LIFE-PAY-A");
    const b = await api.createSession("ORDER-E2E-LIFE-PAY-B");
    const ta = trackRequests(page, a.id);
    const tb = trackRequests(page, b.id);
    const releaseA = deferred();
    const releaseB = deferred();
    const postA = deferred();
    const postB = deferred();
    for (const [s, seen, release] of [
      [a, postA, releaseA],
      [b, postB, releaseB],
    ] as const) {
      await page.route(`${API_URL}/api/sessions/${s.id}/pay`, async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        seen.resolve();
        await release.promise;
        await route.continue();
      });
    }

    await openCheckout(page, a.id);
    await payButton(page).click();
    await postA.promise;
    await page.evaluate(() => window.dispatchEvent(new Event("focus"))); // queued behind A's payment
    await navigateClientSide(page, `/pay/${b.id}`);
    await expect(page.getByText("ORDER-E2E-LIFE-PAY-B")).toBeVisible();
    await expect(payButton(page)).toBeEnabled(); // B starts with its own, idle guard

    await payButton(page).click();
    await postB.promise;
    await expect(page.getByRole("button", { name: /Processing simulated payment/ })).toBeDisabled();

    // A's payment settles while B's is still in flight.
    const aDone = page.waitForResponse((r) => r.url() === `${API_URL}/api/sessions/${a.id}/pay`);
    releaseA.resolve();
    expect((await aDone).status()).toBe(200);
    expect(await noGetFor(page, a.id)).toBe(true); // A's queued recovery was dropped
    // B is untouched: still pending, guard still held, no second POST.
    await expect(page.getByRole("button", { name: /Processing simulated payment/ })).toBeDisabled();
    await page.evaluate(() => {
      [...document.querySelectorAll("button")].find((x) => /^(Pay|Processing)/.test(x.textContent ?? ""))?.click();
    });
    expect(tb.postKeys).toHaveLength(1);
    await expect(page.getByText("Payment complete")).toHaveCount(0);

    releaseB.resolve();
    await expect(page.getByText("Payment complete")).toBeVisible();
    await expect(page.getByText("ORDER-E2E-LIFE-PAY-B")).toBeVisible();
    expect(ta.postKeys).toHaveLength(1);
    expect(tb.postKeys[0]).not.toBe(ta.postKeys[0]); // never another session's key
    expect(db.receipts(a.id)).toBe(1);
    expect(db.receipts(b.id)).toBe(1);

    // Revisiting A shows the committed PAID result and discards A's stored key.
    expect(await storedKey(page, KEY_PREFIX + a.id)).toBe(ta.postKeys[0]);
    await page.goto(`/pay/${a.id}`);
    await expect(page.getByText("Payment complete")).toBeVisible();
    expect(await storedKey(page, KEY_PREFIX + a.id)).toBeNull();
  });
});
