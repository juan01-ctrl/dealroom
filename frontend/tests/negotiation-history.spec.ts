import { expect, test } from "@playwright/test";

test("loads earlier negotiation history while a new offer arrives and restores keyboard focus", async ({ page }) => {
  const thread = { id: "history-fixture", label: "Long campaign", revision: 4, status: "AWAITING_APPROVAL" };
  const created_at = "2026-11-26T13:00:00Z";
  const messages = (revisions: number[]) => revisions.map((revision) => ({
    revision, body: `Incoming offer ${revision}`, created_at,
  }));
  const draft = {
    id: "draft-four", revision: 4, version: 1, body: "Current counteroffer",
    status: "PENDING", created_at, policy_version: 1,
    decision: {
      action: "counteroffer", total: 250000, base: 250000, surcharge: 0, currency: "USD",
      questions: [], checks: [], items: {}, advertising: false,
    },
    proposal: { tone: "warm", invite_followup: true },
  };
  let arrived = false;
  let confirmed = false;
  const historyCursor = "3:2147483648:00000000-0000-0000-0000-000000000000";
  const inbox = { revision: 2, delivery_key: "older-delivery", body: "Delivered earlier reply", created_at };
  let releaseHistory!: () => void;
  const waiting = new Promise<void>((resolve) => { releaseHistory = resolve; });
  await page.route("**/api/session", (route) => route.fulfill({ json: {
    threads: [thread], mode: "scripted",
  } }));
  await page.route("**/api/profile", (route) => route.fulfill({ json: {
    version: 1, suggested_tone: null, data: {
      currency: "USD", rates: {}, minimum_reel: 150000, exclusivity_pct: 30,
      lead_days: 14, max_exclusive_days: 90, allow_paid_usage: true, tone: "warm",
    },
  } }));
  await page.route(/\/api\/negotiations\/history-fixture(?:\?history=paged)?$/, (route) => route.fulfill({
    headers: { ETag: confirmed ? '"confirmed"' : arrived ? '"arrived"' : '"initial"' },
    json: {
      ...thread,
      ...(arrived ? { revision: 5, status: "ANALYZING" } : {}),
      fault: null, messages: messages(arrived ? [4, 5] : [3, 4]),
      drafts: [confirmed ? { ...draft, status: "DELIVERY_FAILED" } : arrived ? { ...draft, status: "STALE" } : draft],
      deliveries: [], timeline: [], recoverable_turns: [{ job_id: "failed-turn", revision: 1 }],
      history_cursor: arrived ? "4:1:7061d44a-9559-491a-82f0-560713683ad8" : historyCursor,
      ...(confirmed && { recent_receipts: [{
        ...inbox, revision: 1, draft_id: "draft-one", delivery_key: "confirmed-old-delivery", body: "Late confirmed reply",
      }] }),
    },
  }));
  await page.route("**/api/negotiations/history-fixture/history?before=*", async (route) => {
    expect(new URL(route.request().url()).searchParams.get("before")).toBe(historyCursor);
    arrived = true;
    await waiting;
    await route.fulfill({ json: {
      messages: messages([1, 2]), drafts: [{ ...draft, id: "draft-one", revision: 1, status: "STALE" }],
      deliveries: [inbox], history_cursor: null,
    } });
  });

  await page.goto("/");
  const conversation = page.getByRole("region", { name: "Negotiation thread" });
  await expect(conversation.getByRole("button", { name: "Approve & simulate send" })).toBeVisible();
  const load = conversation.getByRole("button", { name: "Load earlier history" });
  await load.focus();
  await page.keyboard.press("Enter");
  try {
    await expect(conversation.getByText("Incoming offer 5", { exact: true })).toBeVisible();
  } finally {
    releaseHistory();
  }
  await expect(conversation.getByRole("group", { name: "Revision 1" })).toBeFocused();
  for (const revision of [1, 2, 3, 4, 5]) {
    await expect(conversation.getByText(`Incoming offer ${revision}`, { exact: true })).toHaveCount(1);
  }
  await expect(load).toHaveCount(0);
  await expect(conversation.getByRole("button", { name: "Approve & simulate send" })).toHaveCount(0);
  await conversation.getByText("Simulated brand inbox").click();
  await expect(conversation.getByText(/Delivered · Revision 2/)).toBeVisible();
  await expect(conversation.getByText("Delivered earlier reply")).toBeVisible();
  confirmed = true;
  await expect(conversation.getByText("Late confirmed reply", { exact: true })).toBeVisible();
  await expect(conversation.getByRole("group", { name: "Revision 1" }).getByText("Sent", { exact: true })).toBeVisible();
  await expect(conversation.getByText("Delivery failed", { exact: true })).toBeVisible();
  await conversation.getByRole("button", { name: "Recovery controls" }).click();
  await expect(conversation.getByRole("button", { name: "Retry revision 1" })).toBeVisible();
});
