import { expect, test, type BrowserContext } from "@playwright/test";

const thread = { id: "first", label: "Reference negotiation", revision: 0, status: "NEW" };
const policy = {
  currency: "USD", rates: { reel: 200000 }, minimum_reel: 150000, exclusivity_pct: 30,
  lead_days: 7, max_exclusive_days: 90, allow_paid_usage: true, tone: "warm",
};
const empty = { fault: null, messages: [], drafts: [], deliveries: [], timeline: [], recoverable_turns: [] };

async function workspace(context: BrowserContext, mode = "scripted", evaluations = true) {
  await context.route("**/api/session", (route) => route.fulfill({
    json: { mode, threads: [thread], capabilities: { evaluations, simulated_failures: mode === "scripted" } },
  }));
  await context.route("**/api/profile", (route) => route.fulfill({
    json: { version: 1, data: policy, suggested_tone: null },
  }));
  await context.route("**/api/negotiations/first", (route) => route.fulfill({ json: { ...thread, ...empty } }));
}

test("two tabs keep the policy version attached to the edited form", async ({ context, page }) => {
  await workspace(context);
  let version = 1;
  let savedPolicy = policy;
  let versionSubmitted = 0;
  await context.route("**/api/profile", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: { version, data: savedPolicy, suggested_tone: null } });
      return;
    }
    const payload = route.request().postDataJSON();
    versionSubmitted = payload.version;
    if (payload.version !== version) await route.fulfill({ status: 409, json: { detail: "Policy changed." } });
    else {
      savedPolicy = payload.policy;
      await route.fulfill({ json: { version: ++version } });
    }
  });
  let delivered = false;
  await page.route("**/api/negotiations/first", (route) => route.fulfill({
    json: { ...thread, ...empty, deliveries: delivered ? [{ body: "Delivered", created_at: "2026-11-25T12:00:00Z", delivery_key: "1" }] : [] },
  }));
  const other = await context.newPage();
  await page.goto("/");
  await other.goto("/");
  await page.getByRole("button", { name: "Edit policy", exact: true }).click();
  await page.getByLabel("Exclusivity surcharge (%)").fill("55");
  await other.getByRole("button", { name: "Edit policy", exact: true }).click();
  await other.getByLabel("Reel rate", { exact: true }).fill("2200");
  await other.getByRole("button", { name: "Save policy" }).click();
  await expect(other.getByText("$2,200", { exact: true })).toBeVisible();
  const refreshed = page.waitForResponse((response) => response.url().endsWith("/api/profile"));
  delivered = true;
  // Read once on return to the tab, which also exercises polling's resume path.
  await page.bringToFront();
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await refreshed;
  await page.getByRole("button", { name: "Save policy" }).click();
  await expect(page.getByRole("button", { name: "Load latest policy" })).toBeVisible();
  expect(versionSubmitted).toBe(1);
  await expect(page.getByLabel("Exclusivity surcharge (%)")).toHaveValue("55");
  await page.getByRole("button", { name: "Load latest policy" }).click();
  await expect(page.getByLabel("Reel rate", { exact: true })).toHaveValue("2200");
  await expect(page.getByLabel("Exclusivity surcharge (%)")).toHaveValue("30");
});

test("opens a committed creation when a subsequent read fails", async ({ context, page }) => {
  await workspace(context);
  let eventId: string | undefined;
  await page.route("**/api/negotiations", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ status: 503, json: { detail: "Refresh failed." } });
    else {
      eventId = route.request().postDataJSON().event_id;
      await route.fulfill({ json: { id: "created" } });
    }
  });
  await page.route("**/api/negotiations/created", (route) => route.fulfill({ status: 503, json: { detail: "Refresh failed." } }));
  await page.goto("/");
  await page.getByRole("button", { name: "New", exact: true }).click();
  await page.getByLabel("Negotiation name").fill("Summer campaign");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByLabel("Negotiation name")).toHaveCount(0);
  await expect(page.getByLabel("Negotiation", { exact: true })).toHaveValue("created");
  await expect(page.getByRole("status").filter({ hasText: "Negotiation created" })).toBeVisible();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Refresh failed.");
  expect(eventId).toBeTruthy();
});

