import { mkdirSync } from "node:fs";
import path from "node:path";

import jsQR from "jsqr";
import { PNG } from "pngjs";

import { APP_URL, API_URL, SCREENSHOT_DIR } from "./support/env";
import { expect, noHorizontalOverflow, payButton, test } from "./support/fixtures";

mkdirSync(SCREENSHOT_DIR, { recursive: true });
const shot = (name: string) => path.join(SCREENSHOT_DIR, name);

test.describe("merchant page", () => {
  test("validates input locally before calling the API", async ({ page }) => {
    let creates = 0;
    page.on("request", (r) => {
      if (r.method() === "POST" && r.url() === `${API_URL}/api/sessions`) creates += 1;
    });
    await page.goto("/");
    await expect(page.getByText("Simulated payments only")).toBeVisible();
    const submit = page.getByRole("button", { name: "Create payment session" });

    await submit.click();
    await expect(page.getByText("Enter a merchant reference.")).toBeVisible();
    await expect(page.getByText("Enter an amount.")).toBeVisible();

    await page.getByLabel("Merchant reference").fill("ORDER-E2E-VALIDATION");
    await page.getByLabel("Amount").fill("10.999");
    await submit.click();
    await expect(page.getByText("Use at most 2 decimal places.")).toBeVisible();

    await page.getByLabel("Amount").fill("0.00");
    await submit.click();
    await expect(page.getByText("Amount must be greater than zero.")).toBeVisible();

    await page.getByLabel("Amount").fill("10.00");
    await page.getByLabel("Expires in (minutes)").fill("1441");
    await submit.click();
    await expect(page.getByText("from 1 to 1440")).toBeVisible();
    expect(creates).toBe(0);
  });

  test("creates a session with exact money and a scannable, copyable checkout link", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: APP_URL });
    const bodies: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "POST" && r.url() === `${API_URL}/api/sessions`) bodies.push(r.postData() ?? "");
    });
    await page.goto("/");
    await page.getByLabel("Merchant reference").fill("  ORDER-E2E-1  ");
    await page.getByLabel("Amount").fill("49.99");
    await page.getByLabel("Currency").selectOption("EUR");
    await page.getByLabel(/Description/).fill("Browser test");
    await page.getByLabel("Expires in (minutes)").fill("15");
    // A double submit in one tick must create only one session.
    await page.evaluate(() => {
      const form = document.querySelector("form")!;
      form.requestSubmit();
      form.requestSubmit();
    });
    await expect(page.getByRole("heading", { name: "Payment session created" })).toBeVisible();

    expect(bodies).toHaveLength(1);
    const sent = JSON.parse(bodies[0]);
    expect(sent).toMatchObject({ merchant_reference: "ORDER-E2E-1", amount: "49.99", expires_in_minutes: 15 });
    expect(bodies[0]).toContain('"amount":"49.99"'); // a string, never a float
    expect(bodies[0]).toMatch(/"expires_in_minutes":15[,}]/); // a JSON integer

    await expect(page.locator(".amount__value")).toHaveText("49.99");
    await expect(page.locator(".amount__currency")).toHaveText("EUR");
    await expect(page.getByText("Awaiting payment")).toBeVisible();
    const checkoutUrl = await page.locator("#checkout-url").inputValue();
    expect(checkoutUrl).toMatch(/^http:\/\/localhost:3020\/pay\/[0-9a-f-]{36}$/);

    // QR is rendered locally and encodes exactly the backend's checkout URL.
    expect(await page.locator("figure.qr img").count()).toBe(0);
    const png = PNG.sync.read(await page.locator("figure.qr svg").screenshot());
    expect(jsQR(new Uint8ClampedArray(png.data), png.width, png.height)?.data).toBe(checkoutUrl);

    await page.getByRole("button", { name: "Copy link" }).click();
    await expect(page.getByRole("button", { name: "Copied" })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(checkoutUrl);
    await page.screenshot({ path: shot("desktop-created.png"), fullPage: true });

    const [checkout] = await Promise.all([
      context.waitForEvent("page"),
      page.getByRole("link", { name: "Open checkout" }).click(),
    ]);
    await expect(checkout).toHaveURL(checkoutUrl);
    await expect(payButton(checkout)).toHaveText("Pay 49.99 EUR (simulated)");

    await page.getByRole("button", { name: "Create another session" }).click();
    await expect(page.getByRole("button", { name: "Create payment session" })).toBeVisible();
  });

  test("mobile layout has no horizontal overflow from form to paid checkout", async ({ newContext }) => {
    const context = await newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    const page = await context.newPage();
    await page.goto("/");
    expect(await noHorizontalOverflow(page)).toBe(true);
    await page.screenshot({ path: shot("mobile-form.png"), fullPage: true });

    await page.getByLabel("Merchant reference").fill("ORDER-E2E-MOBILE");
    await page.getByLabel("Amount").fill("1234567890.05");
    await page.getByLabel("Currency").selectOption("INR");
    await page.getByRole("button", { name: "Create payment session" }).tap();
    await expect(page.locator(".amount__value")).toHaveText("1234567890.05");
    expect(await noHorizontalOverflow(page)).toBe(true);
    await page.screenshot({ path: shot("mobile-created.png"), fullPage: true });

    await page.goto(await page.locator("#checkout-url").inputValue());
    await expect(payButton(page)).toHaveText("Pay 1234567890.05 INR (simulated)");
    expect(await noHorizontalOverflow(page)).toBe(true);
    await payButton(page).tap();
    await expect(page.getByText("Payment complete")).toBeVisible();
    expect(await noHorizontalOverflow(page)).toBe(true);
    await page.screenshot({ path: shot("mobile-paid.png"), fullPage: true });
  });
});
