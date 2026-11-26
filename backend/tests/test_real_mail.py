import base64
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import uuid4

import pytest

from app.identity import IdentityError, IdentityGateway, IdentityStore
from app.mail_delivery import (
    InboundGateway,
    InboundMail,
    MailAuthenticationError,
    MailDeliveryPending,
    MailStore,
    ProviderResult,
    configured_inbound_gateway,
    configured_mail_provider,
    draft_envelope,
)
from app.models import ScriptedModel
from app.storage import Conflict, NotFound
from app.worker import Worker

NOW = 1700000000
SECRET = "synthetic-test-key-for-integration-only"
ISSUER = "https://identity.example.test"


def identity_token(**changes):
    payload = {
        "iss": ISSUER,
        "aud": "dealroom-creator",
        "sub": "creator-44",
        "email": "creator@example.test",
        "email_verified": True,
        "iat": NOW,
        "exp": NOW + 60,
    } | changes
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    tag = hmac.digest(SECRET.encode(), b"dealroom-identity-v1." + encoded, "sha256").hex()
    return f"{encoded.decode()}.{tag}"


def inbound_payload(**changes):
    return {
        "provider": "relay",
        "event_id": "upstream-event-44",
        "to": "campaign@receive.example.test",
        "from_address": "brand@example.test",
        "sender_authenticated": True,
        "subject": "Campaign 预算",
        "body": "Hi, we can offer $3,000 for one reel.",
    } | changes


def signed_inbound(**changes):
    payload = json.dumps(inbound_payload(**changes)).encode()
    timestamp = str(NOW)
    signature = hmac.digest(
        SECRET.encode(),
        b"dealroom-inbound-v1." + timestamp.encode() + b"." + payload,
        "sha256",
    ).hex()
    return payload, timestamp, signature


@pytest.fixture
def gateway():
    return IdentityGateway(SECRET, ISSUER, clock=lambda: NOW)


def test_identity_checks_signature_audience_expiry_and_verified_email(gateway):
    valid = gateway.verify(identity_token())
    assert (valid.subject, valid.email) == ("creator-44", "creator@example.test")
    for invalid in (
        identity_token()[:-64] + "0" * 64,
        identity_token(aud="unrelated-service"),
        identity_token(exp=NOW),
        identity_token(email_verified=False),
    ):
        with pytest.raises(IdentityError):
            gateway.verify(invalid)


def test_inbound_checks_original_bytes_freshness_and_sender_authentication():
    incoming = InboundGateway(SECRET, "relay", clock=lambda: NOW)
    body, timestamp, signature = signed_inbound()
    assert incoming.verify(body, timestamp, signature).subject == "Campaign 预算"

    with pytest.raises(MailAuthenticationError):
        incoming.verify(body + b" ", timestamp, signature)
    with pytest.raises(MailAuthenticationError):
        InboundGateway(SECRET, "relay", clock=lambda: NOW + 301).verify(body, timestamp, signature)
    for changes in (
        {"sender_authenticated": False},
        {"sender_authenticated": 1},
        {"provider": "another-relay"},
    ):
        with pytest.raises(MailAuthenticationError):
            incoming.verify(*signed_inbound(**changes))


def test_integration_defaults_to_disabled():
    assert configured_inbound_gateway({}) is None
    assert configured_mail_provider({}) is None


def route_address(thread):
    return f"campaign-{thread}@receive.example.test"


def verified_payload(thread, *, event="upstream", **changes):
    return InboundMail.model_validate(
        inbound_payload(to=route_address(thread), event_id=f"{thread}-{event}", **changes)
    )


@pytest.fixture
def creator_setup(store, gateway):
    identities = IdentityStore(store)
    claims = gateway.verify(identity_token())
    member = identities.provision_workspace(claims)
    thread = store.create_thread(member.tenant_id, "Confirmed campaign")
    mail = MailStore(store)
    mail.configure_route(
        member.tenant_id,
        thread,
        provider="relay",
        inbound_address=route_address(thread),
        sender="creator@send.example.test",
        recipient="brand@example.test",
    )
    return member, thread, mail


def test_verified_identity_requires_workspace_membership(store, gateway):
    identities = IdentityStore(store)
    claims = gateway.verify(identity_token())
    member = identities.provision_workspace(claims)
    assert identities.member(claims, member.tenant_id) == member
    with pytest.raises(IdentityError):
        identities.member(claims, uuid4())
    with pytest.raises(IdentityError):
        identities.member(gateway.verify(identity_token(email="other@example.test")), member.tenant_id)
    with store.connect() as db:
        db.execute(
            "UPDATE workspace_memberships SET active=false WHERE tenant_id=%s AND account_id=%s",
            (member.tenant_id, member.account_id),
        )
    with pytest.raises(IdentityError):
        identities.member(claims, member.tenant_id)


