"""HTTP transport and payload identity for the trusted mail relay."""

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .configuration import ConfigurationError

MAIL_PROVIDER_PATTERN = r"[a-z][a-z0-9_-]{0,39}"


def payload_fingerprint(value):
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
        return payload_fingerprint(asdict(self))

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
        if not re.fullmatch(MAIL_PROVIDER_PATTERN, name):
            raise ConfigurationError("Set MAILMAIL_PROVIDER_PATTERN to a lowercase provider identifier.")
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
        except (OSError, TypeError, ValueError):
            return ProviderResult("unknown")
        return parse_outcome(raw, key)


def parse_outcome(raw: bytes, key: UUID) -> ProviderResult:
    try:
        result = json.loads(raw) if len(raw) <= 4096 else None
        if isinstance(result, dict) and result.get("idempotency_key") == str(key):
            return ProviderResult(result.get("state"), result.get("receipt_id"))
    except (TypeError, ValueError):
        pass
    return ProviderResult("unknown")


def configured_mail_provider(env=None):
    values = os.environ if env is None else env
    if values.get("REAL_MAIL_ENABLED") != "1":
        return None
    return RelayProvider(
        values.get("MAIL_PROVIDER", ""),
        values.get("MAIL_RELAY_URL", ""),
        values.get("MAIL_RELAY_TOKEN", ""),
    )
