from __future__ import annotations

from typing import Any

import psycopg


MIGRATION_VERSION = 7
MIGRATION_LOCK_KEY = 1_785_369_922


DDL = r"""
CREATE TABLE IF NOT EXISTS bb_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bb_identity_records (
    kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    tenant_id TEXT,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, record_key)
);
CREATE INDEX IF NOT EXISTS bb_identity_records_tenant
    ON bb_identity_records (tenant_id, kind);
CREATE TABLE IF NOT EXISTS bb_identity_audit_events (
    sequence BIGSERIAL UNIQUE,
    audit_event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bb_commercial_records (
    sequence BIGSERIAL UNIQUE,
    kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    tenant_id TEXT,
    company_id TEXT,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, scope_key, version)
);
CREATE INDEX IF NOT EXISTS bb_commercial_scope
    ON bb_commercial_records (tenant_id, company_id, kind);
CREATE TABLE IF NOT EXISTS bb_commercial_audit_events (
    sequence BIGSERIAL UNIQUE,
    audit_event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bb_processed_billing_events (
    provider TEXT NOT NULL,
    provider_event_ref TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, provider_event_ref)
);
CREATE TABLE IF NOT EXISTS bb_commercial_outbox (
    sequence BIGSERIAL UNIQUE,
    outbox_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    body TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'dispatching', 'acknowledged')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    claimed_until TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS bb_commercial_outbox_dispatch
    ON bb_commercial_outbox (state, available_at, sequence);

CREATE TABLE IF NOT EXISTS bb_runtime_events (
    sequence BIGSERIAL UNIQUE,
    event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    body TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_runtime_events_scope_sequence
    ON bb_runtime_events (tenant_id, company_id, sequence);
CREATE TABLE IF NOT EXISTS bb_runtime_jobs (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    body TEXT NOT NULL,
    UNIQUE (tenant_id, company_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS bb_runtime_jobs_scope
    ON bb_runtime_jobs (tenant_id, company_id);
CREATE TABLE IF NOT EXISTS bb_runtime_approvals (
    approval_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bb_runtime_budgets (
    budget_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bb_runtime_reservations (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    budget_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    reserved_minor INTEGER NOT NULL,
    settled_minor INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bb_runtime_capability_invocations (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    job_id TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    PRIMARY KEY (tenant_id, company_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS bb_runtime_audit_events (
    sequence BIGSERIAL UNIQUE,
    audit_event_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bb_runtime_agent_job_envelopes (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_runtime_agent_job_scope
    ON bb_runtime_agent_job_envelopes (tenant_id, company_id, job_id);
CREATE TABLE IF NOT EXISTS bb_runtime_agent_executions (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'retryable', 'succeeded', 'failed', 'cancelled')),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_runtime_agent_execution_scope
    ON bb_runtime_agent_executions (tenant_id, company_id, state);
CREATE TABLE IF NOT EXISTS bb_runtime_agent_queue_outbox (
    sequence BIGSERIAL UNIQUE,
    message_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'dispatching', 'acknowledged')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    claimed_by TEXT,
    claimed_until TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS bb_runtime_agent_queue_dispatch
    ON bb_runtime_agent_queue_outbox (state, available_at, sequence);
CREATE TABLE IF NOT EXISTS bb_runtime_schedules (
    schedule_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    next_due_at TIMESTAMPTZ NOT NULL,
    enabled BOOLEAN NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_runtime_schedules_due
    ON bb_runtime_schedules (enabled, next_due_at);

CREATE TABLE IF NOT EXISTS bb_broker_records (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, record_id)
);
CREATE INDEX IF NOT EXISTS bb_broker_records_scope
    ON bb_broker_records (tenant_id, company_id, kind, record_id);
CREATE INDEX IF NOT EXISTS bb_communications_compliance_scope
    ON bb_broker_records (tenant_id, company_id, kind, record_id)
    WHERE kind LIKE 'communication_compliance_%';
CREATE TABLE IF NOT EXISTS bb_provider_receipts (
    receipt_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'succeeded', 'failed', 'refused')),
    body TEXT NOT NULL,
    UNIQUE (tenant_id, company_id, provider, operation, idempotency_key)
);
CREATE INDEX IF NOT EXISTS bb_provider_receipts_scope
    ON bb_provider_receipts (tenant_id, company_id, job_id);
CREATE TABLE IF NOT EXISTS bb_oauth_transactions (
    state_digest TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    redirect_digest TEXT NOT NULL,
    verifier_digest TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bb_oauth_transactions_expiry
    ON bb_oauth_transactions (expires_at) WHERE consumed_at IS NULL;
CREATE TABLE IF NOT EXISTS bb_provider_refresh_leases (
    connection_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS bb_provider_callback_events (
    provider TEXT NOT NULL,
    event_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(provider, event_id)
);
CREATE TABLE IF NOT EXISTS bb_communication_send_reservations (
    reservation_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    reserved_at TIMESTAMPTZ NOT NULL,
    body TEXT NOT NULL,
    UNIQUE(tenant_id, company_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS bb_communication_send_rate_scope
    ON bb_communication_send_reservations(tenant_id, company_id, purpose, recipient_id, reserved_at);
CREATE TABLE IF NOT EXISTS bb_communication_events (
    kind TEXT NOT NULL,
    event_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(kind, event_id)
);

CREATE TABLE IF NOT EXISTS bb_ai_workforce_records (
    kind TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (kind, scope_key)
);
CREATE INDEX IF NOT EXISTS bb_ai_workforce_scope
    ON bb_ai_workforce_records (tenant_id, company_id, kind);
CREATE TABLE IF NOT EXISTS bb_ai_workforce_audit_events (
    sequence BIGSERIAL UNIQUE,
    audit_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bb_companies (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    readiness TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, company_id)
);
CREATE TABLE IF NOT EXISTS bb_company_versions (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    payload TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    readiness TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, company_id, version),
    FOREIGN KEY (tenant_id, company_id) REFERENCES bb_companies (tenant_id, company_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS bb_brain_records (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    knowledge_class TEXT NOT NULL,
    confidence DOUBLE PRECISION CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    owner_ref_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    supersedes_version INTEGER,
    superseded_by_version INTEGER,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    PRIMARY KEY (tenant_id, company_id, record_id, version),
    FOREIGN KEY (tenant_id, company_id) REFERENCES bb_companies (tenant_id, company_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS bb_one_current_record
    ON bb_brain_records (tenant_id, company_id, record_id)
    WHERE superseded_by_version IS NULL;
CREATE INDEX IF NOT EXISTS bb_current_records_by_kind
    ON bb_brain_records (tenant_id, company_id, kind, record_id)
    WHERE superseded_by_version IS NULL;
CREATE TABLE IF NOT EXISTS bb_brain_dependencies (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    dependent_type TEXT NOT NULL,
    dependent_id TEXT NOT NULL,
    dependent_version INTEGER,
    trigger TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, company_id, source_record_id, dependent_type, dependent_id),
    FOREIGN KEY (tenant_id, company_id) REFERENCES bb_companies (tenant_id, company_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS bb_invalidation_notices (
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    notice_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    dependent_type TEXT NOT NULL,
    dependent_id TEXT NOT NULL,
    dependent_version INTEGER,
    trigger TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, company_id, notice_id),
    FOREIGN KEY (tenant_id, company_id) REFERENCES bb_companies (tenant_id, company_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS bb_verification_records (
    sequence BIGSERIAL UNIQUE,
    tenant_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    verification_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, company_id, verification_id, version)
);
CREATE INDEX IF NOT EXISTS bb_verification_scope
    ON bb_verification_records (tenant_id, company_id, verification_id, version DESC);

CREATE TABLE IF NOT EXISTS bb_cloud_proofs (
    proof_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    body TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE OR REPLACE FUNCTION bb_reject_append_only_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bb_identity_audit_append_only ON bb_identity_audit_events;
CREATE TRIGGER bb_identity_audit_append_only
    BEFORE UPDATE OR DELETE ON bb_identity_audit_events
    FOR EACH ROW EXECUTE FUNCTION bb_reject_append_only_mutation();
DROP TRIGGER IF EXISTS bb_commercial_audit_append_only ON bb_commercial_audit_events;
CREATE TRIGGER bb_commercial_audit_append_only
    BEFORE UPDATE OR DELETE ON bb_commercial_audit_events
    FOR EACH ROW EXECUTE FUNCTION bb_reject_append_only_mutation();
DROP TRIGGER IF EXISTS bb_runtime_audit_append_only ON bb_runtime_audit_events;
CREATE TRIGGER bb_runtime_audit_append_only
    BEFORE UPDATE OR DELETE ON bb_runtime_audit_events
    FOR EACH ROW EXECUTE FUNCTION bb_reject_append_only_mutation();
DROP TRIGGER IF EXISTS bb_ai_workforce_audit_append_only ON bb_ai_workforce_audit_events;
CREATE TRIGGER bb_ai_workforce_audit_append_only
    BEFORE UPDATE OR DELETE ON bb_ai_workforce_audit_events
    FOR EACH ROW EXECUTE FUNCTION bb_reject_append_only_mutation();
"""


def migrate(connection: psycopg.Connection[dict[str, Any]]) -> None:
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,)
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS bb_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cursor.execute(
                "SELECT 1 AS applied FROM bb_schema_migrations WHERE version=%s",
                (MIGRATION_VERSION,),
            )
            if cursor.fetchone() is not None:
                return
            cursor.execute(DDL)
            cursor.execute(
                "INSERT INTO bb_schema_migrations(version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
                (MIGRATION_VERSION,),
            )