test("can retry an interrupted evaluation read using the stored run ID", async ({ context, page }) => {
  await workspace(context);
  await page.addInitScript(() => sessionStorage.setItem("dealroom-evaluation", "existing-run"));
  let reads = 0;
  await page.route("**/api/evaluations/existing-run", async (route) => {
    if (++reads === 1) await route.fulfill({ status: 503, json: { detail: "Evaluation read failed." } });
    else await route.fulfill({ json: {
      id: "existing-run", status: "COMPLETE", mode: "scripted",
      result: { fixture_version: "v1", metrics: {}, scenarios: [] },
    } });
  });
  await page.goto("/evaluations");
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Evaluation read failed.");
  await expect(page.getByRole("button", { name: "Run evaluation suite" })).toBeDisabled();
  await expect(page.getByRole("region", { name: "Scenario results" })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("dealroom-evaluation"))).toBe("existing-run");
});

test("displays available evaluation actions from session capabilities", async ({ context, page }) => {
  await workspace(context, "live", false);
  await page.goto("/evaluations");
  await expect(page.getByText("Evaluations are disabled in this environment.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run evaluation suite" })).toBeDisabled();
});

test("gates simulated fault injection and exposes predecessor recovery", async ({ context, page }) => {
  await workspace(context, "live", false);
  await page.route("**/api/negotiations/first", (route) => route.fulfill({ json: {
    ...thread, ...empty, status: "FAILED", revision: 2, recoverable_turns: [{ job_id: "older-job", revision: 1 }],
  } }));
  await page.route("**/api/negotiations/first/recovery", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.goto("/");
  await page.getByRole("button", { name: "Recovery controls" }).click();
  await expect(page.getByRole("button", { name: "Fail after checkpoint" })).toHaveCount(0);
  const request = page.waitForRequest("**/api/negotiations/first/recovery");
  await page.getByRole("button", { name: "Skip revision 1" }).click();
  expect((await request).postDataJSON()).toEqual({ action: "skip", job_id: "older-job", revision: 2 });
});

test("polling revalidates with the server ETag and pauses while hidden", async ({ context, page }) => {
  await workspace(context);
  const tags: (string | undefined)[] = [];
  await page.route("**/api/negotiations/first", async (route) => {
    const tag = route.request().headers()["if-none-match"];
    tags.push(tag);
    if (tag === '"initial"') await route.fulfill({ status: 304, headers: { etag: tag } });
    else await route.fulfill({ json: { ...thread, ...empty }, headers: { etag: '"initial"' } });
  });
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Negotiation thread" })).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.waitForTimeout(1200);
  expect(tags).toHaveLength(1);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => tags.at(-1)).toBe('"initial"');
  await expect(page.getByRole("region", { name: "Negotiation thread" })).toBeVisible();
});

test("rejects a malformed success response without losing the workspace", async ({ context, page }) => {
  await workspace(context);
  await page.route("**/api/negotiations/first", (route) => route.fulfill({
    json: { ...thread, ...empty, messages: null },
  }));
  await page.goto("/");
  await expect(page.getByRole("main").getByRole("alert")).toContainText("The server returned an invalid response.");
  await expect(page.getByLabel("Negotiation", { exact: true })).toHaveValue("first");
});

