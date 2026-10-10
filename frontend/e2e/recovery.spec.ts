import { mkdirSync } from "node:fs";
import path from "node:path";

import { API_URL, KEY_PREFIX, SCREENSHOT_DIR } from "./support/env";
import {
  db,
  deferred,
  expect,
  failNextPay,
  noHorizontalOverflow,
  openCheckout,
  payButton,
  storedKey,
  test,
  trackRequests,
} from "./support/fixtures";

mkdirSync(SCREENSHOT_DIR, { recursive: true });
const shot = (name: string) => path.join(SCREENSHOT_DIR, name);
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UNCONFIRMED = "We couldn't confirm whether your payment went through";
const CONFIRMED_CREATED = "We confirmed with the server that this session is still awaiting payment";

test.describe("lost responses and reloads", () => {
  test("a successful payment whose response is lost recovers to PAID", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-LOST");
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);

    const evidence: Record<string, unknown> = {};
    await page.route(`${API_URL}/api/sessions/${s.id}/pay`, async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      // Forward to the real backend and wait for its complete response...
      const forwarded = await route.fetch();
      evidence.status = forwarded.status();
      evidence.bodyStatus = (await forwarded.json()).status;
      // ...confirm the commit independently...
      evidence.backendStatus = await api.status(s.id);
      evidence.receipts = db.receipts(s.id);
      // ...then drop the response: the browser sees a connection reset.
      await route.abort("connectionreset");
    });
    const failure = page.waitForEvent("requestfailed", (r) => r.method() === "POST");
    const getsBefore = t.gets;

    await payButton(page).click();
    expect((await failure).failure()?.errorText).toContain("ERR_CONNECTION_RESET");
    await expect(page.getByText("Payment complete")).toBeVisible();

    expect(evidence).toEqual({ status: 200, bodyStatus: "PAID", backendStatus: "PAID", receipts: 1 });
    expect(t.gets).toBeGreaterThan(getsBefore); // canonical refetch
    expect(t.postKeys).toHaveLength(1); // no automatic retry
    await expect(payButton(page)).toHaveCount(0);
    expect(db.receipts(s.id)).toBe(1);
    expect(await storedKey(page, KEY_PREFIX + s.id)).toBeNull();
  });

  test("a failed attempt's key survives reload and the explicit retry reuses it", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-RELOAD");
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);
    await failNextPay(page, s.id);

    await payButton(page).click();
    await expect(page.getByText("Payment not completed")).toBeVisible();
    const firstKey = t.postKeys[0];
    expect(firstKey).toMatch(UUID_V4);
    expect(await storedKey(page, KEY_PREFIX + s.id)).toBe(firstKey);

    await page.reload();
    await expect(payButton(page)).toBeEnabled();
    expect(await storedKey(page, KEY_PREFIX + s.id)).toBe(firstKey);
    expect(t.postKeys).toHaveLength(1); // reloading never posts

    await payButton(page).click();
    await expect(page.getByText("Payment complete")).toBeVisible();
    expect(t.postKeys).toEqual([firstKey, firstKey]);
    expect(await storedKey(page, KEY_PREFIX + s.id)).toBeNull();
    expect(db.receipts(s.id)).toBe(1);
  });
});

