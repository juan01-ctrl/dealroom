import { expect, test } from "@playwright/test";

test("creator approves the reviewed version and negotiation survives a reload", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Load example offer" }).click();
  await page.getByRole("button", { name: "Add to thread" }).click();
  const thread = page.getByRole("region", { name: "Negotiation thread" });
  await expect(thread.getByText(/Hi Jordan,/)).toBeVisible();
  await expect(thread.getByText(/USD 2,600\.00/)).toBeVisible();
  await expect(thread.getByText(/45-day paid social license/)).toBeVisible();
  await expect(thread.getByText("Northstar Summer Launch | Reel partnership", { exact: true })).toBeVisible();
  await expect(thread.getByText("Jordan Lee <jordan@northstar.example>", { exact: true })).toBeVisible();
  const link = thread.getByRole("link", { name: /briefs.dealroom.example/ });
  await expect(link).toHaveAttribute("href", "/api/briefs/northstar-summer-launch");
  const doc = await page.request.get(await link.getAttribute("href") || "");
  expect(doc.ok()).toBeTruthy();
  expect(await doc.text()).toContain("Paid usage: 45 days");
  await expect(page.getByText("Linked brief evidence")).toBeVisible();
  await expect(page.getByText("Revision 2026-11-25.1")).toBeVisible();
  await expect(thread.getByText(/USD 1,500/)).toHaveCount(0);
  await expect(thread.getByText("Simulated brand inbox")).toHaveCount(0);
  await thread.getByRole("button", { name: "Edit draft" }).click();
  await thread.getByLabel("Tone").selectOption("formal");
  await thread.getByRole("button", { name: "Save new version" }).click();
  await expect(thread.getByText(/Version 2 · Counteroffer/)).toBeVisible();
  await thread.getByRole("button", { name: "Recovery controls" }).click();
  await thread.getByRole("button", { name: "Fail after delivery" }).click();
  await thread.getByRole("button", { name: "Approve & simulate send" }).click();
  await expect(thread.getByText("Simulated brand inbox")).toBeVisible();
  await expect(page.getByText("Recovered from failure")).toBeVisible();
  await expect(page.getByText("Suggested tone: formal.", { exact: false })).toBeVisible();
  await page.reload();
  await thread.getByText("Simulated brand inbox").click();
  await expect(thread.getByText("Hello Jordan,", { exact: false }).last()).toBeVisible();
  await expect(thread.locator(".delivered-message")).toHaveCount(1);
  await thread.getByRole("button", { name: "Load follow-up" }).click();
  await thread.getByRole("button", { name: "Add to thread" }).click();
  await expect(thread.getByText(/USD 2,800\.00/)).toBeVisible();
  await expect(thread.getByRole("button", { name: "Approve & simulate send" })).toBeVisible();
});

test("dashboard runs and expands real evaluation results", async ({ page }) => {
  await page.goto("/evaluations");
  await page.getByRole("button", { name: "Run evaluation suite" }).click();
  await expect(page.getByRole("region", { name: "Scenario results" }).locator("details"))
    .toHaveCount(33, { timeout: 20_000 });
  await expect(page.getByText("Failed", { exact: true })).toHaveCount(0);
  const recovery = page.locator("details.scenario").filter({ hasText: "Failure after simulated delivery" });
  await recovery.locator("summary").click();
  await expect(recovery.getByText("Duplicate deliveries", { exact: true })).toBeVisible();
  await expect(recovery.getByText("Expected", { exact: true })).toBeVisible();
  await expect(recovery.getByText("Actual", { exact: true })).toBeVisible();
});

test("dashboard shows a failed assertion and observed value", async ({ page }) => {
  await page.route("**/api/evaluations", async (route) => route.fulfill({ json: { id: "example-run" }}));
  await page.route("**/api/evaluations/example-run", async (route) => route.fulfill({ json: {
    id: "example-run", status: "COMPLETE", mode: "scripted",
    result: { fixture_version: "v1", metrics: { scenario_success: 0 }, scenarios: [{
      id: 1, name: "Fixture failure", passed: false, failures: ["action"], extraction: {}, quality: {},
      checks: { action: { expected: "acceptance", actual: "counteroffer", passed: false } },
    }] },
  }}));
  await page.goto("/evaluations");
  await page.getByRole("button", { name: "Run evaluation suite" }).click();
  await page.getByText("Fixture failure", { exact: true }).click();
  await expect(page.getByText("Failed checks: action", { exact: true })).toBeVisible();
  await expect(page.getByText('"counteroffer"', { exact: true })).toBeVisible();
});

test("mobile workspace remains usable without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Load example offer" }).click();
  await expect(page.getByRole("button", { name: "Add to thread" })).toBeEnabled();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
});
