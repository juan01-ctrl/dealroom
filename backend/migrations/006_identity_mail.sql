CREATE TABLE creator_accounts (
    id uuid PRIMARY KEY,
    issuer text NOT NULL,
    subject text NOT NULL,
    email text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (issuer, subject)
);
CREATE TABLE creator_workspaces (
    tenant_id uuid PRIMARY KEY REFERENCES tenants ON DELETE CASCADE
);
CREATE TABLE workspace_memberships (
    tenant_id uuid REFERENCES creator_workspaces ON DELETE CASCADE,
    account_id uuid REFERENCES creator_accounts,
    role text NOT NULL CHECK (role IN ('owner', 'approver', 'viewer')),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, account_id)
);
CREATE TABLE mail_routes (
    tenant_id uuid REFERENCES creator_workspaces ON DELETE CASCADE,
    thread_id uuid,
    provider text NOT NULL,
    inbound_address text NOT NULL,
    sender text NOT NULL,
    recipient text NOT NULL,
    PRIMARY KEY (tenant_id, thread_id),
    UNIQUE (provider, inbound_address),
    FOREIGN KEY (tenant_id, thread_id) REFERENCES negotiations ON DELETE CASCADE
);
CREATE TABLE mail_inbound_events (
    provider text NOT NULL,
    provider_event_id text NOT NULL,
    payload_digest text NOT NULL,
    tenant_id uuid NOT NULL,
    event_id text NOT NULL,
    inbound_address text NOT NULL,
    sender text NOT NULL,
    subject text NOT NULL,
    PRIMARY KEY (provider, provider_event_id),
    UNIQUE (tenant_id, event_id),
    FOREIGN KEY (tenant_id, event_id) REFERENCES messages ON DELETE CASCADE
);
CREATE TABLE mail_approvals (
    tenant_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    account_id uuid NOT NULL,
    provider text NOT NULL,
    sender text NOT NULL,
    recipient text NOT NULL,
    subject text NOT NULL,
    envelope_digest text NOT NULL,
    PRIMARY KEY (tenant_id, approval_id),
    FOREIGN KEY (tenant_id, approval_id) REFERENCES approvals (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, account_id) REFERENCES workspace_memberships
);
CREATE TABLE mail_attempts (
    tenant_id uuid NOT NULL,
    approval_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('PENDING_CONFIRMATION', 'ACCEPTED', 'REJECTED', 'BLOCKED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, approval_id),
    FOREIGN KEY (tenant_id, approval_id) REFERENCES mail_approvals ON DELETE CASCADE
);
ALTER TABLE deliveries
    ADD COLUMN mail_provider text,
    ADD COLUMN provider_receipt_id text,
    ADD CONSTRAINT delivery_receipt_fields CHECK ((mail_provider IS NULL) = (provider_receipt_id IS NULL));
CREATE UNIQUE INDEX provider_delivery_receipts ON deliveries (mail_provider, provider_receipt_id)
    WHERE mail_provider IS NOT NULL;

-- An older worker may still be running during a rollout. Its simulated receipt
-- must never mark a creator delivery as sent.
CREATE FUNCTION require_creator_provider_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM creator_workspaces WHERE tenant_id=NEW.tenant_id)
       AND NEW.provider_receipt_id IS NULL THEN
        RAISE EXCEPTION 'Creator workspace deliveries require a provider receipt.';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER creator_provider_receipt BEFORE INSERT OR UPDATE ON deliveries
    FOR EACH ROW EXECUTE FUNCTION require_creator_provider_receipt();
