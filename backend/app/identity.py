"""Creator identity behind a trusted external identity gateway."""

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from time import time
from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError, field_validator

from .configuration import ConfigurationError
from .domain import Input, Policy

Role = Literal["owner", "approver", "viewer"]

_TOKEN = re.compile(r"([A-Za-z0-9_-]+)\.([0-9a-f]{64})", re.ASCII)
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_MAILBOX = re.compile(rf"({_ATOM}(?:\.{_ATOM})*)@({_LABEL}(?:\.{_LABEL})+)", re.ASCII)


class IdentityError(ValueError):
    """The asserted identity or workspace membership could not be verified."""


def mailbox(value: str) -> str:
    match = _MAILBOX.fullmatch(value)
    if not match or len(value) > 254 or len(match[1]) > 64:
        raise ValueError("Use a single plain email address.")
    return f"{match[1]}@{match[2].lower()}"


class _IdentityAssertion(Input):
    iss: str = Field(min_length=1, max_length=256)
    aud: Literal["dealroom-creator"]
    sub: str = Field(min_length=1, max_length=256, pattern=r"^[!-~]+$")
    email: str
    email_verified: bool
    iat: int
    exp: int

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        return mailbox(value)


@dataclass(frozen=True)
class IdentityClaims:
    issuer: str
    subject: str
    email: str


@dataclass(frozen=True)
class CreatorMember:
    tenant_id: UUID
    account_id: UUID
    role: Role

    @property
    def can_approve(self):
        return self.role in {"owner", "approver"}


def integration_secret(value, name):
    if not value or not value.isascii() or len(value) < 32 or not value.isprintable():
        raise ConfigurationError(f"Set {name} to a printable ASCII secret of at least 32 characters.")
    return value.encode("ascii")


def valid_hmac(secret, message, provided):
    if not isinstance(provided, str) or not re.fullmatch(r"[0-9a-f]{64}", provided):
        return False
    expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


class IdentityGateway:
    def __init__(self, secret: str, issuer: str, *, clock=time):
        self.secret = integration_secret(secret, "CREATOR_IDENTITY_SECRET")
        self.issuer = issuer
        self.clock = clock

    def verify(self, token: str) -> IdentityClaims:
        match = _TOKEN.fullmatch(token) if isinstance(token, str) and len(token) <= 4096 else None
        if not match:
            raise IdentityError("Creator identity could not be verified.")
        encoded = match[1].encode("ascii")
        if not valid_hmac(self.secret, b"dealroom-identity-v1." + encoded, match[2]):
            raise IdentityError("Creator identity could not be verified.")
        try:
            raw = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            assertion = _IdentityAssertion.model_validate_json(raw)
        except (ValidationError, binascii.Error):
            raise IdentityError("Creator identity could not be verified.") from None
        now = int(self.clock())
        if (
            assertion.iss != self.issuer
            or not assertion.email_verified
            or assertion.iat > now + 30
            or assertion.exp <= now
            or not 0 < assertion.exp - assertion.iat <= 300
        ):
            raise IdentityError("Creator identity could not be verified.")
        return IdentityClaims(assertion.iss, assertion.sub, assertion.email)


def is_creator_workspace(db, tenant):
    return (
        db.execute("SELECT 1 FROM creator_workspaces WHERE tenant_id=%s", (tenant,)).fetchone()
        is not None
    )


def active_approver(db, tenant, account):
    if not account:
        return False
    return (
        db.execute(
            """SELECT 1 FROM workspace_memberships m JOIN creator_accounts a ON a.id=m.account_id
               WHERE m.tenant_id=%s AND m.account_id=%s AND m.active AND a.active
                 AND m.role IN ('owner','approver') FOR SHARE OF m,a""",
            (tenant, account),
        ).fetchone()
        is not None
    )


class IdentityStore:
    def __init__(self, store):
        self.store = store

    def member(self, claims: IdentityClaims, workspace: UUID) -> CreatorMember:
        with self.store.connect() as db:
            row = db.execute(
                """SELECT m.tenant_id,m.account_id,m.role FROM workspace_memberships m
                   JOIN creator_accounts a ON a.id=m.account_id
                   WHERE a.issuer=%s AND a.subject=%s AND a.email=%s AND m.tenant_id=%s
                     AND a.active AND m.active""",
                (claims.issuer, claims.subject, claims.email, workspace),
            ).fetchone()
        if not row:
            raise IdentityError("Creator workspace membership could not be verified.")
        return CreatorMember(**row)

    def provision_workspace(self, claims: IdentityClaims, *, policy=None) -> CreatorMember:
        """Operator-only provisioning after independently confirming a creator identity."""
        tenant, account = uuid4(), uuid4()
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO creator_accounts(id,issuer,subject,email) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (issuer,subject) DO NOTHING""",
                (account, claims.issuer, claims.subject, claims.email),
            )
            identity = db.execute(
                """SELECT id FROM creator_accounts WHERE issuer=%s AND subject=%s AND email=%s
                     AND active FOR SHARE""",
                (claims.issuer, claims.subject, claims.email),
            ).fetchone()
            if not identity:
                raise IdentityError("Creator identity could not be provisioned.")
            account = identity["id"]
            db.execute("INSERT INTO tenants(id) VALUES (%s)", (tenant,))
            db.execute(
                "INSERT INTO policies VALUES (%s,1,%s)",
                (tenant, Jsonb((policy or Policy()).model_dump())),
            )
            db.execute("INSERT INTO creator_workspaces VALUES (%s)", (tenant,))
            db.execute(
                """INSERT INTO workspace_memberships(tenant_id,account_id,role)
                   VALUES (%s,%s,'owner')""",
                (tenant, account),
            )
        return CreatorMember(tenant, account, "owner")


def configured_identity_gateway(env=None):
    values = os.environ if env is None else env
    if values.get("REAL_MAIL_ENABLED") != "1":
        return None
    issuer = values.get("CREATOR_IDENTITY_ISSUER", "")
    if not issuer or len(issuer) > 256 or not issuer.isascii() or not issuer.isprintable():
        raise ConfigurationError("Set CREATOR_IDENTITY_ISSUER to the configured identity issuer.")
    return IdentityGateway(values.get("CREATOR_IDENTITY_SECRET", ""), issuer)
