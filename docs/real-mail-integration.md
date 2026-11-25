# Verified creator email integration

The sandbox uses browser sessions and simulated delivery. Verified workspaces use a separate creator identity, explicit membership, authenticated inbound mail, and a provider receipt. The live model setting and real mail switch are independent.

The application includes the contracts for an identity gateway and a mail relay. A production deployment needs operator-provisioned accounts, an approved identity gateway and relay, verified mailboxes, and an end-to-end staging test. The automated suite uses synthetic mail and a deterministic provider.

## Configuration

Set `REAL_MAIL_ENABLED=1` in the API and worker environments when the integration is authorized. Use independent secrets for each credential.

| Service | Setting | Purpose |
| --- | --- | --- |
| API | `CREATOR_IDENTITY_ISSUER` | Expected issuer in the gateway assertion |
| API | `CREATOR_IDENTITY_SECRET` | Shared signing secret with the identity gateway |
| API | `MAIL_PROVIDER` | Lowercase relay identifier used by workspace routes |
| API | `MAIL_INBOUND_SECRET` | Shared signing secret with the inbound relay |
| Worker | `MAIL_PROVIDER` | Same relay identifier configured for the workspace |
| Worker | `MAIL_RELAY_URL` | HTTPS base URL for send and receipt lookup |
| Worker | `MAIL_RELAY_TOKEN` | Scoped relay bearer credential |

The identity and inbound HMAC keys require at least 32 printable ASCII characters. Store credentials with the service's secret manager. Keep the identity assertion signing key at the gateway and API, and the inbound signature key at the relay and API.

## Creator identity and workspace membership

The trusted gateway authenticates the creator with the selected identity provider. It verifies the provider's issuer, audience, signature, session expiry, and verified email before issuing a Dealroom assertion. The assertion is suitable for short-lived bearer use by a gateway or authenticated client. Send it over TLS in `Authorization: Bearer <assertion>` with the selected `X-Workspace-Id`. The normal browser API still uses its existing request protection when reached through the sandbox proxy.

An assertion contains six identity and lifetime claims plus the fixed audience. Encode the UTF-8 JSON as unpadded base64url, then produce a lowercase hexadecimal HMAC-SHA256 of `dealroom-identity-v1.<encoded-json>`. The token format is `<encoded-json>.<signature>`.

| Claim | Value |
| --- | --- |
| `iss` | Exact configured issuer |
| `aud` | `dealroom-creator` |
| `sub` | Stable account identifier from the identity provider |
| `email` | Verified mailbox |
| `email_verified` | JSON boolean `true` |
| `iat`, `exp` | Unix seconds with a lifetime of at most 300 seconds |

After verifying the assertion, the API resolves its issuer, subject, and email against an active creator account and membership for the selected workspace. Account and membership rows are operator-managed. `IdentityStore.provision_workspace(verified_claims)` creates a dedicated workspace and owner membership. Grant additional membership only for independently verified accounts. A workspace has owner, approver, and viewer roles. Owner and approver memberships may approve mail. Disable a membership to revoke access while retaining its approval attribution.

## Inbound mail

Use `MailStore.configure_route` from an authorized operator environment to bind a workspace thread to a provider, inbound alias, verified sending mailbox, and a confirmed counterpart address. The relay verifies the upstream webhook signature, reads only plain-text content, establishes sender authenticity for the configured counterpart, and uses the actual envelope recipient to route the message. Treat the email subject and body as untrusted content. A display name, `Reply-To`, or claimed `From` alone cannot establish sender authenticity.

The relay sends a JSON request to `POST /integrations/mail/inbound` of at most 16,384 bytes. It includes a Unix timestamp in `X-Mail-Timestamp` and the lowercase hexadecimal HMAC-SHA256 in `X-Mail-Signature`. Sign the byte concatenation of `dealroom-inbound-v1.`, the exact timestamp header, `.`, and the raw request body. The signature is valid within 300 seconds of the API clock.

```json
{
  "provider": "relay",
  "event_id": "stable-upstream-event-identifier",
  "to": "campaign@receive.example.test",
  "from_address": "brand@example.test",
  "sender_authenticated": true,
  "subject": "Reel campaign",
  "body": "We can offer $3,000 for one reel."
}
```

Retries reuse the same provider event ID and normalized payload. Each retry may have a newly signed timestamp. Exact retries return the original revision and job ID. A changed payload under the same provider event ID is a conflict. The API routes by the provider and enrolled mailbox pair, so the relay cannot select a workspace or negotiation in its payload.

## Review and delivery

The draft API includes the sender, recipient, reply subject, and a digest alongside the exact draft body. A real-mail approval must submit the displayed digest as `mail_digest`. The transaction stores the signed-in approver's account ID and the envelope identity together with the existing draft, policy, and thread versions. The worker compares the exact stored payload against the current route and current draft before beginning provider handoff.

The relay's `POST /deliveries` receives the provider idempotency key in the `Idempotency-Key` header and the JSON `idempotency_key` field. The JSON also contains `from`, `to`, `subject`, and `text`. The relay must send from the configured verified mailbox, preserve the exact approved recipient, subject, and text, and durably bind the key to those fields. A duplicate key with a different payload must be rejected before any send. Route changes require reviewing a fresh bound envelope.

`GET /deliveries/{idempotency_key}` looks up an existing attempt. Both calls use the scoped bearer credential. Both success responses echo the `idempotency_key` and return a `state` field.

| State | Required relay behavior |
| --- | --- |
| `accepted` | Includes `receipt_id`, a stable identifier confirming upstream acceptance for the exact bound payload |
| `rejected` | Definitive evidence that this key did not send and cannot send through the attempted operation |
| `unknown` | Acceptance has yet to be established |

Roll out the real-mail-compatible API and all workers before enrolling a real workspace. Older analysis and maintenance processes assume demo data.

The worker commits an attempt before first dispatch. A timeout, lost response, process exit, or unavailable lookup leaves it in `PENDING_CONFIRMATION`. Subsequent worker executions perform receipt lookup using the original key. An unresolved attempt blocks a new approval in the same negotiation until the relay confirms the earlier outcome. A 404 or malformed response supplies no delivery evidence. Provider acceptance records the receipt and advances the thread only if its approved draft is still current. Treat upstream receipt acceptance as a handoff status, distinct from recipient inbox delivery or a read acknowledgment.

Operators can reconcile a pending attempt after automatic retries are exhausted using the worker environment.

```sh
python -m app.mail_delivery <workspace-uuid> <thread-uuid> <approval-uuid>
```

The command queries the provider for the recorded attempt. If the relay has no evidence of the outcome, keep the attempt open while the provider investigates. A crash after the first durable attempt record and before its network call can also leave a pending attempt. Only an authoritative provider rejection can close it without an acceptance receipt.

## Staging acceptance

Before enrolling real commercial mail, confirm the identity provider and gateway configuration, review membership revocation and operator access, and verify sender authorization with the selected relay. Exercise deduplication, changed recipients, relay rejection, and a lost provider response. Confirm the relay and its upstream provider meet the workspace's retention and deletion requirements. The local suite covers the application's boundary and failure behavior independently of those account settings.