test("keeps mobile keyboard navigation in visual panel order", async ({ context, page }) => {
  await workspace(context);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Recovery controls" })).toBeVisible();
  await page.getByRole("button", { name: "New", exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Recovery controls" })).toBeFocused();
});

test("keeps a draft through expiry and reconnects to an accessible thread", async ({ context, page }) => {
  await workspace(context);
  let expired = false;
  let opened = 0;
  await context.route("**/api/session", (route) => {
    opened += 1;
    expired = false;
    return route.fulfill({ json: { mode: "scripted", threads: [thread] } });
  });
  await page.route("**/api/negotiations/first", (route) => expired
    ? route.fulfill({ status: 401, json: { detail: "Start a new sandbox session." } })
    : route.fulfill({ json: { ...thread, ...empty } }));
  await page.goto("/");
  const composer = page.getByLabel("Simulate a brand email");
  await composer.fill("An unsent synthetic offer");
  expired = true;
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.getByRole("button", { name: "Reconnect" }).click();
  await expect(composer).toHaveValue("An unsent synthetic offer");
  await expect(page.getByRole("button", { name: "Add to thread" })).toBeEnabled();
  expect(opened).toBe(2);
});

test("retries an ambiguous thread creation with the original event ID", async ({ context, page }) => {
  await workspace(context);
  const events: string[] = [];
  await page.route("**/api/negotiations", (route) => {
    events.push(route.request().postDataJSON().event_id);
    return events.length === 1 ? route.fulfill({ status: 502, json: { detail: "Write status unknown." } })
      : route.fulfill({ json: { id: "created" } });
  });
  await page.route("**/api/negotiations/created", (route) => route.fulfill({ json: { ...thread, ...empty, id: "created" } }));
  await page.goto("/");
  await page.getByRole("button", { name: "New", exact: true }).click();
  await page.getByLabel("Negotiation name").fill("Retry this campaign");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Write status unknown.");
  await expect(page.getByLabel("Negotiation name")).toHaveValue("Retry this campaign");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByLabel("Negotiation", { exact: true })).toHaveValue("created");
  expect(events).toEqual([expect.stringMatching(/^[0-9a-f-]+$/), events[0]]);
});

test("keeps older and current events together after loading timeline history", async ({ context, page }) => {
  await workspace(context);
  const date = "2026-11-25T12:00:00Z";
  const event = (id: number, kind: string) => ({ id, kind, revision: 1, details: {}, created_at: date });
  await page.route("**/api/negotiations/first", (route) => route.fulfill({ json: {
    ...thread, ...empty, timeline_has_more: true, timeline: [event(4, "draft_created"), event(5, "draft_approved")],
  } }));
  await page.route("**/api/negotiations/first/timeline?before=4", (route) => route.fulfill({
    json: { has_more: false, timeline: [event(2, "inbound_received"), event(3, "offer_extracted")] },
  }));
  await page.goto("/");
  await page.getByRole("button", { name: "Load older events" }).click();
  const timeline = page.getByRole("list", { name: "Event timeline" });
  await expect(timeline.getByRole("listitem")).toHaveCount(4);
  await expect(timeline.getByRole("listitem").first()).toContainText("Creator approved draft");
  await expect(timeline.getByRole("listitem").last()).toContainText("Brand email received");
});

test("approval includes the visible mail envelope digest when supplied", async ({ context, page }) => {
  await workspace(context);
  await page.route("**/api/negotiations/first", (route) => route.fulfill({ json: {
    ...thread, ...empty, status: "AWAITING_APPROVAL", revision: 1, drafts: [{
      id: "draft-one", version: 2, revision: 1, policy_version: 1, status: "PENDING",
      body: "Thanks for sending your offer.", created_at: "2026-11-25T12:00:00Z",
      decision: { action: "rejection", total: null, base: null, surcharge: null, currency: null,
        questions: [], checks: ["Paid usage is outside the creator's policy."], items: {}, advertising: true },
      proposal: { tone: "warm", invite_followup: true }, mail: {
        sender: "creator@example.test", recipient: "brand@example.test", subject: "Re: Test brand offer", digest: "current-digest",
      },
    }],
  } }));
  await page.route("**/api/negotiations/first/drafts/draft-one", (route) => route.fulfill({ json: { id: "draft-one" } }));
  await page.goto("/");
  const draft = page.getByRole("article").filter({ hasText: "Agent draft" });
  await expect(draft.getByText("brand@example.test", { exact: true })).toBeVisible();
  await expect(draft.getByText("Re: Test brand offer", { exact: true })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Agent inspector" }).getByText("Blocked", { exact: true })).toBeVisible();
  const request = page.waitForRequest("**/api/negotiations/first/drafts/draft-one");
  await draft.getByRole("button", { name: "Approve & send", exact: true }).click();
  expect((await request).postDataJSON()).toEqual({ action: "approve", version: 2, mail_digest: "current-digest" });
});

test("verified creator workspaces hide synthetic inbound and fault controls", async ({ context, page }) => {
  await workspace(context, "live", false);
  await context.route("**/api/session", (route) => route.fulfill({ json: {
    mode: "live", workspace_kind: "creator", threads: [thread],
    capabilities: { evaluations: false, simulated_failures: false },
  } }));
  await page.goto("/");
  const negotiation = page.getByRole("region", { name: "Negotiation thread" });
  await expect(negotiation).toBeVisible();
  const profile = page.getByRole("complementary", { name: "Creator profile" });
  await expect(profile.getByText("Maya Rivers", { exact: true })).toHaveCount(0);
  await expect(negotiation.getByLabel("Simulate a brand email")).toHaveCount(0);
  await expect(negotiation.getByRole("button", { name: "Load example offer" })).toHaveCount(0);
  await expect(negotiation.getByText("Waiting for a brand email")).toBeVisible();
  await negotiation.getByRole("button", { name: "Recovery controls" }).click();
  await expect(negotiation.getByRole("button", { name: "Fail after checkpoint" })).toHaveCount(0);
});
