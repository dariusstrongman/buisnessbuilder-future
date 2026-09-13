from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

from .errors import ConflictError, NotFoundError
from .model import (
    BrainRecord, Company, Dependency, EntityRef, InvalidationNotice, KnowledgeClass,
    LifecycleState, Provenance, RecordKind, Scope,
)


class SQLiteCompanyBrainRepository:
    """Development adapter. Domain/service code depends only on the repository protocol."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(database))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")

    def migrate(self) -> None:
        migrations = Path(__file__).with_name("migrations")
        self.connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        for path in sorted(migrations.glob("*.sql")):
            exists = self.connection.execute("SELECT 1 FROM schema_migrations WHERE version=?", (path.name,)).fetchone()
            if exists:
                continue
            with self.connection:
                self.connection.executescript(path.read_text(encoding="utf-8"))
                self.connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))", (path.name,))

    def close(self) -> None:
        self.connection.close()

    def create_company(self, company: Company) -> Company:
        payload = json.dumps(company.to_contract(), sort_keys=True, separators=(",", ":"))
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO companies(tenant_id,company_id,payload,lifecycle,readiness,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (company.scope.tenant_id, company.scope.company_id, payload, company.lifecycle.value, company.readiness, company.version, company.created_at, company.updated_at),
                )
                self._insert_company_version(company, payload)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("company already exists in this tenant") from exc
        return company

    def get_company(self, scope: Scope) -> Company:
        row = self.connection.execute(
            "SELECT * FROM companies WHERE tenant_id=? AND company_id=?", (scope.tenant_id, scope.company_id)
        ).fetchone()
        if not row:
            raise NotFoundError("company not found")
        return self._company(row)

    def company_history(self, scope: Scope) -> list[Company]:
        self.get_company(scope)
        rows = self.connection.execute(
            "SELECT * FROM company_versions WHERE tenant_id=? AND company_id=? ORDER BY version",
            (scope.tenant_id, scope.company_id),
        )
        return [self._company(row) for row in rows]

    def save_company(self, company: Company, *, expected_version: int) -> Company:
        payload = json.dumps(company.to_contract(), sort_keys=True, separators=(",", ":"))
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE companies SET payload=?,lifecycle=?,readiness=?,version=?,updated_at=? WHERE tenant_id=? AND company_id=? AND version=?",
                (payload, company.lifecycle.value, company.readiness, company.version, company.updated_at,
                 company.scope.tenant_id, company.scope.company_id, expected_version),
            )
            if cursor.rowcount == 1:
                self._insert_company_version(company, payload)
        if cursor.rowcount != 1:
            if self.connection.execute("SELECT 1 FROM companies WHERE tenant_id=? AND company_id=?", (company.scope.tenant_id, company.scope.company_id)).fetchone():
                raise ConflictError("company version conflict")
            raise NotFoundError("company not found")
        return company

    def append_record(self, record: BrainRecord, *, expected_version: int | None) -> BrainRecord:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.connection.execute(
                "SELECT version FROM brain_records WHERE tenant_id=? AND company_id=? AND record_id=? AND superseded_by_version IS NULL",
                (record.scope.tenant_id, record.scope.company_id, record.record_id),
            ).fetchone()
            if current is None and expected_version is not None:
                raise ConflictError("record does not exist at expected version")
            if current is not None and (expected_version is None or current["version"] != expected_version):
                raise ConflictError("record version conflict")
            if current is not None:
                self.connection.execute(
                    "UPDATE brain_records SET superseded_by_version=? WHERE tenant_id=? AND company_id=? AND record_id=? AND version=? AND superseded_by_version IS NULL",
                    (record.version, record.scope.tenant_id, record.scope.company_id, record.record_id, current["version"]),
                )
            self.connection.execute(
                """INSERT INTO brain_records(
                tenant_id,company_id,record_id,version,kind,data_json,knowledge_class,confidence,
                owner_ref_json,provenance_json,lifecycle,created_at,updated_at,supersedes_version,
                superseded_by_version,invalidated_at,invalidation_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.scope.tenant_id, record.scope.company_id, record.record_id, record.version,
                 record.kind.value, json.dumps(dict(record.data), sort_keys=True, separators=(",", ":")),
                 record.knowledge_class.value, record.confidence,
                 json.dumps(record.owner_ref.to_dict(), sort_keys=True),
                 json.dumps([p.to_dict() for p in record.provenance], sort_keys=True),
                 record.lifecycle, record.created_at, record.updated_at, record.supersedes_version,
                 record.superseded_by_version, record.invalidated_at, record.invalidation_reason),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return record

    def get_record(self, scope: Scope, record_id: str) -> BrainRecord:
        row = self.connection.execute(
            "SELECT * FROM brain_records WHERE tenant_id=? AND company_id=? AND record_id=? AND superseded_by_version IS NULL",
            (scope.tenant_id, scope.company_id, record_id),
        ).fetchone()
        if not row:
            raise NotFoundError("record not found")
        return self._record(row)

    def list_current(self, scope: Scope, *, kinds: Sequence[RecordKind] | None = None) -> list[BrainRecord]:
        sql = "SELECT * FROM brain_records WHERE tenant_id=? AND company_id=? AND superseded_by_version IS NULL"
        params: list[object] = [scope.tenant_id, scope.company_id]
        if kinds:
            sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kind.value for kind in kinds)
        sql += " ORDER BY kind, record_id"
        return [self._record(row) for row in self.connection.execute(sql, params)]

    def history(self, scope: Scope, record_id: str | None = None) -> list[BrainRecord]:
        sql = "SELECT * FROM brain_records WHERE tenant_id=? AND company_id=?"
        params: list[object] = [scope.tenant_id, scope.company_id]
        if record_id:
            sql += " AND record_id=?"
            params.append(record_id)
        sql += " ORDER BY kind,record_id,version"
        return [self._record(row) for row in self.connection.execute(sql, params)]

    def add_dependency(self, dependency: Dependency) -> None:
        # Verify source in the same scope; a foreign-scope ID is indistinguishable from missing.
        if dependency.source_record_id.startswith("company."):
            if dependency.source_record_id not in {"company.owners", "company.jurisdiction"}:
                raise NotFoundError("unsupported company dependency source")
            self.get_company(dependency.scope)
        else:
            self.get_record(dependency.scope, dependency.source_record_id)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO brain_dependencies(tenant_id,company_id,source_record_id,dependent_type,dependent_id,dependent_version,trigger,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (dependency.scope.tenant_id, dependency.scope.company_id, dependency.source_record_id,
                 dependency.dependent_ref.type, dependency.dependent_ref.id, dependency.dependent_ref.version,
                 dependency.trigger, dependency.created_at),
            )

    def dependencies_for(self, scope: Scope, source_record_id: str) -> list[Dependency]:
        rows = self.connection.execute(
            "SELECT * FROM brain_dependencies WHERE tenant_id=? AND company_id=? AND source_record_id=? ORDER BY dependent_type,dependent_id",
            (scope.tenant_id, scope.company_id, source_record_id),
        )
        return [Dependency(scope, row["source_record_id"], EntityRef(row["dependent_type"], row["dependent_id"], row["dependent_version"]), row["trigger"], row["created_at"]) for row in rows]

    def append_invalidation(self, notice: InvalidationNotice) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO invalidation_notices(tenant_id,company_id,notice_id,source_record_id,source_version,dependent_type,dependent_id,dependent_version,trigger,reason,occurred_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (notice.scope.tenant_id, notice.scope.company_id, notice.notice_id, notice.source_record_id,
                 notice.source_version, notice.dependent_ref.type, notice.dependent_ref.id,
                 notice.dependent_ref.version, notice.trigger, notice.reason, notice.occurred_at),
            )

    def list_invalidations(self, scope: Scope) -> list[InvalidationNotice]:
        rows = self.connection.execute(
            "SELECT * FROM invalidation_notices WHERE tenant_id=? AND company_id=? ORDER BY occurred_at,notice_id",
            (scope.tenant_id, scope.company_id),
        )
        return [InvalidationNotice(scope, row["notice_id"], row["source_record_id"], row["source_version"],
                                   EntityRef(row["dependent_type"], row["dependent_id"], row["dependent_version"]),
                                   row["trigger"], row["reason"], row["occurred_at"]) for row in rows]

    def _insert_company_version(self, company: Company, payload: str) -> None:
        self.connection.execute(
            "INSERT INTO company_versions(tenant_id,company_id,version,payload,lifecycle,readiness,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (company.scope.tenant_id, company.scope.company_id, company.version, payload,
             company.lifecycle.value, company.readiness, company.created_at, company.updated_at),
        )

    @staticmethod
    def _company(row: sqlite3.Row) -> Company:
        value = json.loads(row["payload"])
        return Company(
            scope=Scope(row["tenant_id"], row["company_id"]), display_name=value["display_name"],
            legal_name=value.get("legal_name"), archetype=value["archetype"], jurisdiction=value["jurisdiction"],
            owner_refs=tuple(EntityRef(**item) for item in value["owner_refs"]),
            lifecycle=LifecycleState(row["lifecycle"]), readiness=row["readiness"],
            permissions=tuple(value.get("permissions", [])),
            provenance=tuple(SQLiteCompanyBrainRepository._provenance(item) for item in value["provenance"]),
            version=row["version"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _provenance(value: dict) -> Provenance:
        value = dict(value)
        value["actor_ref"] = EntityRef(**value["actor_ref"])
        return Provenance(**value)

    @staticmethod
    def _record(row: sqlite3.Row) -> BrainRecord:
        return BrainRecord(
            scope=Scope(row["tenant_id"], row["company_id"]), record_id=row["record_id"],
            kind=RecordKind(row["kind"]), data=json.loads(row["data_json"]),
            knowledge_class=KnowledgeClass(row["knowledge_class"]), confidence=row["confidence"],
            owner_ref=EntityRef(**json.loads(row["owner_ref_json"])),
            provenance=tuple(SQLiteCompanyBrainRepository._provenance(item) for item in json.loads(row["provenance_json"])),
            lifecycle=row["lifecycle"], version=row["version"], created_at=row["created_at"],
            updated_at=row["updated_at"], supersedes_version=row["supersedes_version"],
            superseded_by_version=row["superseded_by_version"], invalidated_at=row["invalidated_at"],
            invalidation_reason=row["invalidation_reason"],
        )
