from uuid import uuid4

import pytest

from app import ingress_probe
from app.mail_delivery import InboundGateway, InboundMail


def event(event_id):
    return InboundMail(
        provider="relay",
        event_id=event_id,
        to="campaign@example.test",
        from_address="brand@example.test",
        sender_authenticated=True,
        subject="Synthetic campaign",
        body="USD 2,400 for one reel.",
    )


def test_staging_probe_signs_the_actual_body_and_disallows_external_http():
    payload = event("example")
    secret = "synthetic-probe-key-for-tests-only"
    endpoint = ingress_probe.ingress_url("http://127.0.0.1:8000")
    request = ingress_probe.signed_request(endpoint, secret.encode(), payload, 100)
    gateway = InboundGateway(secret, "relay", clock=lambda: 100)
    assert (
        gateway.verify(
            request.data,
            request.get_header("X-mail-timestamp"),
            request.get_header("X-mail-signature"),
        )
        == payload
    )
    with pytest.raises(ValueError):
        ingress_probe.ingress_url("http://staging.example.test")
    with pytest.raises(ValueError):
        ingress_probe.ingress_url("https://staging.example.test/other-path")


def test_staging_probe_rejects_conflicting_or_shared_receipts(monkeypatch):
    calls = iter([uuid4(), uuid4()])
    monkeypatch.setattr(
        ingress_probe, "submit", lambda _, __, payload: (payload.event_id, "202", next(calls), 1)
    )
    report = ingress_probe.run_probe("", b"", [event("one")] * 2, rate=100, workers=1)
    assert not report["passed"]
    assert report["events_with_multiple_receipts"] == 1
    shared = uuid4()
    monkeypatch.setattr(
        ingress_probe, "submit", lambda _, __, payload: (payload.event_id, "202", shared, 1)
    )
    report = ingress_probe.run_probe("", b"", [event("one"), event("two")], rate=100, workers=1)
    assert not report["passed"]
    assert report["unique_jobs"] == 1
