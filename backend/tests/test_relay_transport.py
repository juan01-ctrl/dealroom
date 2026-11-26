import json
from uuid import uuid4

import pytest

from app.relay_transport import ProviderResult, parse_outcome


@pytest.mark.parametrize(
    ("payload", "state"),
    [
        ({"state": "accepted", "receipt_id": "receipt-1"}, "accepted"),
        ({"state": "rejected"}, "rejected"),
        ({"state": "accepted"}, "unknown"),
        ({"state": "rejected", "receipt_id": "unexpected"}, "unknown"),
        ({"state": "accepted", "receipt_id": "receipt\n1"}, "unknown"),
        ({"state": "accepted", "receipt_id": "x" * 4096}, "unknown"),
    ],
)
def test_relay_result_requires_a_valid_outcome_for_the_exact_key(payload, state):
    key = uuid4()
    raw = json.dumps({"idempotency_key": str(key), **payload}).encode()
    result = parse_outcome(raw, key)
    assert result.state == state
    assert parse_outcome(raw, uuid4()) == ProviderResult("unknown")
    assert parse_outcome(b"invalid JSON", key) == ProviderResult("unknown")
