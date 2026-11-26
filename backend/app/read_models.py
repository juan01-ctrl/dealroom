"""Read models for workspace snapshots and bounded negotiation history."""

from typing import NamedTuple
from uuid import UUID

from .identity import is_creator_workspace

HISTORY_PAGE_SIZE = 60
MAX_REVISION = 2_147_483_647
# Messages sort above every draft version of the same turn.
MESSAGE_VERSION = MAX_REVISION + 1
MESSAGE_KEY = UUID(int=0)
DRAFT_FIELDS = "id,analysis_id,version,revision,decision,proposal,body,status,policy_version,created_at"


class HistoryCursor(NamedTuple):
    revision: int
    version: int
    key: UUID

    @classmethod
    def parse(cls, value: str) -> "HistoryCursor":
        revision, version, key = value.split(":")
        cursor = cls(int(revision), int(version), UUID(key))
        if (
            not 0 < cursor.revision <= MAX_REVISION
            or not 0 < cursor.version <= MESSAGE_VERSION
            or (cursor.version == MESSAGE_VERSION) != (cursor.key == MESSAGE_KEY)
            or cursor.token() != value
        ):
            raise ValueError("Invalid history cursor.")
        return cursor

    def token(self) -> str:
        return f"{self.revision}:{self.version}:{self.key}"


