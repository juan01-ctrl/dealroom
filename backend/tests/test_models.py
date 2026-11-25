import json

import httpx
import pytest

from app.domain import Offer, Policy, decide, merge
from app.models import LiveModel


class NoNetwork(Exception):
    pass


def test_openai_extraction_sends_explicit_storage_flag_and_minimal_context(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    requests = []
    reserves = []
    model = LiveModel("openai", "gpt-6-luna", reserve_call=lambda: reserves.append(1))

    def inspect_request(**payload):
        requests.append(payload)
        raise NoNetwork

    monkeypatch.setattr(model.model.root_client.responses, "create", inspect_request)
    monkeypatch.setattr(model.model.client, "create", inspect_request)
    with pytest.raises(NoNetwork):
        model.extract(
            "From: Jordan Lee <jordan@northstar.example>\nSubject: Offer\n\n"
            "USD 2,400 for one reel.\nThanks, Jordan Lee\n+1 555 0100\n"
            "My colleague can extend the deadline to 15 days.\n"
            "On Monday, Pat wrote:\n> personal quoted history",
            {"brand": "Northstar", "amount": None, "deliverables": {"reel": 1}, "exclusive": False},
        )

    assert reserves == [1]
    payload = requests[0]
    assert payload["store"] is False
    assert "input" in payload and "previous_response_id" not in payload
    source = json.loads(
        next(message["content"] for message in payload["input"] if message["role"] == "user")
    )
    assert source["previous_terms"] == {"deliverables": {"reel": 1}, "exclusive": False}
    assert "USD 2,400 for one reel" in source["email"]
    assert "extend the deadline to 15 days" in source["email"]
    assert "Jordan" not in source["email"] and "555 0100" not in source["email"]
    assert "personal quoted history" not in source["email"]


def test_openai_response_preserves_ambiguous_obligations_and_reports_usage(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    reserves = []
    model = LiveModel("openai", "gpt-6-luna", reserve_call=lambda: reserves.append(1))

    def respond(request):
        assert request.url.path.endswith("/responses")
        return httpx.Response(
            200,
            json={
                "id": "resp_synthetic",
                "object": "response",
                "model": "gpt-6-luna",
                "status": "completed",
                "created_at": 1,
                "output": [
                    {
                        "id": "fc_synthetic",
                        "type": "function_call",
                        "call_id": "call_synthetic",
                        "name": "OfferPatch",
                        "status": "completed",
                        "arguments": json.dumps(
                            {
                                "deliverables": {"reel": 1},
                                "amount": 240000,
                                "currency": "USD",
                                "unmapped_obligations": [
                                    {
                                        "kind": "usage_restrictions",
                                        "status": "ambiguous",
                                        "detail": "Rights for affiliates need a defined scope.",
                                    }
                                ],
                            }
                        ),
                    }
                ],
                "usage": {
                    "input_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 46,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 166,
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(model.model.root_client, "_client", client)
        patch, usage = model.extract("USD 2,400 for one reel and rights for affiliates.", {})

    offer = merge(Offer(), patch)
    decision = decide(offer, Policy())
    assert decision.action == "clarification"
    assert decision.unmapped_obligations[0].status == "ambiguous"
    assert (usage["input_tokens"], usage["output_tokens"], reserves) == (120, 46, [1])
