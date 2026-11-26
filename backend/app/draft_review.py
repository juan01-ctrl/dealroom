"""Draft review commands and their approval transaction."""

from typing import Literal
from uuid import UUID, uuid4

from .domain import Decision, Proposal
from .identity import active_approver, is_creator_workspace
from .mail_delivery import draft_envelope, record_mail_approval, verify_mail_approval_retry
from .persistence_events import event
from .storage import Conflict, NotFound, Store, enqueue, invalidate


def review_draft(
    store: Store,
    tenant: UUID,
    thread: UUID,
    draft_id: UUID,
    version: int,
    action: Literal["edit", "reject", "approve"],
    proposal: Proposal | None = None,
    *,
    account_id: UUID | None = None,
    presented_digest: str | None = None,
) -> UUID:
    # Policy/tenant locking precedes thread locking, as it does during inbound and mail handoff.
    with store.connect() as db:
        policy_version = store.tenant_lock(db, tenant)
        current = store.thread(db, tenant, thread, lock=True)
        draft = db.execute(
            "SELECT * FROM drafts WHERE tenant_id=%s AND thread_id=%s AND id=%s",
            (tenant, thread, draft_id),
        ).fetchone()
        if not draft:
            raise NotFound("Draft not found.")
        revision = current["revision"]
        real = is_creator_workspace(db, tenant)
        if real and (not account_id or not active_approver(db, tenant, account_id)):
            raise Conflict("An active creator approver is required.")
        if draft["version"] != version:
            raise Conflict("This draft is outdated.")
        if action == "approve":
            approval = db.execute(
                "SELECT id FROM approvals WHERE tenant_id=%s AND draft_id=%s", (tenant, draft_id)
            ).fetchone()
            if approval:
                if real:
                    verify_mail_approval_retry(db, tenant, approval["id"], account_id, presented_digest)
                return approval["id"]
        if (draft["revision"], draft["policy_version"]) != (revision, policy_version):
            raise Conflict("This draft is outdated.")
        if draft["status"] not in {"PENDING", "APPROVED"}:
            raise Conflict("This draft is no longer available for review.")
        if action == "edit":
            invalidate(db, tenant, thread)
            identifier = store.insert_draft(
                db,
                tenant,
                thread,
                draft["analysis_id"],
                version + 1,
                revision,
                policy_version,
                Decision.model_validate(draft["decision"]),
                proposal,
            )
            store.set_state(db, tenant, thread, "AWAITING_APPROVAL", revision)
            return identifier
        if action == "reject":
            db.execute(
                "UPDATE drafts SET status='REJECTED' WHERE tenant_id=%s AND id=%s",
                (tenant, draft_id),
            )
            event(
                db,
                tenant,
                thread,
                revision,
                "draft_rejected",
                draft_id=str(draft_id),
                version=version,
            )
            store.set_state(db, tenant, thread, "REJECTED", revision)
            return draft_id
        approval_id = uuid4()
        db.execute(
            """INSERT INTO approvals(tenant_id,id,draft_id,version,revision,policy_version,digest)
                      VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (tenant, approval_id, draft_id, version, revision, policy_version, draft["digest"]),
        )
        if real:
            envelope = draft_envelope(db, tenant, thread, draft["analysis_id"], draft["body"])
            record_mail_approval(db, tenant, approval_id, account_id, envelope, presented_digest)
        db.execute(
            "UPDATE drafts SET status='APPROVED' WHERE tenant_id=%s AND id=%s", (tenant, draft_id)
        )
        enqueue(db, approval_id, tenant, thread, revision, "deliver", approval_id=str(approval_id))
        event(
            db,
            tenant,
            thread,
            revision,
            "draft_approved",
            draft_id=str(draft_id),
            version=version,
            approval_id=str(approval_id),
        )
        store.set_state(db, tenant, thread, "APPROVED", revision)
        return approval_id
