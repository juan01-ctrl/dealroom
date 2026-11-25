ALTER TABLE negotiations ADD COLUMN creation_event_id text;
CREATE UNIQUE INDEX negotiations_creation_event ON negotiations(tenant_id, creation_event_id)
    WHERE creation_event_id IS NOT NULL;

ALTER TABLE messages ADD COLUMN skipped_at timestamptz;
CREATE INDEX jobs_failed_thread ON jobs (tenant_id, thread_id, revision)
    WHERE kind = 'analyze' AND state = 'FAILED';

CREATE TABLE worker_heartbeats (
    worker_id uuid PRIMARY KEY,
    lane text NOT NULL CHECK (lane IN ('negotiation','evaluation','all')),
    identity jsonb NOT NULL,
    seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX worker_heartbeats_seen ON worker_heartbeats (seen_at);

CREATE INDEX jobs_queued_age ON jobs (kind, available_at)
    WHERE state = 'QUEUED';