def test_real_inbound_is_routed_and_deduplicated_on_provider_event(creator_setup):
    _, thread, mail = creator_setup
    incoming = verified_payload(thread)
    received = mail.ingest(incoming)
    assert received["revision"] == 1
    assert mail.ingest(incoming) == received
    with pytest.raises(Conflict):
        mail.ingest(verified_payload(thread, subject="Changed subject"))
    with pytest.raises(NotFound):
        mail.ingest(verified_payload(thread, event="unmatched", from_address="other@example.test"))


class FakeProvider:
    name = "relay"

    def __init__(self, *, ambiguous=False):
        self.sent = {}
        self.visible = not ambiguous
        self.ambiguous = ambiguous
        self.calls = 0

    def send(self, key, envelope):
        self.calls += 1
        self.sent[key] = envelope
        if self.ambiguous:
            raise TimeoutError
        return ProviderResult("accepted", f"provider-receipt-{key}")

    def lookup(self, key):
        if self.visible and key in self.sent:
            return ProviderResult("accepted", f"provider-receipt-{key}")
        return ProviderResult("unknown")


@pytest.fixture
def real_draft(store, creator_setup):
    member, thread, mail = creator_setup
    job = mail.ingest(verified_payload(thread))
    assert Worker(store, model=ScriptedModel(), immediate=True, mail_provider=FakeProvider()).step(
        job["job_id"]
    )
    with store.connect() as db:
        draft = db.execute(
            "SELECT * FROM drafts WHERE tenant_id=%s AND thread_id=%s", (member.tenant_id, thread)
        ).fetchone()
        assert draft is not None
        envelope = draft_envelope(db, member.tenant_id, thread, draft["analysis_id"], draft["body"])
    return member, thread, mail, draft, envelope


def approve(store, member, thread, draft, presented_digest):
    return store.review(
        member.tenant_id,
        thread,
        draft["id"],
        draft["version"],
        "approve",
        account_id=member.account_id,
        presented_digest=presented_digest,
    )


