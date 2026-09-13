CREATE TABLE companies(
  tenant_id TEXT NOT NULL,
  company_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  readiness TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, company_id)
);

CREATE TABLE brain_records(
  tenant_id TEXT NOT NULL,
  company_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 1),
  kind TEXT NOT NULL,
  data_json TEXT NOT NULL,
  knowledge_class TEXT NOT NULL,
  confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  owner_ref_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  supersedes_version INTEGER,
  superseded_by_version INTEGER,
  invalidated_at TEXT,
  invalidation_reason TEXT,
  PRIMARY KEY(tenant_id, company_id, record_id, version),
  FOREIGN KEY(tenant_id, company_id) REFERENCES companies(tenant_id, company_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX one_current_record
ON brain_records(tenant_id, company_id, record_id)
WHERE superseded_by_version IS NULL;

CREATE INDEX current_records_by_kind
ON brain_records(tenant_id, company_id, kind, record_id)
WHERE superseded_by_version IS NULL;
