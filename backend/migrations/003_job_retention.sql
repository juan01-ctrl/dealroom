ALTER TABLE jobs ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX jobs_usage_limit ON jobs (tenant_id, kind, created_at);
