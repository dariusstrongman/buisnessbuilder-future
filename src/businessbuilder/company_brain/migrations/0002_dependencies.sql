CREATE TABLE brain_dependencies(
  tenant_id TEXT NOT NULL,
  company_id TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  dependent_type TEXT NOT NULL,
  dependent_id TEXT NOT NULL,
  dependent_version INTEGER,
  trigger TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, company_id, source_record_id, dependent_type, dependent_id),
  FOREIGN KEY(tenant_id, company_id) REFERENCES companies(tenant_id, company_id) ON DELETE RESTRICT
);

CREATE TABLE invalidation_notices(
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
  PRIMARY KEY(tenant_id, company_id, notice_id),
  FOREIGN KEY(tenant_id, company_id) REFERENCES companies(tenant_id, company_id) ON DELETE RESTRICT
);
