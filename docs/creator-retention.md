# Creator negotiation retention

Use the retention command for one creator workspace at a time. Agree on the minimum time to keep a closed negotiation and the relay's maximum replay window before setting the two day counts. The replay window must cover retries and operator redrives at the relay and its upstream providers. Verify the selected database and workspace and check for any holds before applying the batch.

## Preview and apply

From `backend/`, provide `DATABASE_URL` through the approved runtime configuration and set the workspace ID and approved day counts. Run a read-only preview first.

```bash
uv run python -m app.retention "$CREATOR_WORKSPACE_ID" \
  --retention-days "$CLOSED_RETENTION_DAYS" --replay-days "$RELAY_REPLAY_DAYS" --limit 100
```

The JSON report includes the number of eligible negotiations in this batch and the number of associated analysis jobs with checkpoint data. To erase the batch, run the command with the same arguments and `--apply`. The apply command selects candidates again and rechecks each one under lock. It reports committed deletions and skips caused by concurrent activity or a change in eligibility. Capture the workspace, policy values, execution time and counts in the operations record. If the command is interrupted, preview again to see which negotiations remain.

## Eligibility and deletion scope

- The latest close must be recorded for the current negotiation revision and older than both day counts. Reopening and closing starts a new retention clock. A missing close record keeps the negotiation in place.
- A queued, running or failed job blocks deletion. An approval without an authoritative delivery outcome also blocks deletion. Accepted delivery requires a matching provider receipt. A confirmed rejection or a local pre-dispatch block can qualify once its replay window has elapsed. An ambiguous handoff requires provider reconciliation before it qualifies. [Real-mail reconciliation](real-mail-integration.md)
- Inbound event identities and their original receipts stay available through the replay window after message receipt. Outbound attempt state and receipts stay available through the replay window after the last recorded outcome. Once the negotiation is erased, its route is removed and a callback to that address has no matching route. Retire those inbound addresses permanently at the relay and when creating new routes.
- Each deletion uses one PostgreSQL transaction, locked in tenant-then-negotiation order. The operator also takes the worker's job advisory locks and the delivery reconciler's approval advisory locks. A held lock defers that negotiation to a later run. The transaction removes the analysis checkpoint records, blobs and writes, associated preference evidence, negotiation, mail route, messages, offers, drafts, approvals, delivery attempts and receipts, timeline and jobs. Workspace access, creator policies and registered source briefs have their own lifecycle.
- Routine worker housekeeping expires up to 1,000 daily admission counters older than 31 days per pass using UTC dates. Creator negotiation deletion requires the operator command with `--apply` on every invocation.

## External copies and restoration

Set separate retention and deletion policies for backups, relay records, model-provider data and tracing. Review each system's settings and deletion evidence before onboarding creators. A database restore can reintroduce erased negotiations and pending sends. Keep the restored relay and workers disabled, review which recorded deletions need to be reapplied, and reconcile existing provider handoffs before resuming delivery. [Backup and restore](operations.md#backup-and-restore)
