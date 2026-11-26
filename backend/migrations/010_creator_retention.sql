CREATE INDEX jobs_negotiation ON jobs (tenant_id, thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX drafts_negotiation ON drafts (tenant_id, thread_id);