def test_approval_binds_recipient_subject_and_exact_body(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    assert envelope.recipient == "brand@example.test"
    assert envelope.subject == "Re: Campaign 预算"
    assert envelope.body == draft["body"]
    for altered in (
        replace(envelope, recipient="imposter@example.test"),
        replace(envelope, subject="Other deal"),
        replace(envelope, body=draft["body"] + " "),
    ):
        with pytest.raises(Conflict):
            approve(store, member, thread, draft, altered.digest)
    with pytest.raises(Conflict):
        approve(store, replace(member, account_id=uuid4()), thread, draft, envelope.digest)

    approval_id = approve(store, member, thread, draft, envelope.digest)
    assert approve(store, member, thread, draft, envelope.digest) == approval_id
    other_account = uuid4()
    with store.connect() as db:
        db.execute(
            "INSERT INTO creator_accounts(id,issuer,subject,email) VALUES (%s,%s,%s,%s)",
            (other_account, ISSUER, str(other_account), "other@example.test"),
        )
        db.execute(
            "INSERT INTO workspace_memberships(tenant_id,account_id,role) VALUES (%s,%s,'approver')",
            (member.tenant_id, other_account),
        )
    with pytest.raises(Conflict):
        approve(store, replace(member, account_id=other_account), thread, draft, envelope.digest)
    provider = FakeProvider()
    receipt, fault = mail.deliver(member.tenant_id, thread, approval_id, provider)
    assert not fault
    assert provider.sent == {approval_id: envelope}
    assert receipt["provider_receipt_id"] == f"provider-receipt-{approval_id}"
    assert mail.deliver(member.tenant_id, thread, approval_id, provider)[0] == receipt
    assert provider.calls == 1
    with store.connect() as db:
        attribution = db.execute(
            "SELECT account_id,recipient,subject,envelope_digest FROM mail_approvals WHERE approval_id=%s",
            (approval_id,),
        ).fetchone()
    assert attribution == {
        "account_id": member.account_id,
        "recipient": "brand@example.test",
        "subject": "Re: Campaign 预算",
        "envelope_digest": envelope.digest,
    }


def test_recipient_change_after_approval_blocks_provider_handoff(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    mail.configure_route(
        member.tenant_id,
        thread,
        provider="relay",
        inbound_address=route_address(thread),
        sender="creator@send.example.test",
        recipient="other@example.test",
    )
    provider = FakeProvider()
    assert mail.deliver(member.tenant_id, thread, approval_id, provider) == (None, False)
    assert provider.sent == {}


def test_lost_provider_response_reconciles_without_resending(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    provider = FakeProvider(ambiguous=True)
    with pytest.raises(MailDeliveryPending):
        mail.deliver(member.tenant_id, thread, approval_id, provider)
    with pytest.raises(MailDeliveryPending):
        mail.deliver(member.tenant_id, thread, approval_id, provider)
    assert provider.calls == 1
    assert provider.sent == {approval_id: envelope}
    provider.visible = True
    receipt, _ = mail.reconcile(member.tenant_id, thread, approval_id, provider)
    assert receipt["provider_receipt_id"] == f"provider-receipt-{approval_id}"
    assert provider.calls == 1


def test_provider_receipt_for_superseded_draft_preserves_newer_turn(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    provider = FakeProvider(ambiguous=True)
    with pytest.raises(MailDeliveryPending):
        mail.deliver(member.tenant_id, thread, approval_id, provider)

    incoming = verified_payload(thread, event="next-message", body="I have a different deadline.")
    next_turn = mail.ingest(incoming)
    assert next_turn["revision"] == 2
    assert Worker(store, model=ScriptedModel(), immediate=True, mail_provider=provider).step(
        next_turn["job_id"]
    )
    with store.connect() as db:
        next_draft = db.execute(
            """SELECT * FROM drafts WHERE tenant_id=%s AND thread_id=%s AND revision=2""",
            (member.tenant_id, thread),
        ).fetchone()
        next_envelope = draft_envelope(
            db, member.tenant_id, thread, next_draft["analysis_id"], next_draft["body"]
        )
    with pytest.raises(Conflict):
        approve(store, member, thread, next_draft, next_envelope.digest)

    provider.visible = True
    receipt, _ = mail.reconcile(member.tenant_id, thread, approval_id, provider)
    assert receipt["provider_receipt_id"] == f"provider-receipt-{approval_id}"
    with store.connect() as db:
        latest = store.thread(db, member.tenant_id, thread)
    assert (latest["revision"], latest["status"]) == (2, "AWAITING_APPROVAL")
    assert provider.calls == 1
    assert approve(store, member, thread, next_draft, next_envelope.digest) != approval_id


def test_reanalyzed_draft_keeps_the_signed_original_envelope(store, real_draft):
    member, thread, _, draft, envelope = real_draft
    store.review(
        member.tenant_id, thread, draft["id"], draft["version"], "reject", account_id=member.account_id
    )
    store.transition(member.tenant_id, thread, draft["revision"], "reanalyze")
    with store.connect() as db:
        job_id = db.execute(
            "SELECT id FROM jobs WHERE tenant_id=%s AND thread_id=%s AND revision=%s AND kind='analyze'",
            (member.tenant_id, thread, draft["revision"] + 1),
        ).fetchone()["id"]
    worker = Worker(store, model=ScriptedModel(), immediate=True, mail_provider=FakeProvider())
    assert worker.step(job_id)
    current = store.snapshot(member.tenant_id, thread)["drafts"][-1]
    assert current["mail"]["recipient"] == envelope.recipient
    assert current["mail"]["subject"] == envelope.subject
    assert approve(store, member, thread, current, current["mail"]["digest"])


def test_reconciliation_restores_current_thread_after_exhausted_retries(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    provider = FakeProvider(ambiguous=True)
    worker = Worker(store, model=ScriptedModel(), immediate=True, mail_provider=provider)
    for _ in range(4):
        assert worker.step(approval_id)
    assert store.snapshot(member.tenant_id, thread)["status"] == "FAILED"
    provider.visible = True
    receipt, _ = mail.reconcile(member.tenant_id, thread, approval_id, provider)
    assert receipt["provider_receipt_id"] == f"provider-receipt-{approval_id}"
    assert store.snapshot(member.tenant_id, thread)["status"] == "AWAITING_BRAND_REPLY"
    assert provider.calls == 1
    with store.connect() as db:
        assert (
            db.execute("SELECT state FROM jobs WHERE id=%s", (approval_id,)).fetchone()["state"]
            == "DONE"
        )


def test_slow_relay_does_not_hold_workspace_write_locks(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    other = store.create_thread(member.tenant_id, "Another creator campaign")
    mail.configure_route(
        member.tenant_id,
        other,
        provider="relay",
        inbound_address=route_address(other),
        sender=envelope.sender,
        recipient=envelope.recipient,
    )
    sending = Event()
    release = Event()

    class SlowRelay(FakeProvider):
        def send(self, key, approved):
            sending.set()
            if not release.wait(timeout=10):
                raise TimeoutError
            return super().send(key, approved)

    provider = SlowRelay()
    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(mail.deliver, member.tenant_id, thread, approval_id, provider)
        try:
            assert sending.wait(timeout=5)
            ingress = pool.submit(mail.ingest, verified_payload(other, event="while-sending"))
            assert ingress.result(timeout=2)["revision"] == 1
            # A later route edit leaves the already committed handoff bound to its original recipient.
            mail.configure_route(
                member.tenant_id,
                thread,
                provider="relay",
                inbound_address=route_address(thread),
                sender=envelope.sender,
                recipient="new-counterparty@example.test",
            )
        finally:
            release.set()
        receipt, _ = delivery.result(timeout=5)
    assert receipt["provider_receipt_id"] == f"provider-receipt-{approval_id}"
    assert provider.sent == {approval_id: envelope}


def test_interrupted_dispatch_keeps_committed_attempt_lookup_only(store, real_draft):
    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)

    class InterruptedRelay(FakeProvider):
        def send(self, key, approved):
            with store.connect() as db:
                attempt = db.execute(
                    "SELECT state FROM mail_attempts WHERE tenant_id=%s AND approval_id=%s",
                    (member.tenant_id, key),
                ).fetchone()
            assert attempt == {"state": "PENDING_CONFIRMATION"}
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        mail.deliver(member.tenant_id, thread, approval_id, InterruptedRelay())
    restarted = FakeProvider()
    with pytest.raises(MailDeliveryPending):
        mail.deliver(member.tenant_id, thread, approval_id, restarted)
    assert restarted.calls == 0


def test_operations_reports_aged_unconfirmed_mail(store, real_draft):
    from app.operations import MAIL_CONFIRMATION_AGE_LIMIT_SECONDS, operations_status

    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    provider = FakeProvider(ambiguous=True)
    with pytest.raises(MailDeliveryPending):
        mail.deliver(member.tenant_id, thread, approval_id, provider)
    with store.connect() as db:
        db.execute(
            """UPDATE mail_attempts SET created_at=now()-(%s * interval '1 second')
               WHERE tenant_id=%s AND approval_id=%s""",
            (MAIL_CONFIRMATION_AGE_LIMIT_SECONDS + 1, member.tenant_id, approval_id),
        )
    health = operations_status(store, require_evaluation=False)
    assert health["mail_confirmation"]["pending"] >= 1
    assert health["mail_confirmation"]["oldest_seconds"] > MAIL_CONFIRMATION_AGE_LIMIT_SECONDS
    assert health["status"] == "degraded"
    provider.visible = True
    assert mail.reconcile(member.tenant_id, thread, approval_id, provider)[0]
    assert (
        operations_status(store, require_evaluation=False)["mail_confirmation"]["oldest_seconds"]
        <= MAIL_CONFIRMATION_AGE_LIMIT_SECONDS
    )


def test_pilot_metrics_reports_an_original_delivery_without_exposing_content(store, real_draft):
    from datetime import UTC, datetime, timedelta

    from app.pilot_metrics import collect_pilot_metrics

    member, thread, mail, draft, envelope = real_draft
    approval_id = approve(store, member, thread, draft, envelope.digest)
    assert mail.deliver(member.tenant_id, thread, approval_id, FakeProvider())[0]
    now = datetime.now(UTC)
    metrics = collect_pilot_metrics(
        store, member.tenant_id, now - timedelta(minutes=5), now + timedelta(seconds=1)
    )
    assert metrics["reviewable_analyses"] == metrics["delivered_as_written"] == 1
    assert metrics["delivered_with_body_changes"] == metrics["explicitly_rejected"] == 0
    assert metrics["estimated_published_analysis_cost_per_delivered_draft_usd"] == 0
    assert metrics["p95_seconds_to_reviewable"] >= 0
    assert envelope.body not in json.dumps(metrics)
    demo, _ = store.create_session()
    with pytest.raises(NotFound):
        collect_pilot_metrics(store, demo, now - timedelta(minutes=5), now)
