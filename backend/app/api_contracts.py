from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from .domain import Decision, Policy, Proposal


class ThreadSummary(BaseModel):
    id: UUID
    label: str
    revision: int
    status: str


class Capabilities(BaseModel):
    evaluations: bool
    simulated_failures: bool
    can_write: bool = True


class SessionView(BaseModel):
    threads: list[ThreadSummary]
    mode: Literal["scripted", "live"]
    capabilities: Capabilities
    workspace_kind: Literal["sandbox", "creator"]


class ProfileView(BaseModel):
    version: int
    data: Policy
    suggested_tone: str | None


class TimelineEntry(BaseModel):
    id: int
    revision: int
    kind: str
    details: dict
    created_at: datetime


class TimelinePage(BaseModel):
    timeline: list[TimelineEntry]
    has_more: bool


class ThreadMessage(BaseModel):
    revision: int
    body: str
    created_at: datetime


class MailEnvelopeView(BaseModel):
    sender: str
    recipient: str
    subject: str
    digest: str


class DraftView(ThreadMessage):
    id: UUID
    version: int
    status: str
    policy_version: int
    decision: Decision
    proposal: Proposal
    mail: MailEnvelopeView | None = None


class DeliveryView(BaseModel):
    delivery_key: UUID
    body: str
    created_at: datetime
    mail_provider: str | None = None
    provider_receipt_id: str | None = None
    revision: int | None = None


class RecentReceipt(DeliveryView):
    draft_id: UUID


class RecoverableTurn(BaseModel):
    job_id: UUID
    revision: int


class ThreadSnapshot(ThreadSummary):
    fault: str | None
    messages: list[ThreadMessage]
    drafts: list[DraftView]
    deliveries: list[DeliveryView]
    timeline: list[TimelineEntry]
    timeline_has_more: bool
    recoverable_turns: list[RecoverableTurn]
    history_cursor: str | None = None
    unresolved_delivery_revision: int | None = None
    recent_receipts: list[RecentReceipt] | None = None


class HistoryPage(BaseModel):
    messages: list[ThreadMessage]
    drafts: list[DraftView]
    deliveries: list[DeliveryView]
    history_cursor: str | None
