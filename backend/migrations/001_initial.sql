CREATE TABLE tenants (
    id uuid PRIMARY KEY,
    session_hash text UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    policy_version int NOT NULL DEFAULT 1
);
CREATE TABLE policies (
    tenant_id uuid REFERENCES tenants ON DELETE CASCADE,
    version int,
    data jsonb NOT NULL,
    PRIMARY KEY (tenant_id, version)
);
CREATE TABLE preferences (
    tenant_id uuid REFERENCES tenants ON DELETE CASCADE,
    id uuid,
    evidence text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE negotiations (
    tenant_id uuid REFERENCES tenants ON DELETE CASCADE,
    id uuid,
    label text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revision int NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'NEW',
    fault text,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE messages (
    tenant_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    event_id text NOT NULL,
    job_id uuid NOT NULL,
    revision int NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, thread_id, revision),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE
);
CREATE TABLE offers (
    tenant_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    revision int NOT NULL,
    terms jsonb NOT NULL,
    PRIMARY KEY (tenant_id, thread_id, revision),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE
);
CREATE TABLE drafts (
    tenant_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    id uuid NOT NULL,
    analysis_id uuid NOT NULL,
    version int NOT NULL,
    revision int NOT NULL,
    policy_version int NOT NULL,
    decision jsonb NOT NULL,
    proposal jsonb NOT NULL,
    body text NOT NULL,
    digest text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, analysis_id, version),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, policy_version) REFERENCES policies
);
CREATE TABLE approvals (
    tenant_id uuid NOT NULL,
    id uuid NOT NULL,
    draft_id uuid NOT NULL,
    version int NOT NULL,
    revision int NOT NULL,
    policy_version int NOT NULL,
    digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, draft_id),
    FOREIGN KEY (tenant_id, draft_id) REFERENCES drafts ON DELETE CASCADE
);
CREATE TABLE deliveries (
    tenant_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    delivery_key uuid NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, delivery_key),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, delivery_key) REFERENCES approvals(tenant_id, id) ON DELETE CASCADE
);
CREATE TABLE timeline (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    revision int NOT NULL,
    kind text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE
);
CREATE INDEX timeline_thread ON timeline (tenant_id, thread_id, id);
CREATE TABLE eval_runs (
    tenant_id uuid REFERENCES tenants ON DELETE CASCADE,
    id uuid PRIMARY KEY,
    status text NOT NULL DEFAULT 'QUEUED',
    mode text NOT NULL,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
    thread_id uuid,
    revision int,
    kind text NOT NULL CHECK (kind IN ('analyze', 'deliver', 'evaluate')),
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'QUEUED',
    attempts int NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    lease_token uuid,
    worker_id text,
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE
);
CREATE INDEX jobs_ready ON jobs (available_at, id) WHERE state IN ('QUEUED', 'RUNNING');
CREATE INDEX jobs_thread ON jobs (tenant_id, thread_id, revision) WHERE kind = 'analyze';
