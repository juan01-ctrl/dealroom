import hashlib
import os
import secrets
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .domain import Decision, Offer, Policy, Proposal
from .identity import is_creator_workspace
from .negotiation import render_reply
from .operations import execution_identity
from .outbox import Outbox
from .persistence_events import event
from .read_models import NegotiationReads

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Limit(Exception):
    pass


class ModelBudgetExhausted(Limit):
    pass


def digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def enqueue(db, identifier, tenant, thread, revision, kind, **payload):
    if kind == "analyze":
        payload.setdefault("execution", execution_identity())
        build = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")
        if len(build) == 40 and all(char in "abcdefABCDEF0123456789" for char in build):
            payload.setdefault("queued_build", build)
        cap = 24 if os.getenv("MODEL_MODE") == "live" else 160
        usage = db.execute(
            """
            SELECT count(*) AS n FROM jobs j JOIN tenants t ON t.id=j.tenant_id
            WHERE j.tenant_id=%s AND j.kind='analyze' AND t.session_hash IS NOT NULL
            """,
            (tenant,),
        ).fetchone()["n"]
        if usage >= cap:
            raise Limit("This session has reached its analysis limit.")
    db.execute(
        "INSERT INTO jobs(id,tenant_id,thread_id,revision,kind,payload) VALUES (%s,%s,%s,%s,%s,%s)",
        (identifier, tenant, thread, revision, kind, Jsonb(payload)),
    )


def invalidate(db, tenant, thread):
    db.execute(
        """UPDATE drafts SET status='STALE' WHERE tenant_id=%s AND thread_id=%s
                  AND status IN ('PENDING','APPROVED')""",
        (tenant, thread),
    )


@contextmanager
def pooled_store(url: str, *, max_size: int):
    with ConnectionPool(
        url,
        kwargs={"row_factory": dict_row, "connect_timeout": 3},
        min_size=1,
        max_size=max_size,
        timeout=5,
        max_waiting=64,
        open=False,
    ) as pool:
        yield Store(url, pool=pool)


