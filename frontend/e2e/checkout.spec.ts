import { mkdirSync } from "node:fs";
import path from "node:path";

import { API_URL, KEY_PREFIX, SCREENSHOT_DIR } from "./support/env";
import {
  db,
  expect,
  failNextPay,
  openCheckout,
  payButton,
  storedKey,
  test,
  trackRequests,
} from "./support/fixtures";

mkdirSync(SCREENSHOT_DIR, { recursive: true });
const shot = (name: string) => path.join(SCREENSHOT_DIR, name);

test.describe("hosted checkout", () => {
  test("pays a session; PAID persists across refresh and reload", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-PAY");
    await openCheckout(page, s.id);
    await expect(page.locator(".countdown")).toBeVisible();
    await page.screenshot({ path: shot("desktop-checkout.png"), fullPage: true });

    await payButton(page).click();
    await expect(page.getByText("This simulated payment was confirmed")).toBeVisible();
    await expect(payButton(page)).toHaveCount(0);
    expect(await storedKey(page, KEY_PREFIX + s.id)).toBeNull(); // key discarded once PAID
    await page.screenshot({ path: shot("desktop-paid.png"), fullPage: true });

    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByText("Payment complete")).toBeVisible();
    await page.reload();
    await expect(page.getByText("Payment complete")).toBeVisible();
    await expect(payButton(page)).toHaveCount(0);
    expect(await api.status(s.id)).toBe("PAID");
    expect(db.receipts(s.id)).toBe(1);
  });

  test("provider failure keeps the session payable; the retry reuses the same key", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-PROVIDER");
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);
    await failNextPay(page, s.id);

    await payButton(page).click();
    await expect(page.getByText("The payment provider is temporarily unavailable, so no payment was made.")).toBeVisible();
    await expect(page.getByText("Awaiting payment")).toBeVisible();
    await page.screenshot({ path: shot("desktop-provider-failure.png"), fullPage: true });

    // Double click in one tick: still only one more request.
    await page.evaluate(() => {
      const btn = [...document.querySelectorAll("button")].find((b) => b.textContent?.startsWith("Pay "))!;
      btn.click();
      btn.click();
    });
    await expect(page.getByText("Payment complete")).toBeVisible();
    expect(t.postKeys).toHaveLength(2);
    expect(t.postKeys[1]).toBe(t.postKeys[0]);
    expect(t.postKeys[0]).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(db.receipts(s.id)).toBe(1);
  });

  test("countdown reaching zero triggers one refetch; the server decides EXPIRED", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-EXPIRY");
    db.setExpiry(s.id, 4);
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);
    await expect(page.getByText("This session has expired")).toBeVisible({ timeout: 15_000 });
    await expect(payButton(page)).toHaveCount(0);
    expect(t.gets).toBe(2); // initial load + one deadline check
    expect(await api.status(s.id)).toBe("EXPIRED");
    await page.screenshot({ path: shot("desktop-expired.png"), fullPage: true });
  });

  test("unknown and malformed session IDs show a not-found state", async ({ page }) => {
    await page.goto("/pay/00000000-0000-4000-8000-000000000000");
    await expect(page.getByRole("heading", { name: "Payment session not found" })).toBeVisible();
    await page.goto("/pay/not-a-uuid");
    await expect(page.getByRole("heading", { name: "Payment session not found" })).toBeVisible();
  });

  test("a network error on load offers Try again, which recovers", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-NETERR");
    await page.route(`${API_URL}/api/sessions/${s.id}`, (route) => route.abort("connectionrefused"));
    await page.goto(`/pay/${s.id}`);
    await expect(page.getByRole("heading", { name: "Couldn't load this checkout" })).toBeVisible();
    await page.unroute(`${API_URL}/api/sessions/${s.id}`);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(payButton(page)).toBeEnabled();
  });
});
