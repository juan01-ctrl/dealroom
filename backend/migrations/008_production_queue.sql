CREATE TABLE creator_mail_daily_usage (
    tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
    usage_date date NOT NULL,
    messages integer NOT NULL CHECK (messages > 0),
    PRIMARY KEY (tenant_id, usage_date)
);
CREATE INDEX creator_mail_daily_usage_date ON creator_mail_daily_usage (usage_date);

CREATE INDEX jobs_expired_leases ON jobs (kind, lease_until) WHERE state = 'RUNNING';
CREATE INDEX jobs_active_thread ON jobs (tenant_id, thread_id, revision)
    WHERE kind = 'analyze' AND state IN ('QUEUED', 'RUNNING');

ALTER TABLE worker_heartbeats DROP CONSTRAINT worker_heartbeats_lane_check;
ALTER TABLE worker_heartbeats ADD CONSTRAINT worker_heartbeats_lane_check
    CHECK (lane IN ('all', 'negotiation', 'analysis', 'delivery', 'evaluation'));
