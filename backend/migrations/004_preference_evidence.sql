ALTER TABLE preferences ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX preferences_recent ON preferences (tenant_id, created_at DESC);
