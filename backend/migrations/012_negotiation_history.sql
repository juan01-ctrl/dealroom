CREATE INDEX drafts_thread_history
    ON drafts (tenant_id, thread_id, revision DESC, version DESC, id DESC);
