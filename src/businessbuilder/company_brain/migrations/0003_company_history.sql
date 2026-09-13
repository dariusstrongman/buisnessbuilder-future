CREATE TABLE company_versions(
  tenant_id TEXT NOT NULL,
  company_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version >= 1),
  payload TEXT NOT NULL,
  lifecycle TEXT NOT NULL,
  readiness TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id, company_id, version),
  FOREIGN KEY(tenant_id, company_id) REFERENCES companies(tenant_id, company_id) ON DELETE RESTRICT
);

INSERT INTO company_versions(
  tenant_id, company_id, version, payload, lifecycle, readiness, created_at, updated_at
)
SELECT tenant_id, company_id, version, payload, lifecycle, readiness, created_at, updated_at
FROM companies;