test.describe("connectivity and stale state", () => {
  test("offline payment is unconfirmed; reconnect refetches without posting", async ({ page, context, api }) => {
    const s = await api.createSession("ORDER-E2E-OFFLINE");
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);

    await context.setOffline(true);
    await page.getByRole("button", { name: "Refresh status" }).click();
    await expect(page.getByText("Couldn't refresh the status")).toBeVisible();

    await payButton(page).click();
    await expect(page.getByText(UNCONFIRMED)).toBeVisible();
    await expect(page.getByText("no payment was made")).toHaveCount(0);
    await expect(payButton(page)).toBeDisabled();
    await page.screenshot({ path: shot("desktop-offline-unconfirmed.png"), fullPage: true });
    const savedKey = await storedKey(page, KEY_PREFIX + s.id);
    const postsWhileOffline = t.postKeys.length;
    const getsBefore = t.gets;

    // Listen first (awaited), then reconnect: the real browser event, not a synthetic one.
    await page.evaluate(() => {
      window.addEventListener("online", () => document.body.setAttribute("data-e2e-online", "1"), { once: true });
    });
    await context.setOffline(false);
    await expect(page.locator("body")).toHaveAttribute("data-e2e-online", "1");
    await expect(page.getByText(CONFIRMED_CREATED)).toBeVisible();
    expect(t.gets).toBeGreaterThan(getsBefore);
    expect(t.postKeys).toHaveLength(postsWhileOffline); // no automatic POST
    expect(await api.status(s.id)).toBe("CREATED");

    await payButton(page).click();
    await expect(page.getByText("Payment complete")).toBeVisible();
    expect(t.postKeys.at(-1)).toBe(savedKey);
    expect(db.receipts(s.id)).toBe(1);
  });

  test("stale tabs converge on PAID and only one payment succeeds", async ({ context, api }) => {
    const s = await api.createSession("ORDER-E2E-TABS");
    const [a, b, c] = await Promise.all([context.newPage(), context.newPage(), context.newPage()]);
    const tb = trackRequests(b, s.id);
    for (const p of [a, b, c]) await openCheckout(p, s.id);

    await a.bringToFront();
    await payButton(a).click();
    await expect(a.getByText("Payment complete")).toBeVisible();

    const bGets = tb.gets;
    await b.bringToFront();
    await b.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(b.getByText("Payment complete")).toBeVisible();
    expect(tb.gets).toBeGreaterThan(bGets);
    expect(tb.postKeys).toHaveLength(0);

    // C still shows CREATED and tries to pay: the backend refuses, C refetches.
    const response = c.waitForResponse((r) => r.url().endsWith(`/${s.id}/pay`));
    await payButton(c).click();
    expect((await response).status()).toBe(409);
    await expect(c.getByText("This session has already been paid.")).toBeVisible();
    await expect(c.getByText("Payment complete")).toBeVisible();
    expect(db.receipts(s.id)).toBe(1);
  });

  test("an older CREATED response cannot replace a newer PAID result", async ({ page, api }) => {
    for (const variant of ["paid in this tab", "paid by another client"] as const) {
      const s = await api.createSession(`ORDER-E2E-ORDER-${variant === "paid in this tab" ? "UI" : "API"}`);
      await openCheckout(page, s.id);
      const release = deferred();
      const held = deferred<string>();
      const delivered = deferred();
      let holdNext = true;
      await page.route(`${API_URL}/api/sessions/${s.id}`, async (route) => {
        if (route.request().method() !== "GET" || !holdNext) return route.continue();
        holdNext = false;
        const old = await route.fetch(); // real CREATED snapshot, delivered late
        held.resolve((await old.json()).status);
        await release.promise;
        await route.fulfill({ response: old }).catch(() => {}); // the app may have aborted it
        delivered.resolve();
      });

      await page.getByRole("button", { name: "Refresh status" }).click();
      expect(await held.promise).toBe("CREATED");
      if (variant === "paid in this tab") {
        await payButton(page).click();
      } else {
        await fetch(`${API_URL}/api/sessions/${s.id}/pay`, {
          method: "POST",
          headers: { "Idempotency-Key": crypto.randomUUID() },
        });
        await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      }
      await expect(page.getByText("Payment complete")).toBeVisible();

      release.resolve();
      await delivered.promise; // the stale response has been handed to the page (or refused)
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0))));
      await expect(page.getByText("Payment complete")).toBeVisible();
      await expect(payButton(page)).toHaveCount(0);
      await page.unroute(`${API_URL}/api/sessions/${s.id}`);
    }
  });

  test("focus/online/visibility events during a pending payment are deferred and coalesced", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-PENDING");
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);
    const release = deferred();
    const postSeen = deferred();
    await page.route(`${API_URL}/api/sessions/${s.id}/pay`, async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      postSeen.resolve();
      await release.promise;
      await route.continue();
    });

    await payButton(page).click();
    await postSeen.promise;
    const getsDuringPending = t.gets;
    await page.evaluate(() => {
      for (let i = 0; i < 3; i++) {
        window.dispatchEvent(new Event("focus"));
        window.dispatchEvent(new Event("online"));
        document.dispatchEvent(new Event("visibilitychange"));
      }
      [...document.querySelectorAll("button")].find((b) => /^(Pay|Processing)/.test(b.textContent ?? ""))?.click();
    });
    await page.evaluate(() => new Promise((r) => setTimeout(r, 0))); // let any scheduled recovery run
    expect(t.gets).toBe(getsDuringPending);
    expect(t.postKeys).toHaveLength(1);

    const queuedRefresh = page.waitForResponse((r) => r.url() === `${API_URL}/api/sessions/${s.id}`);
    release.resolve();
    await expect(page.getByText("Payment complete")).toBeVisible();
    await queuedRefresh;
    expect(t.gets - getsDuringPending).toBe(1);
    await expect(page.getByText("Payment complete")).toBeVisible();
    expect(db.receipts(s.id)).toBe(1);
  });
});