class Store:
    def __init__(self, url: str, *, pool=None):
        self.url = url
        self.pool = pool
        self.reads = NegotiationReads(self)
        self.outbox = Outbox(self)

    def connect(self, **kwargs):
        # Session-affine advisory locks and explicitly short readiness deadlines use dedicated connections.
        if self.pool is not None and not kwargs:
            return self.pool.connection()
        return psycopg.connect(self.url, row_factory=dict_row, **kwargs)

    def migrate(self):
        with self.connect(autocommit=True) as db:
            db.execute("SELECT pg_advisory_lock(91496201)")
            db.execute("CREATE TABLE IF NOT EXISTS schema_versions (name text PRIMARY KEY)")
            for path in sorted(MIGRATIONS.glob("*.sql")):
                with db.transaction():
                    if not db.execute(
                        "SELECT 1 FROM schema_versions WHERE name=%s", (path.name,)
                    ).fetchone():
                        db.execute(path.read_text())
                        db.execute("INSERT INTO schema_versions VALUES (%s)", (path.name,))
            PostgresSaver(db).setup()

    def check_schema(self):
        with self.connect(connect_timeout=3) as db:
            db.execute("SET LOCAL statement_timeout = 3000")
            versions = {row["name"] for row in db.execute("SELECT name FROM schema_versions")}
            if any(path.name not in versions for path in MIGRATIONS.glob("*.sql")):
                raise RuntimeError("Application migrations are pending.")
            db.execute("SELECT 1 FROM checkpoints LIMIT 0")

    def reserve_model_call(self, limit):
        with self.connect() as db:
            reserved = db.execute(
                """INSERT INTO live_model_usage(usage_date,calls)
                   VALUES ((now() AT TIME ZONE 'UTC')::date,1)
                   ON CONFLICT (usage_date) DO UPDATE SET calls=live_model_usage.calls+1
                   WHERE live_model_usage.calls<%s RETURNING calls""",
                (limit,),
            ).fetchone()
            if not reserved:
                raise ModelBudgetExhausted("The daily live model call budget has been reached.")

    def create_session(self, token: str | None = None, policy: Policy | None = None):
        tenant, thread = uuid4(), uuid4()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tenants(id,session_hash) VALUES (%s,%s)",
                (tenant, digest(token) if token else None),
            )
            db.execute(
                "INSERT INTO policies VALUES (%s,1,%s)",
                (tenant, Jsonb((policy or Policy()).model_dump())),
            )
            db.execute(
                "INSERT INTO negotiations(tenant_id,id,label) VALUES (%s,%s,%s)",
                (tenant, thread, "Reel campaign"),
            )
        return tenant, thread

    def session(self, token):
        if not token or len(token) > 100:
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT id FROM tenants WHERE session_hash=%s
                             AND created_at > now() - interval '7 days'""",
                (digest(token),),
            ).fetchone()
        return row["id"] if row else None

    def new_browser_session(self):
        token = secrets.token_urlsafe(32)
        tenant, thread = self.create_session(token)
        return token, tenant, thread

    def create_thread(self, tenant, label, event_id=None):
        identifier = uuid4()
        with self.connect() as db:
            self.tenant_lock(db, tenant)
            if event_id:
                existing = db.execute(
                    "SELECT id,label FROM negotiations WHERE tenant_id=%s AND creation_event_id=%s",
                    (tenant, event_id),
                ).fetchone()
                if existing:
                    if existing["label"] != label:
                        raise Conflict("This creation ID is already bound to a different negotiation.")
                    return existing["id"]
            if not is_creator_workspace(db, tenant) and (
                db.execute(
                    "SELECT count(*) AS n FROM negotiations WHERE tenant_id=%s", (tenant,)
                ).fetchone()["n"]
                >= 12
            ):
                raise Limit("This session has reached its negotiation limit.")
            db.execute(
                "INSERT INTO negotiations(tenant_id,id,label,creation_event_id) VALUES (%s,%s,%s,%s)",
                (tenant, identifier, label, event_id),
            )
        return identifier

    def threads(self, tenant):
        with self.connect() as db:
            return db.execute(
                """SELECT id,label,status,revision FROM negotiations
                              WHERE tenant_id=%s ORDER BY created_at""",
                (tenant,),
            ).fetchall()

    @staticmethod
    def tenant_lock(db, tenant):
        row = db.execute(
            "SELECT policy_version FROM tenants WHERE id=%s FOR UPDATE", (tenant,)
        ).fetchone()
        if not row:
            raise NotFound("Session not found.")
        return row["policy_version"]

    @staticmethod
    def thread(db, tenant, thread, *, lock=False):
        query = "SELECT * FROM negotiations WHERE tenant_id=%s AND id=%s"
        row = db.execute(query + (" FOR UPDATE" if lock else ""), (tenant, thread)).fetchone()
        if not row:
            raise NotFound("Negotiation not found.")
        return row

    def policy(self, tenant):
        with self.connect() as db:
            return db.execute(
                """
                SELECT p.version,p.data,
                    CASE WHEN recent.tone <> p.data->>'tone' THEN recent.tone END AS suggested_tone
                FROM policies p JOIN tenants t ON t.id=p.tenant_id AND t.policy_version=p.version
                LEFT JOIN LATERAL (
                    SELECT data->>'tone' AS tone FROM preferences
                    WHERE tenant_id=t.id AND data ? 'tone' ORDER BY created_at DESC LIMIT 1
                ) recent ON true
                WHERE t.id=%s
                """,
                (tenant,),
            ).fetchone()

    def update_policy(self, tenant, version, policy):
        with self.connect() as db:
            if self.tenant_lock(db, tenant) != version:
                raise Conflict("The creator policy changed. Refresh and retry.")
            new_version = version + 1
            db.execute(
                "INSERT INTO policies VALUES (%s,%s,%s)",
                (tenant, new_version, Jsonb(policy.model_dump())),
            )
            db.execute("UPDATE tenants SET policy_version=%s WHERE id=%s", (new_version, tenant))
            threads = db.execute(
                """SELECT id,revision,status FROM negotiations WHERE tenant_id=%s
                                 ORDER BY id FOR UPDATE""",
                (tenant,),
            ).fetchall()
            for row in threads:
                thread = row["id"]
                invalidate(db, tenant, thread)
                if row["status"] in {"ANALYZING", "AWAITING_APPROVAL", "APPROVED", "FAILED"}:
                    self.reanalyze(db, tenant, thread, row, new_version)
                event(db, tenant, thread, row["revision"], "policy_updated", policy_version=new_version)
        return new_version

    def ingest(self, tenant, thread, event_id, body, *, quota=80, db=None):
        with nullcontext(db) if db is not None else self.connect() as connection:
            version = self.tenant_lock(connection, tenant)
            current = self.thread(connection, tenant, thread, lock=True)
            existing = connection.execute(
                """SELECT thread_id,revision,job_id,body FROM messages
                                  WHERE tenant_id=%s AND event_id=%s""",
                (tenant, event_id),
            ).fetchone()
            if existing:
                if existing["thread_id"] != thread or existing["body"] != body:
                    raise Conflict("This event ID is already bound to a different message.")
                return {"revision": existing["revision"], "job_id": existing["job_id"]}
            if current["status"] == "CLOSED":
                raise Conflict("Reopen the negotiation before adding a reply.")
            if quota is not None and (
                connection.execute(
                    "SELECT count(*) AS n FROM messages WHERE tenant_id=%s", (tenant,)
                ).fetchone()["n"]
                >= quota
            ):
                raise Limit("This session has reached its message limit.")
            identifier, revision = uuid4(), current["revision"] + 1
            connection.execute(
                """INSERT INTO messages(tenant_id,thread_id,event_id,job_id,revision,body)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                (tenant, thread, event_id, identifier, revision, body),
            )
            invalidate(connection, tenant, thread)
            self.set_state(connection, tenant, thread, "ANALYZING", revision)
            enqueue(
                connection,
                identifier,
                tenant,
                thread,
                revision,
                "analyze",
                event_id=event_id,
                policy_version=version,
            )
            event(connection, tenant, thread, revision, "inbound_received", event_id=event_id)
        return {"revision": revision, "job_id": identifier}

    @staticmethod
    def set_state(db, tenant, thread, status, revision):
        db.execute(
            "UPDATE negotiations SET status=%s,revision=%s WHERE tenant_id=%s AND id=%s",
            (status, revision, tenant, thread),
        )
        event(db, tenant, thread, revision, "state_transition", status=status)

    def reanalyze(self, db, tenant, thread, current, policy_version):
        message = db.execute(
            """SELECT event_id FROM messages WHERE tenant_id=%s AND thread_id=%s
                             AND skipped_at IS NULL
                             ORDER BY revision DESC LIMIT 1""",
            (tenant, thread),
        ).fetchone()
        if not message:
            raise Conflict("Add a brand email before running analysis.")
        revision = current["revision"] + 1
        self.set_state(db, tenant, thread, "ANALYZING", revision)
        enqueue(
            db,
            uuid4(),
            tenant,
            thread,
            revision,
            "analyze",
            event_id=message["event_id"],
            policy_version=policy_version,
        )

    def transition(self, tenant, thread, revision, action):
        with self.connect() as db:
            version = self.tenant_lock(db, tenant)
            current = self.thread(db, tenant, thread, lock=True)
            if revision != current["revision"]:
                raise Conflict("The negotiation changed. Refresh and retry.")
            if action == "close" and current["status"] != "CLOSED":
                invalidate(db, tenant, thread)
                db.execute(
                    "UPDATE negotiations SET fault=NULL WHERE tenant_id=%s AND id=%s", (tenant, thread)
                )
                self.set_state(db, tenant, thread, "CLOSED", revision + 1)
            elif action == "reopen" and current["status"] == "CLOSED":
                self.set_state(db, tenant, thread, "NEW", revision + 1)
            elif action == "reanalyze" and current["status"] == "REJECTED":
                self.reanalyze(db, tenant, thread, current, version)
            elif action == "retry" and current["status"] == "FAILED":
                job = db.execute(
                    """UPDATE jobs SET state='QUEUED',attempts=0,available_at=now()
                                 WHERE tenant_id=%s AND thread_id=%s AND revision=%s AND state='FAILED'
                                 RETURNING kind""",
                    (tenant, thread, revision),
                ).fetchone()
                if not job:
                    raise Conflict("There is no failed job to retry.")
                self.set_state(
                    db, tenant, thread, "APPROVED" if job["kind"] == "deliver" else "ANALYZING", revision
                )
            else:
                raise Conflict("That action is unavailable in the current state.")

    def recover_turn(self, tenant, thread, revision, job_id, action):
        with self.connect() as db:
            self.tenant_lock(db, tenant)
            current = self.thread(db, tenant, thread, lock=True)
            if current["revision"] != revision or current["status"] == "CLOSED":
                raise Conflict("The negotiation changed. Refresh and retry.")
            job = db.execute(
                """SELECT revision,payload FROM jobs WHERE id=%s AND tenant_id=%s AND thread_id=%s
                   AND kind='analyze' AND state='FAILED' FOR UPDATE""",
                (job_id, tenant, thread),
            ).fetchone()
            if not job:
                raise Conflict("This turn is no longer awaiting recovery.")
            if action == "retry":
                db.execute(
                    "UPDATE jobs SET state='QUEUED',attempts=0,available_at=now() WHERE id=%s",
                    (job_id,),
                )
                if job["revision"] == revision:
                    self.set_state(db, tenant, thread, "ANALYZING", revision)
            else:
                db.execute(
                    """UPDATE messages SET skipped_at=now() WHERE tenant_id=%s AND thread_id=%s
                       AND event_id=%s""",
                    (tenant, thread, job["payload"]["event_id"]),
                )
                skipped = db.execute(
                    """UPDATE jobs SET state='SKIPPED',lease_until=NULL
                       WHERE tenant_id=%s AND thread_id=%s AND kind='analyze'
                       AND state IN ('QUEUED','RUNNING','FAILED') AND payload->>'event_id'=%s
                       RETURNING revision""",
                    (tenant, thread, job["payload"]["event_id"]),
                ).fetchall()
                if any(turn["revision"] == revision for turn in skipped):
                    sent = db.execute(
                        "SELECT 1 FROM deliveries WHERE tenant_id=%s AND thread_id=%s LIMIT 1",
                        (tenant, thread),
                    ).fetchone()
                    self.set_state(
                        db, tenant, thread, "AWAITING_BRAND_REPLY" if sent else "NEW", revision
                    )
            event(
                db,
                tenant,
                thread,
                revision,
                "turn_recovery",
                action=action,
                job_id=str(job_id),
                turn_revision=job["revision"],
            )

    def arm_fault(self, tenant, thread, revision, fault):
        with self.connect() as db:
            current = self.thread(db, tenant, thread, lock=True)
            if current["revision"] != revision or current["status"] == "CLOSED":
                raise Conflict("Refresh the negotiation before arming a failure.")
            if current["fault"]:
                raise Conflict("A simulated failure is already armed.")
            db.execute(
                "UPDATE negotiations SET fault=%s WHERE tenant_id=%s AND id=%s", (fault, tenant, thread)
            )
            event(db, tenant, thread, revision, "failure_armed", boundary=fault)

    def snapshot(self, tenant, thread):
        return self.snapshot_result(tenant, thread)[1]

    def snapshot_result(self, tenant, thread, if_none_match=None):
        return self.reads.snapshot_result(tenant, thread, if_none_match)

    def analysis_input(self, job):
        return self.reads.analysis_input(job)

    def publish(self, job, terms: Offer, decision: Decision, proposal: Proposal, telemetry):
        tenant, thread, revision = job["tenant_id"], job["thread_id"], job["revision"]
        version = job["payload"]["policy_version"]
        with self.connect() as db:
            current_version = self.tenant_lock(db, tenant)
            current = self.thread(db, tenant, thread, lock=True)
            if db.execute(
                """SELECT 1 FROM messages WHERE tenant_id=%s AND thread_id=%s
                   AND event_id=%s AND skipped_at IS NOT NULL""",
                (tenant, thread, job["payload"]["event_id"]),
            ).fetchone():
                return
            db.execute(
                "INSERT INTO offers VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant, thread, revision, Jsonb(terms.model_dump())),
            )
            if (
                current["revision"] != revision
                or current_version != version
                or current["status"] != "ANALYZING"
            ):
                return
            self.insert_draft(db, tenant, thread, job["id"], 1, revision, version, decision, proposal)
            event(
                db,
                tenant,
                thread,
                revision,
                "offer_extracted",
                terms=terms.model_dump(exclude={"brand", "usage_scope", "unmapped_obligations"}),
            )
            event(
                db,
                tenant,
                thread,
                revision,
                "policy_checked",
                policy_version=version,
                action=decision.action,
                checks=decision.checks,
                total_minor=decision.total,
            )
            event(
                db, tenant, thread, revision, "analysis_completed", turn_id=str(job["id"]), **telemetry
            )
            self.set_state(db, tenant, thread, "AWAITING_APPROVAL", revision)

    @staticmethod
    def insert_draft(
        db, tenant, thread, analysis_id, version, revision, policy_version, decision, proposal
    ):
        identifier, body = uuid4(), render_reply(decision, proposal)
        db.execute(
            """INSERT INTO drafts(tenant_id,thread_id,id,analysis_id,version,revision,policy_version,
                       decision,proposal,body,digest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                tenant,
                thread,
                identifier,
                analysis_id,
                version,
                revision,
                policy_version,
                Jsonb(decision.model_dump()),
                Jsonb(proposal.model_dump()),
                body,
                digest(body),
            ),
        )
        event(db, tenant, thread, revision, "draft_created", draft_id=str(identifier), version=version)
        return identifier

    def review(
        self,
        tenant,
        thread,
        draft_id,
        version,
        action,
        proposal=None,
        *,
        account_id=None,
        presented_digest=None,
    ):
        from .draft_review import review_draft

        return review_draft(
            self,
            tenant,
            thread,
            draft_id,
            version,
            action,
            proposal,
            account_id=account_id,
            presented_digest=presented_digest,
        )

    def deliver(self, tenant, thread, approval_id):
        with self.connect() as db:
            if is_creator_workspace(db, tenant):
                raise Conflict("Creator mail requires provider delivery.")
            policy = self.tenant_lock(db, tenant)
            current = self.thread(db, tenant, thread, lock=True)
            delivered = db.execute(
                """SELECT * FROM deliveries
                                   WHERE tenant_id=%s AND thread_id=%s AND delivery_key=%s""",
                (tenant, thread, approval_id),
            ).fetchone()
            if delivered:
                return delivered, False
            approved = db.execute(
                """SELECT d.* FROM approvals a JOIN drafts d
                                   ON (d.tenant_id,d.id)=(a.tenant_id,a.draft_id)
                                   WHERE a.tenant_id=%s AND a.id=%s AND d.thread_id=%s
                                     AND (d.version,d.revision,d.policy_version,d.digest) =
                                         (a.version,a.revision,a.policy_version,a.digest)""",
                (tenant, approval_id, thread),
            ).fetchone()
            revision = current["revision"]
            if (
                not approved
                or approved["status"] != "APPROVED"
                or current["status"] != "APPROVED"
                or approved["revision"] != revision
                or approved["policy_version"] != policy
                or digest(approved["body"]) != approved["digest"]
            ):
                event(db, tenant, thread, revision, "delivery_blocked", approval_id=str(approval_id))
                return None, False
            receipt = db.execute(
                """INSERT INTO deliveries(tenant_id,thread_id,delivery_key,body)
                                 VALUES (%s,%s,%s,%s) RETURNING *""",
                (tenant, thread, approval_id, approved["body"]),
            ).fetchone()
            db.execute(
                "UPDATE drafts SET status='SENT' WHERE tenant_id=%s AND id=%s", (tenant, approved["id"])
            )
            if approved["version"] > 1:
                tone = approved["proposal"]["tone"]
                default = db.execute(
                    "SELECT data->>'tone' AS tone FROM policies WHERE tenant_id=%s AND version=%s",
                    (tenant, policy),
                ).fetchone()["tone"]
                if tone != default:
                    db.execute(
                        "INSERT INTO preferences(tenant_id,id,evidence,data) VALUES (%s,%s,%s,%s)",
                        (
                            tenant,
                            approved["id"],
                            "Creator edited and approved the draft",
                            Jsonb({"tone": tone}),
                        ),
                    )
                    event(db, tenant, thread, revision, "preference_inferred", field="tone", value=tone)
            self.set_state(db, tenant, thread, "SENT", revision)
            event(db, tenant, thread, revision, "simulated_delivery", delivery_key=str(approval_id))
            self.set_state(db, tenant, thread, "AWAITING_BRAND_REPLY", revision)
            fault = current["fault"] == "delivery"
            if fault:
                self.consume_fault(db, tenant, thread, revision, "delivery")
            return receipt, fault

    @staticmethod
    def consume_fault(db, tenant, thread, revision, boundary):
        db.execute("UPDATE negotiations SET fault=NULL WHERE tenant_id=%s AND id=%s", (tenant, thread))
        event(db, tenant, thread, revision, "simulated_failure", boundary=boundary)

    def checkpoint_fault(self, job):
        tenant, thread, revision = job["tenant_id"], job["thread_id"], job["revision"]
        with self.connect() as db:
            current = self.thread(db, tenant, thread, lock=True)
            if current["revision"] == revision and current["fault"] == "checkpoint":
                self.consume_fault(db, tenant, thread, revision, "checkpoint")
                return True
        return False

    def claim(
        self, worker, *, identifier=None, lease=120, lane="all", identity=None, real_allowed=False
    ):
        return self.outbox.claim(
            worker,
            identifier=identifier,
            lease=lease,
            lane=lane,
            identity=identity,
            real_allowed=real_allowed,
        )

    def finish(self, job, *, retry=False, simulated=False, busy=False, immediate=False, terminal=False):
        return self.outbox.finish(
            job, retry=retry, simulated=simulated, busy=busy, immediate=immediate, terminal=terminal
        )
