from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime
from enum import Enum
import json
import sqlite3
from threading import RLock
from typing import Any

from .models import ApprovalRecord, Budget, Event, Job


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"cannot serialize {type(value)!r}")


def _json_hook(value: dict[str, Any]) -> Any:
    if set(value) == {"__datetime__"}:
        return datetime.fromisoformat(value["__datetime__"])
    return value


def encode(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def decode(value: str) -> Any:
    return json.loads(value, object_hook=_json_hook)


class RuntimeRepository(ABC):
    @abstractmethod
    def append_event(self, event: Event) -> bool: ...

    @abstractmethod
    def save_job(self, job: Job) -> None: ...

    @abstractmethod
    def get_job(self, tenant_id: str, company_id: str, job_id: str) -> Job | None: ...

    @abstractmethod
    def append_audit(self, record: dict[str, Any]) -> None: ...

    @abstractmethod
    def get_job_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> Job | None: ...

    @abstractmethod
    def save_approval(self, approval: ApprovalRecord) -> None: ...

    @abstractmethod
    def get_approval(self, tenant_id: str, company_id: str, approval_id: str) -> ApprovalRecord | None: ...

    @abstractmethod
    def save_budget(self, budget: Budget) -> None: ...

    @abstractmethod
    def get_budget(self, tenant_id: str, company_id: str, budget_id: str) -> Budget | None: ...

    @abstractmethod
    def save_reservation(
        self, job: Job, budget_id: str, currency: str, reserved_minor: int, settled_minor: int, state: str
    ) -> None: ...

    @abstractmethod
    def begin_invocation(self, tenant_id: str, company_id: str, key: str, job_id: str) -> bool: ...

    @abstractmethod
    def complete_invocation(self, tenant_id: str, company_id: str, key: str, result: Any) -> None: ...

    @abstractmethod
    def invocation(self, tenant_id: str, company_id: str, key: str) -> dict[str, Any] | None: ...


class SQLiteRuntimeRepository(RuntimeRepository):
    """SQLite adapter. Orchestration depends only on repository methods, not SQL."""

    MIGRATION_VERSION = 1

    def __init__(self, path: str = ":memory:") -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_scope_sequence
                    ON events(tenant_id, company_id, recorded_at, event_id);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    UNIQUE(tenant_id, company_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_scope ON jobs(tenant_id, company_id);
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budgets (
                    budget_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    budget_id TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    reserved_minor INTEGER NOT NULL,
                    settled_minor INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capability_invocations (
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result TEXT,
                    PRIMARY KEY(tenant_id, company_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    audit_event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit log is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events BEGIN
                    SELECT RAISE(ABORT, 'audit log is append-only');
                END;
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                (self.MIGRATION_VERSION,),
            )

    def append_event(self, event: Event) -> bool:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.tenant_id,
                    event.company_id,
                    event.type,
                    encode(event),
                    event.recorded_at.isoformat() if event.recorded_at else event.occurred_at.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def list_events(self, tenant_id: str, company_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT body FROM events WHERE tenant_id=? AND company_id=? ORDER BY rowid",
            (tenant_id, company_id),
        ).fetchall()
        return [decode(row["body"]) for row in rows]

    def save_job(self, job: Job) -> None:
        with self._lock, self.connection:
            self.connection.execute(
                """INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, body=excluded.body""",
                (job.job_id, job.tenant_id, job.company_id, job.idempotency_key, job.status.value, encode(job)),
            )

    def get_job(self, tenant_id: str, company_id: str, job_id: str) -> Job | None:
        row = self.connection.execute(
            "SELECT body FROM jobs WHERE tenant_id=? AND company_id=? AND job_id=?",
            (tenant_id, company_id, job_id),
        ).fetchone()
        return self._job_from_dict(decode(row["body"])) if row else None

    def get_job_by_idempotency(self, tenant_id: str, company_id: str, key: str) -> Job | None:
        row = self.connection.execute(
            "SELECT body FROM jobs WHERE tenant_id=? AND company_id=? AND idempotency_key=?",
            (tenant_id, company_id, key),
        ).fetchone()
        return self._job_from_dict(decode(row["body"])) if row else None

    def _job_from_dict(self, data: dict[str, Any]) -> Job:
        from .models import ApprovalMode, ArtifactRef, CapabilityFailure, FailureKind, JobStatus, Money, RetryPolicy

        data["status"] = JobStatus(data["status"])
        data["approval_mode"] = ApprovalMode(data["approval_mode"])
        data["dependency_ids"] = tuple(data["dependency_ids"])
        data["approval_ids"] = tuple(data["approval_ids"])
        data["artifacts"] = tuple(ArtifactRef(**item) for item in data["artifacts"])
        data["retry_policy"] = RetryPolicy(**data["retry_policy"])
        data["per_job_ceiling"] = Money(**data["per_job_ceiling"])
        if data.get("failure"):
            data["failure"]["kind"] = FailureKind(data["failure"]["kind"])
            data["failure"] = CapabilityFailure(**data["failure"])
        return Job(**data)

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO approvals VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET body=excluded.body""",
                (approval.approval_id, approval.tenant_id, approval.company_id, approval.job_id, encode(approval)),
            )

    def get_approval(self, tenant_id: str, company_id: str, approval_id: str) -> ApprovalRecord | None:
        row = self.connection.execute(
            "SELECT body FROM approvals WHERE tenant_id=? AND company_id=? AND approval_id=?",
            (tenant_id, company_id, approval_id),
        ).fetchone()
        if not row:
            return None
        from .models import ApprovalMode, ApprovalState
        data = decode(row["body"])
        data["mode"] = ApprovalMode(data["mode"])
        data["state"] = ApprovalState(data["state"])
        return ApprovalRecord(**data)

    def save_budget(self, budget: Budget) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO budgets VALUES (?, ?, ?, ?)
                ON CONFLICT(budget_id) DO UPDATE SET body=excluded.body""",
                (budget.budget_id, budget.tenant_id, budget.company_id, encode(budget)),
            )

    def get_budget(self, tenant_id: str, company_id: str, budget_id: str) -> Budget | None:
        row = self.connection.execute(
            "SELECT body FROM budgets WHERE tenant_id=? AND company_id=? AND budget_id=?",
            (tenant_id, company_id, budget_id),
        ).fetchone()
        if not row:
            return None
        from .models import Money
        data = decode(row["body"])
        data["ceiling"] = Money(**data["ceiling"])
        return Budget(**data)

    def save_reservation(
        self, job: Job, budget_id: str, currency: str, reserved_minor: int, settled_minor: int, state: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET reserved_minor=excluded.reserved_minor,
                settled_minor=excluded.settled_minor, state=excluded.state""",
                (job.job_id, job.tenant_id, job.company_id, budget_id, currency, reserved_minor, settled_minor, state),
            )

    def begin_invocation(self, tenant_id: str, company_id: str, key: str, job_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO capability_invocations VALUES (?, ?, ?, ?, 'started', NULL)",
                (tenant_id, company_id, key, job_id),
            )
        return cursor.rowcount == 1

    def complete_invocation(self, tenant_id: str, company_id: str, key: str, result: Any) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE capability_invocations SET state='completed', result=? WHERE tenant_id=? AND company_id=? AND idempotency_key=?",
                (encode(result), tenant_id, company_id, key),
            )

    def invocation(self, tenant_id: str, company_id: str, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM capability_invocations WHERE tenant_id=? AND company_id=? AND idempotency_key=?",
            (tenant_id, company_id, key),
        ).fetchone()
        return dict(row) if row else None

    def append_audit(self, record: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO audit_events VALUES (?, ?, ?, ?, ?)",
                (
                    record["audit_event_id"],
                    record["tenant_id"],
                    record["company_id"],
                    record["occurred_at"],
                    encode(record),
                ),
            )

    def list_audit(self, tenant_id: str, company_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT body FROM audit_events WHERE tenant_id=? AND company_id=? ORDER BY occurred_at, audit_event_id",
            (tenant_id, company_id),
        ).fetchall()
        return [decode(row["body"]) for row in rows]