test.describe("payment-key storage failures", () => {
  const otherSessionId = "00000000-0000-4000-8000-000000000999";
  const otherSessionKey = "11111111-2222-4333-8444-555555555555";

  const cases = {
    "sessionStorage unavailable": () => {
      Object.defineProperty(window, "sessionStorage", {
        configurable: true,
        get() {
          throw new DOMException("Storage disabled", "SecurityError");
        },
      });
    },
    "sessionStorage quota exceeded": () => {
      Storage.prototype.setItem = function () {
        throw new DOMException("Full", "QuotaExceededError");
      };
    },
  };

  for (const [name, script] of Object.entries(cases)) {
    test(`${name}: page works and retries keep one in-memory key`, async ({ page, api }) => {
      await page.addInitScript(script);
      const s = await api.createSession("ORDER-E2E-STORAGE");
      const t = trackRequests(page, s.id);
      await openCheckout(page, s.id);
      await failNextPay(page, s.id);
      await payButton(page).click();
      await expect(page.getByText("Payment not completed")).toBeVisible();
      await payButton(page).click();
      await expect(page.getByText("Payment complete")).toBeVisible();
      expect(t.postKeys).toHaveLength(2);
      expect(t.postKeys[0]).toMatch(UUID_V4);
      expect(t.postKeys[1]).toBe(t.postKeys[0]);
      expect(db.receipts(s.id)).toBe(1);
    });
  }

  test("malformed and foreign entries are ignored; only this session's entry is removed", async ({ page, api }) => {
    const s = await api.createSession("ORDER-E2E-MALFORMED");
    await page.addInitScript(
      ({ prefix, id, otherId, otherKey }) => {
        if (sessionStorage.getItem("e2e-seeded")) return;
        sessionStorage.setItem("e2e-seeded", "1");
        sessionStorage.setItem(prefix + id, "not-a-uuid");
        sessionStorage.setItem(prefix + otherId, otherKey);
        sessionStorage.setItem("unrelated:keep", "yes");
      },
      { prefix: KEY_PREFIX, id: s.id, otherId: otherSessionId, otherKey: otherSessionKey },
    );
    const t = trackRequests(page, s.id);
    await openCheckout(page, s.id);
    await payButton(page).click();
    await expect(page.getByText("Payment complete")).toBeVisible();

    expect(t.postKeys[0]).toMatch(UUID_V4);
    expect(t.postKeys[0]).not.toBe(otherSessionKey);
    expect(await storedKey(page, KEY_PREFIX + s.id)).toBeNull();
    expect(await storedKey(page, KEY_PREFIX + otherSessionId)).toBe(otherSessionKey);
    expect(await storedKey(page, "unrelated:keep")).toBe("yes");
  });
});

test("mobile: unconfirmed state fits and recovers", async ({ newContext, api }) => {
  const context = await newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  const page = await context.newPage();
  const s = await api.createSession("ORDER-E2E-MOBILE-RECOVERY");
  await openCheckout(page, s.id);
  await context.setOffline(true);
  await payButton(page).tap();
  await expect(page.getByText(UNCONFIRMED)).toBeVisible();
  expect(await noHorizontalOverflow(page)).toBe(true);
  await page.screenshot({ path: shot("mobile-unconfirmed.png"), fullPage: true });
  await context.setOffline(false);
  await expect(page.getByText(CONFIRMED_CREATED)).toBeVisible();
  await payButton(page).tap();
  await expect(page.getByText("Payment complete")).toBeVisible();
});
