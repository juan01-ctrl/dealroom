CREATE TABLE campaign_briefs (
    tenant_id uuid REFERENCES creator_workspaces ON DELETE CASCADE,
    document_id text CHECK (document_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(document_id) <= 80),
    source_url text NOT NULL CHECK (length(source_url) <= 512),
    revoked_at timestamptz,
    PRIMARY KEY (tenant_id, document_id),
    UNIQUE (tenant_id, source_url)
);

CREATE TABLE campaign_brief_revisions (
    tenant_id uuid,
    document_id text,
    revision integer CHECK (revision > 0),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 160),
    sections jsonb NOT NULL CHECK (jsonb_typeof(sections) = 'array'
        AND jsonb_array_length(sections) BETWEEN 1 AND 16),
    registered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id, revision),
    FOREIGN KEY (tenant_id, document_id) REFERENCES campaign_briefs ON DELETE CASCADE
);
