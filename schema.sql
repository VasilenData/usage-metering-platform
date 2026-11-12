-- Bootstrap runs as an administrative identity; request queries use meter_runtime.
CREATE TABLE IF NOT EXISTS tenants (
    id text PRIMARY KEY, name text NOT NULL, token_hash text UNIQUE NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz,
    scope text NOT NULL CHECK (scope IN ('synthetic-demo','disposable-sandbox')),
    duplicate_events bigint NOT NULL DEFAULT 0,
    seed_complete boolean NOT NULL DEFAULT false
);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS seed_complete boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS usage_events (
    tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source text NOT NULL, id text NOT NULL, occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(), units bigint NOT NULL CHECK(units <> 0),
    correction_of text, payload_sha256 text NOT NULL,
    PRIMARY KEY(tenant_id, source, id),
    FOREIGN KEY(tenant_id, source, correction_of) REFERENCES usage_events(tenant_id,source,id)
);
CREATE INDEX IF NOT EXISTS usage_month ON usage_events(tenant_id,occurred_at);
CREATE TABLE IF NOT EXISTS prices (
    tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, id text NOT NULL,
    effective_from timestamptz NOT NULL, unit_price_microusd bigint NOT NULL CHECK(unit_price_microusd >= 0),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,effective_from)
);
-- Existing releases only provisioned fixed prices at tenant creation. Preserve
-- that known-before-publication baseline; new prices receive a database clock.
ALTER TABLE prices ADD COLUMN IF NOT EXISTS recorded_at timestamptz;
UPDATE prices p SET recorded_at=t.created_at FROM tenants t
WHERE p.tenant_id=t.id AND p.recorded_at IS NULL;
ALTER TABLE prices ALTER COLUMN recorded_at SET DEFAULT clock_timestamp();
ALTER TABLE prices ALTER COLUMN recorded_at SET NOT NULL;
CREATE TABLE IF NOT EXISTS statements (
    tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    id text NOT NULL, month text NOT NULL, revision integer NOT NULL CHECK(revision > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), document jsonb NOT NULL,
    content_sha256 text NOT NULL, PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,month,revision),
    UNIQUE(tenant_id,month,content_sha256)
);
CREATE TABLE IF NOT EXISTS product_events (
    tenant_id text NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, event_id text NOT NULL,
    actor_id text NOT NULL, event_name text NOT NULL, occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(), properties jsonb NOT NULL,
    experiment_id text, variant text, payload_sha256 text NOT NULL,
    PRIMARY KEY(tenant_id,event_id)
);
CREATE INDEX IF NOT EXISTS product_time ON product_events(tenant_id,occurred_at);
