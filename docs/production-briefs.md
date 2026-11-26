# Registered campaign briefs

The operator registers reviewed commercial terms for one creator workspace. When an authenticated inbound mail references that exact HTTPS URL, the worker looks up the latest active revision in the workspace that owns the negotiation. The URL serves as a key in PostgreSQL. There are no external requests or document downloads. Sandbox sessions continue to use the bundled demo catalog.

## Register a revision

Apply the database migrations and confirm the target creator workspace. Review the source and transcribe supported commercial terms into a local JSON file.

```json
{
  "document_id": "summer-launch",
  "source_url": "https://brand.example.com/campaigns/summer-launch",
  "revision": 1,
  "title": "Summer launch",
  "sections": [
    { "id": "exclusivity", "exclusive_days": 30 },
    { "id": "digital-ads", "usage_days": 45, "usage_scope": "paid social" }
  ]
}
```

From the backend directory, with `DATABASE_URL` set for the approved environment:

```bash
uv run python -m app.campaign_briefs register <workspace-uuid> brief.json
uv run python -m app.campaign_briefs revoke <workspace-uuid> summer-launch
```

Use an exact HTTPS reference with a lowercase hostname and no embedded credentials, query, or fragment. Use a document URL suitable for email and keep access tokens out of the path. Files are capped at 16 KiB. Each revision has up to 16 uniquely identified sections. Durations range from 1 to 3,650 days. The supported advertising scopes are `paid social`, `paid search`, and `display ads`.

Start at revision 1 and increment by one. The document ID and URL are bound for the document's lifetime. Identical revision replays succeed. Changes require a new revision. Revocation blocks new retrieval and source inspection for all revisions of the document.

## Verify the evidence

Send a synthetic campaign email with the registered URL as one of its first three distinct links. The inspector lists the brief title, exact revision, and section IDs stored with the turn. Its link opens the structured terms at that revision through the same creator membership and negotiation scope as the thread. Unknown, revoked, and other-workspace links yield the same unavailable evidence outcome. Conflicting retrieved values are excluded and the draft requests clarification. The protected analysis timeline associates matching current terms with their supporting document, revision and section. A conflicting value or an email override is not attributed to the brief. These locators are kept out of external traces.

Revisions and revocation affect subsequent source reads. Checkpoint retries can resume from previously collected evidence, and published drafts, extracted terms, and timeline citations remain part of the negotiation record. Treat revision, revocation, and negotiation data deletion as separate operator actions.