class NegotiationReads:
    def __init__(self, store):
        self.store = store

    def snapshot_result(self, tenant, thread, if_none_match=None, *, paged=False):
        with self.store.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = self.store.thread(db, tenant, thread)
            params = (tenant, thread)
            version = db.execute(
                "SELECT coalesce(max(id),0) AS version FROM timeline WHERE tenant_id=%s AND thread_id=%s",
                params,
            ).fetchone()["version"]
            representation = "v1-paged" if paged else "v1"
            etag = f'"{representation}:{thread}:{version}"'
            if if_none_match and etag in {value.strip() for value in if_none_match.split(",")}:
                return etag, None
            result = {
                "id": row["id"],
                "label": row["label"],
                "revision": row["revision"],
                "status": row["status"],
                "fault": row["fault"],
            }
            if paged:
                result.update(self._history(db, tenant, thread))
            else:
                for name, query in {
                    "messages": "SELECT body,revision,created_at FROM messages WHERE tenant_id=%s AND thread_id=%s ORDER BY revision",
                    "drafts": f"""SELECT {DRAFT_FIELDS}
                                 FROM drafts WHERE tenant_id=%s AND thread_id=%s ORDER BY revision,version""",
                    "deliveries": """SELECT delivery_key,body,created_at,mail_provider,provider_receipt_id FROM deliveries
                                     WHERE tenant_id=%s AND thread_id=%s ORDER BY created_at""",
                }.items():
                    result[name] = db.execute(query, params).fetchall()
            result["recoverable_turns"] = db.execute(
                """SELECT id AS job_id,revision FROM jobs
                   WHERE tenant_id=%s AND thread_id=%s AND state='FAILED' AND kind='analyze'
                   ORDER BY revision""",
                params,
            ).fetchall()
            page = self._timeline(db, tenant, thread)
            result.update(timeline=page["timeline"], timeline_has_more=page["has_more"])
            if paged:
                recent_receipts = self._recent_receipts(
                    db, tenant, thread, page["timeline"], result["deliveries"]
                )
                if recent_receipts:
                    result["recent_receipts"] = recent_receipts
            from .mail_delivery import draft_envelope

            real = is_creator_workspace(db, tenant)
            if real and paged:
                pending = db.execute(
                    """SELECT a.revision FROM mail_attempts m
                       JOIN approvals a ON (a.tenant_id,a.id)=(m.tenant_id,m.approval_id)
                       JOIN drafts d ON (d.tenant_id,d.id)=(a.tenant_id,a.draft_id)
                       WHERE d.tenant_id=%s AND d.thread_id=%s AND m.state='PENDING_CONFIRMATION'
                       ORDER BY a.revision DESC LIMIT 1""",
                    params,
                ).fetchone()
                if pending:
                    result["unresolved_delivery_revision"] = pending["revision"]
            for draft in result["drafts"]:
                analysis_id = draft.pop("analysis_id")
                if (
                    real
                    and draft["status"] in {"PENDING", "APPROVED"}
                    and draft["revision"] == row["revision"]
                ):
                    envelope = draft_envelope(db, tenant, thread, analysis_id, draft["body"])
                    if envelope:
                        draft["mail"] = envelope.review_fields()
        return etag, result

    def history_page(self, tenant, thread, before: HistoryCursor):
        with self.store.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self.store.thread(db, tenant, thread)
            result = self._history(db, tenant, thread, before)
            for draft in result["drafts"]:
                del draft["analysis_id"]
            return result

    @staticmethod
    def _history(db, tenant, thread, before=None):
        before = before or HistoryCursor(MAX_REVISION + 1, 0, MESSAGE_KEY)
        keys = db.execute(
            """(SELECT revision,%(message_version)s AS version,%(message_key)s AS key
                FROM messages WHERE tenant_id=%(tenant)s AND thread_id=%(thread)s
                  AND revision<=%(revision)s
                  AND (revision,%(message_version)s)<(%(revision)s,%(version)s)
                ORDER BY revision DESC LIMIT %(limit)s)
               UNION ALL
               (SELECT revision,version,id FROM drafts
                WHERE tenant_id=%(tenant)s AND thread_id=%(thread)s
                  AND (revision,version,id)<(%(revision)s,%(version)s,%(key)s)
                ORDER BY revision DESC,version DESC,id DESC LIMIT %(limit)s)
               ORDER BY revision DESC,version DESC,key DESC LIMIT %(limit)s""",
            {
                "tenant": tenant,
                "thread": thread,
                "revision": before.revision,
                "version": before.version,
                "key": before.key,
                "message_version": MESSAGE_VERSION,
                "message_key": MESSAGE_KEY,
                "limit": HISTORY_PAGE_SIZE + 1,
            },
        ).fetchall()
        page = [HistoryCursor(**row) for row in keys[:HISTORY_PAGE_SIZE]]
        message_revisions = [item.revision for item in page if item.version == MESSAGE_VERSION]
        draft_ids = [item.key for item in page if item.version != MESSAGE_VERSION]
        messages = db.execute(
            """SELECT body,revision,created_at FROM messages
               WHERE tenant_id=%s AND thread_id=%s AND revision=ANY(%s) ORDER BY revision""",
            (tenant, thread, message_revisions),
        ).fetchall()
        drafts = db.execute(
            f"""SELECT {DRAFT_FIELDS} FROM drafts WHERE tenant_id=%s AND thread_id=%s
                AND id=ANY(%s) ORDER BY revision,version,id""",
            (tenant, thread, draft_ids),
        ).fetchall()
        deliveries = db.execute(
            """SELECT d.delivery_key,d.body,d.created_at,d.mail_provider,d.provider_receipt_id,
                      a.revision
               FROM approvals a
               JOIN deliveries d ON (d.tenant_id,d.delivery_key)=(a.tenant_id,a.id)
               WHERE a.tenant_id=%s AND d.thread_id=%s AND a.draft_id=ANY(%s)
               ORDER BY d.created_at,d.delivery_key""",
            (tenant, thread, draft_ids),
        ).fetchall()
        return {
            "messages": messages,
            "drafts": drafts,
            "deliveries": deliveries,
            "history_cursor": page[-1].token() if len(keys) > HISTORY_PAGE_SIZE else None,
        }

    @staticmethod
    def _recent_receipts(db, tenant, thread, timeline, deliveries):
        # A provider can confirm an earlier revision after its page has been loaded.
        # Limit supplemental receipts to the same recent audit window as the snapshot.
        missing = set()
        for entry in timeline:
            if entry["kind"] != "provider_delivery_accepted":
                continue
            approval_id = entry["details"].get("approval_id")
            if isinstance(approval_id, str):
                try:
                    missing.add(UUID(approval_id))
                except ValueError:
                    continue
        missing.difference_update(row["delivery_key"] for row in deliveries)
        if not missing:
            return []
        return db.execute(
            """SELECT d.delivery_key,d.body,d.created_at,d.mail_provider,d.provider_receipt_id,
                      a.revision,a.draft_id
               FROM deliveries d
               JOIN approvals a ON (a.tenant_id,a.id)=(d.tenant_id,d.delivery_key)
               WHERE d.tenant_id=%s AND d.thread_id=%s AND d.delivery_key=ANY(%s)
               ORDER BY d.created_at,d.delivery_key""",
            (tenant, thread, list(missing)),
        ).fetchall()

    def timeline_page(self, tenant, thread, before):
        with self.store.connect() as db:
            self.store.thread(db, tenant, thread)
            return self._timeline(db, tenant, thread, before)

    @staticmethod
    def _timeline(db, tenant, thread, before=None):
        events = db.execute(
            """SELECT id,revision,kind,details,created_at FROM timeline
               WHERE tenant_id=%s AND thread_id=%s AND (%s::bigint IS NULL OR id<%s)
               ORDER BY id DESC LIMIT 101""",
            (tenant, thread, before, before),
        ).fetchall()
        return {"timeline": list(reversed(events[:100])), "has_more": len(events) > 100}

    def analysis_input(self, job):
        tenant, thread, revision = job["tenant_id"], job["thread_id"], job["revision"]
        with self.store.connect() as db:
            policy = db.execute(
                "SELECT data FROM policies WHERE tenant_id=%s AND version=%s",
                (tenant, job["payload"]["policy_version"]),
            ).fetchone()
            message = db.execute(
                """SELECT body FROM messages WHERE tenant_id=%s AND thread_id=%s
                   AND event_id=%s AND skipped_at IS NULL""",
                (tenant, thread, job["payload"]["event_id"]),
            ).fetchone()
            if message is None:
                return None
            previous = db.execute(
                """SELECT revision,terms FROM offers WHERE tenant_id=%s AND thread_id=%s
                                  AND revision<%s ORDER BY revision DESC LIMIT 1""",
                (tenant, thread, revision),
            ).fetchone()
            recent = db.execute(
                """SELECT event_id,body,revision FROM messages
                                WHERE tenant_id=%s AND thread_id=%s AND revision>%s AND revision<%s
                                AND event_id<>%s AND skipped_at IS NULL ORDER BY revision LIMIT 1""",
                (
                    tenant,
                    thread,
                    previous["revision"] if previous else 0,
                    revision,
                    job["payload"]["event_id"],
                ),
            ).fetchall()
            last_quote = db.execute(
                """SELECT d.decision,o.terms FROM drafts d
                   JOIN offers o ON (o.tenant_id,o.thread_id,o.revision)=(d.tenant_id,d.thread_id,d.revision)
                   WHERE d.tenant_id=%s AND d.thread_id=%s AND d.revision<%s
                     AND d.policy_version=%s AND d.status='SENT' AND d.decision->>'action'='counteroffer'
                   ORDER BY d.revision DESC LIMIT 1""",
                (tenant, thread, revision, job["payload"]["policy_version"]),
            ).fetchone()
            preferences = db.execute(
                """SELECT id FROM preferences WHERE tenant_id=%s
                                      AND (data ? 'minimum_reel' OR data ? 'exclusivity_pct' OR data ? 'tone')
                                      ORDER BY created_at DESC LIMIT 4""",
                (tenant,),
            ).fetchall()
        return {
            "email": message["body"],
            "policy": policy["data"],
            "previous": previous["terms"] if previous else {},
            "unprocessed": recent,
            "last_quote": last_quote,
            "context_ids": ([f"offer:{previous['revision']}"] if previous else [])
            + [f"preference:{item['id']}" for item in preferences],
        }
