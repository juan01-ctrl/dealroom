"""Read models for workspace snapshots and bounded negotiation history."""

from .identity import is_creator_workspace


class NegotiationReads:
    def __init__(self, store):
        self.store = store

    def snapshot_result(self, tenant, thread, if_none_match=None):
        with self.store.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            row = self.store.thread(db, tenant, thread)
            params = (tenant, thread)
            version = db.execute(
                "SELECT coalesce(max(id),0) AS version FROM timeline WHERE tenant_id=%s AND thread_id=%s",
                params,
            ).fetchone()["version"]
            etag = f'"v1:{thread}:{version}"'
            if if_none_match and etag in {value.strip() for value in if_none_match.split(",")}:
                return etag, None
            result = {
                "id": row["id"],
                "label": row["label"],
                "revision": row["revision"],
                "status": row["status"],
                "fault": row["fault"],
            }
            for name, query in {
                "messages": "SELECT body,revision,created_at FROM messages WHERE tenant_id=%s AND thread_id=%s ORDER BY revision",
                "drafts": """SELECT id,analysis_id,version,revision,decision,proposal,body,status,policy_version,created_at
                             FROM drafts WHERE tenant_id=%s AND thread_id=%s ORDER BY revision,version""",
                "deliveries": """SELECT delivery_key,body,created_at,mail_provider,provider_receipt_id FROM deliveries
                                 WHERE tenant_id=%s AND thread_id=%s ORDER BY created_at""",
                "recoverable_turns": """SELECT id AS job_id,revision FROM jobs
                    WHERE tenant_id=%s AND thread_id=%s AND state='FAILED' AND kind='analyze'
                    ORDER BY revision""",
            }.items():
                result[name] = db.execute(query, params).fetchall()
            page = self._timeline(db, tenant, thread)
            result.update(timeline=page["timeline"], timeline_has_more=page["has_more"])
            from .mail_delivery import draft_envelope

            real = is_creator_workspace(db, tenant)
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
