"""Verified inbound events, approval-bound envelopes, and durable provider handoff."""

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from time import time
from typing import Literal, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from pydantic import Field, ValidationError, field_validator

from . import storage
from .configuration import ConfigurationError
from .domain import Input
from .identity import active_approver, integration_secret, mailbox, valid_hmac

MAX_INBOUND_BYTES = 16_384
_PROVIDER = r"[a-z][a-z0-9_-]{0,39}"


class MailAuthenticationError(ValueError):
    """Inbound mail could not be verified."""


class MailDeliveryPending(RuntimeError):
    """An attempt's outcome remains uncertain and requires receipt lookup."""


class InboundMail(Input):
    provider: str = Field(pattern=rf"^{_PROVIDER}$")
    event_id: str = Field(min_length=1, max_length=256, pattern=r"^[!-~]+$")
    to: str
    from_address: str
    sender_authenticated: bool
    subject: str = Field(min_length=1, max_length=240)
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("to", "from_address")
    @classmethod
    def validate_address(cls, value):
        return mailbox(value)

    @field_validator("sender_authenticated")
    @classmethod
    def require_verified_sender(cls, value):
        if not value:
            raise ValueError("The upstream sender must be authenticated.")
        return value

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value):
        if not value.strip() or not value.isprintable():
            raise ValueError("Use a single-line subject.")
        return value

    @field_validator("body")
    @classmethod
    def validate_body(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("Use plain-text message content.")
        return value


class InboundGateway:
    def __init__(self, secret: str, provider: str, *, clock=time):
        self.secret = integration_secret(secret, "MAIL_INBOUND_SECRET")
        if not re.fullmatch(_PROVIDER, provider):
            raise ConfigurationError("Set MAIL_PROVIDER to a lowercase provider identifier.")
        self.provider = provider
        self.clock = clock

    def verify(self, raw: bytes, timestamp: str, signature: str) -> InboundMail:
        if (
            not raw
            or len(raw) > MAX_INBOUND_BYTES
            or not isinstance(timestamp, str)
            or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or abs(int(self.clock()) - int(timestamp)) > 300
        ):
            raise MailAuthenticationError("Inbound mail authentication failed.")
        if not valid_hmac(
            self.secret, b"dealroom-inbound-v1." + timestamp.encode("ascii") + b"." + raw, signature
        ):
            raise MailAuthenticationError("Inbound mail authentication failed.")
        try:
            payload = InboundMail.model_validate_json(raw)
        except ValidationError:
            raise MailAuthenticationError("Inbound mail authentication failed.") from None
        if payload.provider != self.provider:
            raise MailAuthenticationError("Inbound mail authentication failed.")
        return payload


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Envelope:
    provider: str
    sender: str
    recipient: str
    subject: str
    body: str

    @property
    def digest(self):
        return _fingerprint(asdict(self))

    def review_fields(self):
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "subject": self.subject,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class ProviderResult:
    state: Literal["accepted", "rejected", "unknown"]
    receipt_id: str | None = None

    def __post_init__(self):
        if self.state not in {"accepted", "rejected", "unknown"}:
            raise ValueError("Invalid provider outcome.")
        if self.state == "accepted":
            if not isinstance(self.receipt_id, str) or not re.fullmatch(
                r"[!-~]{1,256}", self.receipt_id
            ):
                raise ValueError("An accepted result requires a provider receipt identifier.")
        elif self.receipt_id is not None:
            raise ValueError("A provider receipt requires acceptance.")


class MailProvider(Protocol):
    name: str

    def send(self, key: UUID, envelope: Envelope) -> ProviderResult: ...

    def lookup(self, key: UUID) -> ProviderResult: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RelayProvider:
    """HTTPS adapter for a trusted relay that implements the delivery contract."""

    def __init__(self, name: str, base_url: str, token: str):
        if not re.fullmatch(_PROVIDER, name):
            raise ConfigurationError("Set MAIL_PROVIDER to a lowercase provider identifier.")
        try:
            endpoint = urlsplit(base_url)
            valid = (
                endpoint.scheme == "https"
                and endpoint.hostname
                and not endpoint.username
                and endpoint.password is None
                and not endpoint.query
                and not endpoint.fragment
                and (endpoint.port is None or 0 < endpoint.port < 65536)
                and base_url.isascii()
                and not any(char.isspace() for char in base_url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError("Set MAIL_RELAY_URL to an HTTPS endpoint.")
        if not re.fullmatch(r"[!-~]{32,512}", token):
            raise ConfigurationError("Set MAIL_RELAY_TOKEN to a printable ASCII bearer token.")
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(_NoRedirect)

    def send(self, key, envelope):
        return self._request(
            "POST",
            "/deliveries",
            key,
            {
                "idempotency_key": str(key),
                "from": envelope.sender,
                "to": envelope.recipient,
                "subject": envelope.subject,
                "text": envelope.body,
            },
        )

    def lookup(self, key):
        return self._request("GET", f"/deliveries/{key}", key)

    def _request(self, method, path, key, payload=None):
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": str(key),
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=5) as response:
                raw = response.read(4097)
            result = json.loads(raw) if len(raw) <= 4096 else None
            if not isinstance(result, dict) or result.get("idempotency_key") != str(key):
                return ProviderResult("unknown")
            return ProviderResult(result.get("state"), result.get("receipt_id"))
        except (OSError, TypeError, ValueError):
            return ProviderResult("unknown")


def configured_inbound_gateway(env=None):
    values = os.environ if env is None else env
    if values.get("REAL_MAIL_ENABLED") != "1":
        return None
    return InboundGateway(values.get("MAIL_INBOUND_SECRET", ""), values.get("MAIL_PROVIDER", ""))


def configured_mail_provider(env=None):
    values = os.environ if env is None else env
    if values.get("REAL_MAIL_ENABLED") != "1":
        return None
    return RelayProvider(
        values.get("MAIL_PROVIDER", ""),
        values.get("MAIL_RELAY_URL", ""),
        values.get("MAIL_RELAY_TOKEN", ""),
    )


def draft_envelope(db, tenant, thread, analysis_id, body):
    row = db.execute(
        """SELECT r.provider,r.sender,r.recipient,e.subject
           FROM mail_routes r
           JOIN jobs j ON (j.tenant_id,j.thread_id)=(r.tenant_id,r.thread_id)
           JOIN messages m ON (m.tenant_id,m.thread_id)=(r.tenant_id,r.thread_id)
             AND m.event_id=j.payload->>'event_id' AND m.skipped_at IS NULL
           JOIN mail_inbound_events e ON (e.tenant_id,e.event_id)=(m.tenant_id,m.event_id)
           WHERE r.tenant_id=%s AND r.thread_id=%s AND j.id=%s AND j.kind='analyze'
             AND (r.provider,r.inbound_address,r.recipient)=(e.provider,e.inbound_address,e.sender)""",
        (tenant, thread, analysis_id),
    ).fetchone()
    if not row:
        return None
    subject = row.pop("subject")
    return Envelope(
        **row, subject=subject if subject.lower().startswith("re:") else f"Re: {subject}", body=body
    )


def record_mail_approval(db, tenant, approval_id, account_id, envelope, presented_digest):
    if not active_approver(db, tenant, account_id):
        raise storage.Conflict("An active creator approver is required.")
    if envelope is None or envelope.digest != presented_digest:
        raise storage.Conflict(
            "The displayed mail envelope changed. Refresh and review the exact reply."
        )
    pending = db.execute(
        """SELECT 1 FROM mail_attempts m JOIN approvals a ON (a.tenant_id,a.id)=(m.tenant_id,m.approval_id)
           JOIN drafts d ON (d.tenant_id,d.id)=(a.tenant_id,a.draft_id)
           WHERE m.tenant_id=%s AND m.state='PENDING_CONFIRMATION' AND d.thread_id=(
               SELECT next_draft.thread_id FROM approvals next_approval JOIN drafts next_draft
                    ON (next_draft.tenant_id,next_draft.id)=(next_approval.tenant_id,next_approval.draft_id)
               WHERE next_approval.tenant_id=%s AND next_approval.id=%s
           ) LIMIT 1""",
        (tenant, tenant, approval_id),
    ).fetchone()
    if pending:
        raise storage.Conflict("Confirm the preceding email delivery before approving another reply.")
    db.execute(
        """INSERT INTO mail_approvals(tenant_id,approval_id,account_id,provider,sender,recipient,
                     subject,envelope_digest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            tenant,
            approval_id,
            account_id,
            envelope.provider,
            envelope.sender,
            envelope.recipient,
            envelope.subject,
            envelope.digest,
        ),
    )


def verify_mail_approval_retry(db, tenant, approval_id, account_id, presented_digest):
    if not active_approver(db, tenant, account_id):
        raise storage.Conflict("An active creator approver is required.")
    original = db.execute(
        """SELECT account_id,envelope_digest FROM mail_approvals
           WHERE tenant_id=%s AND approval_id=%s""",
        (tenant, approval_id),
    ).fetchone()
    if not original or (original["account_id"], original["envelope_digest"]) != (
        account_id,
        presented_digest,
    ):
        raise storage.Conflict("This retry does not match the original approval.")


class MailStore:
    def __init__(self, store):
        self.store = store

    def configure_route(self, tenant, thread, *, provider, inbound_address, sender, recipient):
        """Operator-only routing for a verified send mailbox and an authorized counterpart."""
        if not re.fullmatch(_PROVIDER, provider):
            raise ValueError("Use a lowercase provider identifier.")
        addresses = tuple(map(mailbox, (inbound_address, sender, recipient)))
        with self.store.connect() as db:
            self.store.tenant_lock(db, tenant)
            current = self.store.thread(db, tenant, thread, lock=True)
            changed = db.execute(
                """INSERT INTO mail_routes(tenant_id,thread_id,provider,inbound_address,sender,recipient)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,thread_id) DO UPDATE SET
                   (provider,inbound_address,sender,recipient)=
                       (EXCLUDED.provider,EXCLUDED.inbound_address,EXCLUDED.sender,EXCLUDED.recipient)
                   WHERE (mail_routes.provider,mail_routes.inbound_address,mail_routes.sender,mail_routes.recipient)
                     IS DISTINCT FROM
                         (EXCLUDED.provider,EXCLUDED.inbound_address,EXCLUDED.sender,EXCLUDED.recipient)
                   RETURNING thread_id""",
                (tenant, thread, provider, *addresses),
            ).fetchone()
            if changed:
                storage.event(db, tenant, thread, current["revision"], "mail_route_changed")

    def ingest(self, payload: InboundMail, *, quota=80):
        payload_digest = _fingerprint(payload.model_dump())
        stable_id = "mail:" + _fingerprint([payload.provider, payload.event_id])
        with self.store.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (stable_id,))
            existing = db.execute(
                """SELECT e.payload_digest,m.revision,m.job_id FROM mail_inbound_events e
                   JOIN messages m ON (m.tenant_id,m.event_id)=(e.tenant_id,e.event_id)
                   WHERE e.provider=%s AND e.provider_event_id=%s""",
                (payload.provider, payload.event_id),
            ).fetchone()
            if existing:
                if existing.pop("payload_digest") != payload_digest:
                    raise storage.Conflict(
                        "This provider event is already bound to a different message."
                    )
                return existing
            route = db.execute(
                """SELECT tenant_id,thread_id FROM mail_routes
                   WHERE provider=%s AND inbound_address=%s AND recipient=%s""",
                (payload.provider, payload.to, payload.from_address),
            ).fetchone()
            if not route:
                raise storage.NotFound("The inbound mail route was not found.")
            tenant, thread = route["tenant_id"], route["thread_id"]
            self.store.tenant_lock(db, tenant)
            still_matches = db.execute(
                """SELECT 1 FROM mail_routes WHERE tenant_id=%s AND thread_id=%s AND
                   (provider,inbound_address,recipient)=(%s,%s,%s)""",
                (tenant, thread, payload.provider, payload.to, payload.from_address),
            ).fetchone()
            if not still_matches:
                raise storage.NotFound("The inbound mail route was not found.")
            body = f"From: {payload.from_address}\nSubject: {payload.subject}\n\n{payload.body}"
            result = self.store.ingest(tenant, thread, stable_id, body, quota=quota, db=db)
            db.execute(
                """INSERT INTO mail_inbound_events(provider,provider_event_id,payload_digest,tenant_id,
                          event_id,inbound_address,sender,subject) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    payload.provider,
                    payload.event_id,
                    payload_digest,
                    tenant,
                    stable_id,
                    payload.to,
                    payload.from_address,
                    payload.subject,
                ),
            )
            return result

    def deliver(self, tenant, thread, approval_id, provider: MailProvider):
        return self._handle(tenant, thread, approval_id, provider, dispatch=True)

    def reconcile(self, tenant, thread, approval_id, provider: MailProvider):
        """Resolve an existing attempt using provider lookup only."""
        return self._handle(tenant, thread, approval_id, provider, dispatch=False)

    def _handle(self, tenant, thread, approval_id, provider, *, dispatch):
        lock_key = f"mail-delivery:{tenant}:{approval_id}"
        with self.store.connect(autocommit=True) as db:
            db.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (lock_key,))
            with db.transaction():
                receipt = db.execute(
                    """SELECT * FROM deliveries WHERE tenant_id=%s AND thread_id=%s AND delivery_key=%s""",
                    (tenant, thread, approval_id),
                ).fetchone()
                if receipt:
                    return receipt, False
                approval = self._approval(db, tenant, thread, approval_id)
                if not approval or approval["attempt_state"] in {"REJECTED", "BLOCKED", "ACCEPTED"}:
                    return None, False
                first_attempt = approval["attempt_state"] is None
                if first_attempt:
                    if not dispatch:
                        return None, False
                    policy, current = self._lock_thread(db, tenant, thread)
                    approval = self._approval(db, tenant, thread, approval_id)
                    if not approval:
                        return None, False
                    if self._valid_envelope(db, tenant, thread, approval, policy, current) is None:
                        self._block(db, tenant, thread, approval_id, approval, policy, current)
                        return None, False
                if not provider or provider.name != approval["provider"]:
                    raise MailDeliveryPending("The approved mail provider is unavailable.")
                if first_attempt:
                    db.execute(
                        "INSERT INTO mail_attempts(tenant_id,approval_id,state) VALUES (%s,%s,'PENDING_CONFIRMATION')",
                        (tenant, approval_id),
                    )
                    storage.event(
                        db,
                        tenant,
                        thread,
                        approval["revision"],
                        "mail_handoff_started",
                        approval_id=str(approval_id),
                    )

            # The intent survives a crash or an ambiguous result. Future callers only look up this key.
            if not first_attempt:
                outcome = self._provider_outcome(provider.lookup, approval_id)
                with db.transaction():
                    return self._finalize(db, tenant, thread, approval_id, outcome), False

            with db.transaction():
                policy, current = self._lock_thread(db, tenant, thread)
                approval = self._approval(db, tenant, thread, approval_id)
                if not approval:
                    return None, False
                envelope = self._valid_envelope(db, tenant, thread, approval, policy, current)
                if envelope is None:
                    self._block(db, tenant, thread, approval_id, approval, policy, current)
                    return None, False
                outcome = self._provider_outcome(provider.send, approval_id, envelope)
                return self._finalize(db, tenant, thread, approval_id, outcome), False

    def _lock_thread(self, db, tenant, thread):
        return self.store.tenant_lock(db, tenant), self.store.thread(db, tenant, thread, lock=True)

    @staticmethod
    def _approval(db, tenant, thread, approval_id):
        return db.execute(
            """SELECT d.id AS draft_id,d.analysis_id,d.status,d.body,d.digest,d.version,d.revision,d.policy_version,
                      a.digest AS approved_body_digest,a.version AS approved_version,
                      a.revision AS approved_revision,a.policy_version AS approved_policy_version,
                      ma.account_id,ma.provider,ma.sender,ma.recipient,ma.subject,ma.envelope_digest,
                      attempt.state AS attempt_state
               FROM approvals a JOIN drafts d ON (d.tenant_id,d.id)=(a.tenant_id,a.draft_id)
               JOIN mail_approvals ma ON (ma.tenant_id,ma.approval_id)=(a.tenant_id,a.id)
               LEFT JOIN mail_attempts attempt ON (attempt.tenant_id,attempt.approval_id)=(a.tenant_id,a.id)
               WHERE a.tenant_id=%s AND a.id=%s AND d.thread_id=%s""",
            (tenant, approval_id, thread),
        ).fetchone()

    @staticmethod
    def _matches_revision(approval, policy, current, *, reconciling=False):
        return (
            approval["status"] == "APPROVED"
            and (current["status"] == "APPROVED" or (reconciling and current["status"] == "FAILED"))
            and approval["revision"] == approval["approved_revision"] == current["revision"]
            and approval["version"] == approval["approved_version"]
            and approval["policy_version"] == approval["approved_policy_version"] == policy
        )

    @staticmethod
    def _approved_envelope(approval):
        return Envelope(
            **{name: approval[name] for name in ("provider", "sender", "recipient", "subject", "body")}
        )

    def _valid_envelope(self, db, tenant, thread, approval, policy, current):
        if (
            not approval
            or not self._matches_revision(approval, policy, current)
            or not active_approver(db, tenant, approval["account_id"])
            or storage.digest(approval["body"]) != approval["digest"]
            or approval["digest"] != approval["approved_body_digest"]
        ):
            return None
        saved = self._approved_envelope(approval)
        live = draft_envelope(db, tenant, thread, approval["analysis_id"], approval["body"])
        return saved if saved == live and approval["envelope_digest"] == saved.digest else None

    def _block(self, db, tenant, thread, approval_id, approval, policy, current):
        db.execute(
            """INSERT INTO mail_attempts(tenant_id,approval_id,state) VALUES (%s,%s,'BLOCKED')
               ON CONFLICT (tenant_id,approval_id) DO UPDATE SET state='BLOCKED',updated_at=now()""",
            (tenant, approval_id),
        )
        storage.event(
            db, tenant, thread, approval["revision"], "delivery_blocked", approval_id=str(approval_id)
        )
        if self._matches_revision(approval, policy, current):
            db.execute(
                "UPDATE drafts SET status='STALE' WHERE tenant_id=%s AND id=%s",
                (tenant, approval["draft_id"]),
            )
            self.store.set_state(db, tenant, thread, "FAILED", approval["revision"])

    @staticmethod
    def _provider_outcome(operation, *args):
        try:
            result = operation(*args)
        except Exception:  # noqa: BLE001 - never expose provider errors or assume they confirm rejection
            raise MailDeliveryPending("Provider acceptance has not been confirmed.") from None
        if not isinstance(result, ProviderResult) or result.state == "unknown":
            raise MailDeliveryPending("Provider acceptance has not been confirmed.")
        return result

    def _finalize(self, db, tenant, thread, approval_id, outcome):
        policy, current = self._lock_thread(db, tenant, thread)
        approval = self._approval(db, tenant, thread, approval_id)
        if not approval or approval["attempt_state"] != "PENDING_CONFIRMATION":
            raise MailDeliveryPending("The provider attempt is no longer available.")
        db.execute(
            "UPDATE mail_attempts SET state=%s,updated_at=now() WHERE tenant_id=%s AND approval_id=%s",
            (outcome.state.upper(), tenant, approval_id),
        )
        db.execute(
            """UPDATE jobs SET state='DONE',lease_until=NULL
               WHERE tenant_id=%s AND thread_id=%s AND id=%s AND kind='deliver'""",
            (tenant, thread, approval_id),
        )
        revision = approval["revision"]
        current_approval = self._matches_revision(approval, policy, current, reconciling=True)
        if outcome.state == "rejected":
            storage.event(
                db, tenant, thread, revision, "provider_delivery_rejected", approval_id=str(approval_id)
            )
            if current_approval:
                db.execute(
                    "UPDATE drafts SET status='DELIVERY_FAILED' WHERE tenant_id=%s AND id=%s",
                    (tenant, approval["draft_id"]),
                )
                self.store.set_state(db, tenant, thread, "FAILED", revision)
            return None
        body = approval["body"]
        if storage.digest(body) != approval["approved_body_digest"] or (
            self._approved_envelope(approval).digest != approval["envelope_digest"]
        ):
            raise MailDeliveryPending("The saved mail approval no longer matches its exact payload.")
        receipt = db.execute(
            """INSERT INTO deliveries(tenant_id,thread_id,delivery_key,body,mail_provider,provider_receipt_id)
               VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (tenant, thread, approval_id, body, approval["provider"], outcome.receipt_id),
        ).fetchone()
        db.execute(
            "UPDATE drafts SET status='SENT' WHERE tenant_id=%s AND id=%s",
            (tenant, approval["draft_id"]),
        )
        storage.event(
            db, tenant, thread, revision, "provider_delivery_accepted", approval_id=str(approval_id)
        )
        if current_approval:
            self.store.set_state(db, tenant, thread, "SENT", revision)
            self.store.set_state(db, tenant, thread, "AWAITING_BRAND_REPLY", revision)
        return receipt


def main():
    parser = argparse.ArgumentParser(description="Look up the outcome of an existing provider attempt.")
    parser.add_argument("tenant", type=UUID)
    parser.add_argument("thread", type=UUID)
    parser.add_argument("approval", type=UUID)
    args = parser.parse_args()
    try:
        provider = configured_mail_provider()
        if provider is None:
            raise ConfigurationError("Real mail is disabled.")
        store = storage.Store(os.environ["DATABASE_URL"])
        mail = MailStore(store)
        receipt, _ = mail.reconcile(args.tenant, args.thread, args.approval, provider)
        print(
            "Provider acceptance recorded."
            if receipt
            else "No accepted provider receipt for this approval."
        )
    except (ConfigurationError, MailDeliveryPending) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
