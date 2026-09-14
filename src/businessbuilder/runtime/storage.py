from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
import json
import sqlite3
from threading import RLock
from typing import Any, Iterator

from .models import ApprovalRecord, Budget, Event, Job


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
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
    def transaction(self) -> Iterator[None]: ...

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
    def list_jobs(self, tenant_id: str, company_id: str) -> tuple[Job, ...]: ...

    @abstractmethod
    def save_approval(self, approval: ApprovalRecord) -> None: ...

    @abstractmethod
    def get_approval(self, tenant_id: str, company_id: str, approval_id: str) -> ApprovalRecord | None: ...

    @abstractmethod
    def list_approvals(self, tenant_id: str, company_id: str) -> tuple[ApprovalRecord, ...]: ...

    @abstractmethod
    def save_budget(self, budget: Budget) -> None: ...

    @abstractmethod
    def get_budget(self, tenant_id: str, company_id: str, budget_id: str) -> Budget | None: ...

    @abstractmethod
    def list_budgets(self, tenant_id: str, company_id: str) -> tuple[Budget, ...]: ...

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

    @abstractmethod
    def save_agent_admission(self, envelope: Any, execution: Any, outbox: Any) -> None: ...

    @abstractmethod
    def get_agent_envelope(self, tenant_id: str, company_id: str, job_id: str) -> Any: ...

    @abstractmethod
    def get_agent_execution(self, tenant_id: str, company_id: str, job_id: str) -> Any: ...

    @abstractmethod
    def list_agent_executions(self, tenant_id: str, company_id: str) -> tuple[Any, ...]: ...

    @abstractmethod
    def save_agent_execution(self, execution: Any) -> None: ...

    @abstractmethod
    def lease_agent_execution(self, tenant_id: str, company_id: str, job_id: str, envelope_digest: str, worker_id: str, *, at: datetime, lease: timedelta) -> Any: ...

    @abstractmethod
    def claim_agent_outbox(self, dispatcher_id: str, *, at: datetime, lease: timedelta, limit: int) -> tuple[Any, ...]: ...

    @abstractmethod
    def acknowledge_agent_outbox(self, message_id: str, dispatcher_id: str, *, at: datetime) -> Any: ...

    @abstractmethod
    def release_agent_outbox(self, message_id: str, dispatcher_id: str, *, retry_at: datetime, error: str) -> Any: ...

    @abstractmethod
    def save_runtime_schedule(self, schedule: Any) -> None: ...

    @abstractmethod
    def get_runtime_schedule(self, tenant_id: str, company_id: str, schedule_id: str) -> Any: ...

    @abstractmethod
    def list_due_runtime_schedules(self, *, at: datetime, limit: int) -> tuple[Any, ...]: ...

    @abstractmethod
    def save_broker_record(self, kind: str, record_id: str, tenant_id: str, company_id: str, record: Any) -> None: ...

    @abstractmethod
    def get_broker_record(self, kind: str, tenant_id: str, company_id: str, record_id: str) -> Any: ...

    @abstractmethod
    def list_broker_records(self, kind: str, tenant_id: str, company_id: str) -> tuple[Any, ...]: ...

    @abstractmethod
    def get_broker_record_by_id(self, kind: str, record_id: str) -> Any: ...

    @abstractmethod
    def claim_provider_receipt(self, receipt: Any) -> tuple[Any, bool]: ...

    @abstractmethod
    def complete_provider_receipt(self, receipt: Any) -> None: ...

    @abstractmethod
    def get_provider_receipt(self, tenant_id: str, company_id: str, provider: str, operation: str, idempotency_key: str) -> Any: ...

    @abstractmethod
    def list_provider_receipts(self, tenant_id: str, company_id: str) -> tuple[Any, ...]: ...

    @abstractmethod
    def save_oauth_transaction(self, transaction: Any) -> None: ...

    @abstractmethod
    def get_oauth_transaction(self, state_digest: str) -> Any: ...

    @abstractmethod
    def consume_oauth_transaction(self, state_digest: str, session_id: str,
                                  redirect_digest: str, verifier_digest: str, *, at: datetime) -> Any: ...

    @abstractmethod
    def claim_provider_refresh(self, tenant_id: str, company_id: str, connection_id: str,
                               owner: str, *, at: datetime, lease: timedelta) -> bool: ...

    @abstractmethod
    def release_provider_refresh(self, tenant_id: str, company_id: str,
                                 connection_id: str, owner: str) -> None: ...

    @abstractmethod
    def claim_provider_callback(self, provider: str, event_id: str) -> bool: ...

    @abstractmethod
    def reserve_communication_send(self, reservation: Any, limits: dict[str, int]) -> tuple[Any, bool, str]: ...

    @abstractmethod
    def claim_communication_event(self, kind: str, event_id: str) -> bool: ...


class SQLiteRuntimeRepository(RuntimeRepository):
    """SQLite adapter. Orchestration depends only on repository methods, not SQL."""

    MIGRATION_VERSION = 1

    def __init__(self, path: str = ":memory:") -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._transaction_depth = 0
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
                CREATE TABLE IF NOT EXISTS agent_job_envelopes (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_execution_records (
                    job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until TEXT,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_queue_outbox (
                    message_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_until TEXT,
                    acknowledged_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS agent_queue_outbox_dispatch
                    ON agent_queue_outbox(state, available_at, message_id);
                CREATE TABLE IF NOT EXISTS runtime_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    next_due_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS broker_records (
                    kind TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY(kind, record_id)
                );
                CREATE INDEX IF NOT EXISTS broker_records_scope
                    ON broker_records(tenant_id, company_id, kind, record_id);
                CREATE TABLE IF NOT EXISTS provider_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    UNIQUE(tenant_id, company_id, provider, operation, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS provider_receipts_scope
                    ON provider_receipts(tenant_id, company_id, job_id);
                CREATE TABLE IF NOT EXISTS oauth_transactions (
                    state_digest TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    redirect_digest TEXT NOT NULL,
                    verifier_digest TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_refresh_leases (
                    connection_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    lease_until TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_callback_events (
                    provider TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY(provider, event_id)
                );
                CREATE TABLE IF NOT EXISTS communication_send_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    company_id TEXT NOT NULL,
                    recipient_id TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    body TEXT NOT NULL,
                    UNIQUE(tenant_id, company_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS communication_send_rate_scope
                    ON communication_send_reservations(tenant_id, company_id, purpose, recipient_id, reserved_at);
                CREATE TABLE IF NOT EXISTS communication_events (
                    kind TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY(kind, event_id)
                );
                """
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                (self.MIGRATION_VERSION,),
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            if self._transaction_depth:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                return
            self._transaction_depth = 1
            try:
                self.connection.execute("BEGIN")
                yield
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                self._transaction_depth = 0

    @contextmanager
    def _write(self) -> Iterator[None]:
        if self._transaction_depth:
            yield
        else:
            with self.connection:
                yield

    def append_event(self, event: Event) -> bool:
        with self._lock, self._write():
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
        with self._lock, self._write():
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

    def list_jobs(self, tenant_id: str, company_id: str) -> tuple[Job, ...]:
        rows = self.connection.execute(
            "SELECT body FROM jobs WHERE tenant_id=? AND company_id=? ORDER BY job_id",
            (tenant_id, company_id),
        ).fetchall()
        return tuple(self._job_from_dict(decode(row["body"])) for row in rows)

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
        with self._write():
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

    def list_approvals(self, tenant_id: str, company_id: str) -> tuple[ApprovalRecord, ...]:
        rows = self.connection.execute(
            "SELECT body FROM approvals WHERE tenant_id=? AND company_id=? ORDER BY rowid",
            (tenant_id, company_id),
        ).fetchall()
        values: list[ApprovalRecord] = []
        for row in rows:
            from .models import ApprovalMode, ApprovalState
            data = decode(row["body"])
            data["mode"] = ApprovalMode(data["mode"])
            data["state"] = ApprovalState(data["state"])
            values.append(ApprovalRecord(**data))
        return tuple(values)

    def save_budget(self, budget: Budget) -> None:
        with self._write():
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

    def list_budgets(self, tenant_id: str, company_id: str) -> tuple[Budget, ...]:
        rows = self.connection.execute(
            "SELECT body FROM budgets WHERE tenant_id=? AND company_id=? ORDER BY budget_id",
            (tenant_id, company_id),
        ).fetchall()
        from .models import Money
        values: list[Budget] = []
        for row in rows:
            data = decode(row["body"])
            data["ceiling"] = Money(**data["ceiling"])
            values.append(Budget(**data))
        return tuple(values)

    def save_reservation(
        self, job: Job, budget_id: str, currency: str, reserved_minor: int, settled_minor: int, state: str
    ) -> None:
        with self._write():
            self.connection.execute(
                """INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET reserved_minor=excluded.reserved_minor,
                settled_minor=excluded.settled_minor, state=excluded.state""",
                (job.job_id, job.tenant_id, job.company_id, budget_id, currency, reserved_minor, settled_minor, state),
            )

    def begin_invocation(self, tenant_id: str, company_id: str, key: str, job_id: str) -> bool:
        with self._write():
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO capability_invocations VALUES (?, ?, ?, ?, 'started', NULL)",
                (tenant_id, company_id, key, job_id),
            )
        return cursor.rowcount == 1

    def complete_invocation(self, tenant_id: str, company_id: str, key: str, result: Any) -> None:
        with self._write():
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
        with self._write():
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

    @staticmethod
    def _agent_envelope(value: str):
        from businessbuilder.agent_runtime.models import AgentJobEnvelope
        return AgentJobEnvelope.from_payload(decode(value))

    @staticmethod
    def _agent_execution(value: str):
        from businessbuilder.agent_runtime.models import ExecutionRecord, ExecutionState
        from .models import ArtifactRef, Money
        data = decode(value)
        data["state"] = ExecutionState(data["state"])
        data["actual_cost"] = Money(**data["actual_cost"]) if data.get("actual_cost") else None
        data["artifact_refs"] = tuple(ArtifactRef(**item) for item in data.get("artifact_refs", ()))
        data["emitted_event_ids"] = tuple(data.get("emitted_event_ids", ()))
        return ExecutionRecord(**data)

    @staticmethod
    def _agent_outbox(row):
        from businessbuilder.agent_runtime.models import DeliveryState, QueueOutboxRecord
        return QueueOutboxRecord(
            row["message_id"], row["idempotency_key"], row["tenant_id"],
            row["company_id"], row["job_id"], row["envelope_digest"],
            DeliveryState(row["state"]), row["attempts"],
            datetime.fromisoformat(row["available_at"]), datetime.fromisoformat(row["created_at"]),
            row["claimed_by"], datetime.fromisoformat(row["claimed_until"]) if row["claimed_until"] else None,
            datetime.fromisoformat(row["acknowledged_at"]) if row["acknowledged_at"] else None,
            row["last_error"],
        )

    def save_agent_admission(self, envelope, execution, outbox) -> None:
        with self._write():
            self.connection.execute(
                "INSERT INTO agent_job_envelopes VALUES (?, ?, ?, ?, ?)",
                (envelope.job_id, envelope.tenant_id, envelope.company_id, envelope.envelope_digest, encode(envelope.to_payload())),
            )
            self.connection.execute(
                "INSERT INTO agent_execution_records VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                (execution.job_id, execution.tenant_id, execution.company_id, execution.state.value, encode(execution)),
            )
            self.connection.execute(
                "INSERT INTO agent_queue_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                (outbox.message_id, outbox.idempotency_key, outbox.tenant_id, outbox.company_id, outbox.job_id, outbox.envelope_digest, outbox.state.value, outbox.attempts, outbox.available_at.isoformat(), outbox.created_at.isoformat()),
            )

    def get_agent_envelope(self, tenant_id: str, company_id: str, job_id: str):
        row = self.connection.execute(
            "SELECT body FROM agent_job_envelopes WHERE tenant_id=? AND company_id=? AND job_id=?",
            (tenant_id, company_id, job_id),
        ).fetchone()
        return self._agent_envelope(row["body"]) if row else None

    def get_agent_execution(self, tenant_id: str, company_id: str, job_id: str):
        row = self.connection.execute(
            "SELECT body FROM agent_execution_records WHERE tenant_id=? AND company_id=? AND job_id=?",
            (tenant_id, company_id, job_id),
        ).fetchone()
        return self._agent_execution(row["body"]) if row else None

    def list_agent_executions(self, tenant_id: str, company_id: str) -> tuple[Any, ...]:
        rows = self.connection.execute(
            "SELECT body FROM agent_execution_records WHERE tenant_id=? AND company_id=? ORDER BY job_id",
            (tenant_id, company_id),
        ).fetchall()
        return tuple(self._agent_execution(row["body"]) for row in rows)

    def save_agent_execution(self, execution) -> None:
        with self._write():
            self.connection.execute(
                """UPDATE agent_execution_records SET state=?, lease_owner=?, lease_until=?, body=?
                WHERE tenant_id=? AND company_id=? AND job_id=?""",
                (execution.state.value, execution.lease_owner, execution.lease_until.isoformat() if execution.lease_until else None, encode(execution), execution.tenant_id, execution.company_id, execution.job_id),
            )

    def lease_agent_execution(self, tenant_id, company_id, job_id, envelope_digest, worker_id, *, at, lease):
        from businessbuilder.agent_runtime.models import ExecutionState
        with self.transaction():
            current = self.get_agent_execution(tenant_id, company_id, job_id)
            if current is None or current.envelope_digest != envelope_digest:
                return None
            if current.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED}:
                return None
            if current.state is ExecutionState.LEASED and current.lease_until and current.lease_until > at:
                return None
            changed = __import__("dataclasses").replace(
                current, state=ExecutionState.LEASED, attempts=current.attempts + 1,
                updated_at=at, lease_owner=worker_id, lease_until=at + lease,
            )
            self.save_agent_execution(changed)
            return changed

    def claim_agent_outbox(self, dispatcher_id, *, at, lease, limit):
        from businessbuilder.agent_runtime.models import DeliveryState
        claimed = []
        with self.transaction():
            rows = self.connection.execute(
                """SELECT * FROM agent_queue_outbox WHERE
                (state='pending' AND available_at<=?) OR
                (state='dispatching' AND claimed_until<=?)
                ORDER BY message_id LIMIT ?""", (at.isoformat(), at.isoformat(), limit),
            ).fetchall()
            for row in rows:
                self.connection.execute(
                    "UPDATE agent_queue_outbox SET state='dispatching', attempts=attempts+1, claimed_by=?, claimed_until=? WHERE message_id=?",
                    (dispatcher_id, (at + lease).isoformat(), row["message_id"]),
                )
                refreshed = self.connection.execute("SELECT * FROM agent_queue_outbox WHERE message_id=?", (row["message_id"],)).fetchone()
                claimed.append(self._agent_outbox(refreshed))
        return tuple(claimed)

    def acknowledge_agent_outbox(self, message_id, dispatcher_id, *, at):
        with self._write():
            cursor = self.connection.execute(
                """UPDATE agent_queue_outbox SET state='acknowledged', acknowledged_at=?, claimed_by=NULL, claimed_until=NULL
                WHERE message_id=? AND state='dispatching' AND claimed_by=?""",
                (at.isoformat(), message_id, dispatcher_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("outbox acknowledgment claim mismatch")

    def release_agent_outbox(self, message_id, dispatcher_id, *, retry_at, error):
        with self._write():
            cursor = self.connection.execute(
                """UPDATE agent_queue_outbox SET state='pending', available_at=?, claimed_by=NULL, claimed_until=NULL, last_error=?
                WHERE message_id=? AND state='dispatching' AND claimed_by=?""",
                (retry_at.isoformat(), error[:200], message_id, dispatcher_id),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("outbox release claim mismatch")

    @staticmethod
    def _runtime_schedule(value: str):
        from businessbuilder.agent_runtime.models import RuntimeSchedule
        return RuntimeSchedule(**decode(value))

    def save_runtime_schedule(self, schedule) -> None:
        with self._write():
            self.connection.execute(
                """INSERT INTO runtime_schedules VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET next_due_at=excluded.next_due_at,
                enabled=excluded.enabled, body=excluded.body""",
                (schedule.schedule_id, schedule.tenant_id, schedule.company_id, schedule.next_due_at.isoformat(), int(schedule.enabled), encode(schedule)),
            )

    def get_runtime_schedule(self, tenant_id: str, company_id: str, schedule_id: str):
        row = self.connection.execute(
            "SELECT body FROM runtime_schedules WHERE tenant_id=? AND company_id=? AND schedule_id=?",
            (tenant_id, company_id, schedule_id),
        ).fetchone()
        return self._runtime_schedule(row["body"]) if row else None

    def list_due_runtime_schedules(self, *, at: datetime, limit: int) -> tuple[Any, ...]:
        rows = self.connection.execute(
            "SELECT body FROM runtime_schedules WHERE enabled=1 AND next_due_at<=? ORDER BY next_due_at, schedule_id LIMIT ?",
            (at.isoformat(), limit),
        ).fetchall()
        return tuple(self._runtime_schedule(row["body"]) for row in rows)

    @staticmethod
    def _broker_record(kind: str, body: str):
        from businessbuilder.access_broker.serialization import decode_broker_record
        return decode_broker_record(kind, decode(body))

    def save_broker_record(self, kind: str, record_id: str, tenant_id: str, company_id: str, record: Any) -> None:
        with self._lock, self._write():
            existing = self.connection.execute(
                "SELECT tenant_id, company_id FROM broker_records WHERE kind=? AND record_id=?",
                (kind, record_id),
            ).fetchone()
            if existing and (existing["tenant_id"], existing["company_id"]) != (tenant_id, company_id):
                raise PermissionError("broker record identifier is already owned by another scope")
            self.connection.execute(
                """INSERT INTO broker_records(kind, record_id, tenant_id, company_id, body)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, record_id) DO UPDATE SET body=excluded.body
                WHERE broker_records.tenant_id=excluded.tenant_id AND broker_records.company_id=excluded.company_id""",
                (kind, record_id, tenant_id, company_id, encode(record)),
            )

    def get_broker_record(self, kind: str, tenant_id: str, company_id: str, record_id: str):
        row = self.connection.execute(
            "SELECT body FROM broker_records WHERE kind=? AND tenant_id=? AND company_id=? AND record_id=?",
            (kind, tenant_id, company_id, record_id),
        ).fetchone()
        return self._broker_record(kind, row["body"]) if row else None

    def list_broker_records(self, kind: str, tenant_id: str, company_id: str) -> tuple[Any, ...]:
        rows = self.connection.execute(
            "SELECT body FROM broker_records WHERE kind=? AND tenant_id=? AND company_id=? ORDER BY record_id",
            (kind, tenant_id, company_id),
        ).fetchall()
        return tuple(self._broker_record(kind, row["body"]) for row in rows)

    def get_broker_record_by_id(self, kind: str, record_id: str):
        row = self.connection.execute(
            "SELECT body FROM broker_records WHERE kind=? AND record_id=?", (kind, record_id)
        ).fetchone()
        return self._broker_record(kind, row["body"]) if row else None

    @staticmethod
    def _provider_receipt(body: str):
        from businessbuilder.access_broker.serialization import decode_provider_receipt
        return decode_provider_receipt(decode(body))

    def claim_provider_receipt(self, receipt: Any) -> tuple[Any, bool]:
        from dataclasses import replace
        from businessbuilder.access_broker.models import ReceiptStatus
        with self._lock, self.transaction():
            row = self.connection.execute(
                """SELECT body FROM provider_receipts WHERE tenant_id=? AND company_id=?
                AND provider=? AND operation=? AND idempotency_key=?""",
                (receipt.tenant_id, receipt.company_id, receipt.provider, receipt.operation, receipt.idempotency_key),
            ).fetchone()
            if row:
                current = self._provider_receipt(row["body"])
                if current.status is ReceiptStatus.FAILED and current.retryable:
                    retried = replace(current, status=ReceiptStatus.IN_PROGRESS, attempts=current.attempts + 1, retryable=False, completed_at=None)
                    self.complete_provider_receipt(retried)
                    return retried, True
                return current, False
            self.connection.execute(
                "INSERT INTO provider_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (receipt.receipt_id, receipt.tenant_id, receipt.company_id, receipt.job_id, receipt.capability, receipt.provider, receipt.operation, receipt.idempotency_key, receipt.status.value, encode(receipt)),
            )
            return receipt, True

    def complete_provider_receipt(self, receipt: Any) -> None:
        with self._lock, self._write():
            cursor = self.connection.execute(
                """UPDATE provider_receipts SET status=?, body=? WHERE receipt_id=? AND tenant_id=?
                AND company_id=? AND job_id=?""",
                (receipt.status.value, encode(receipt), receipt.receipt_id, receipt.tenant_id, receipt.company_id, receipt.job_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("provider receipt not found in tenant/company scope")

    def get_provider_receipt(self, tenant_id: str, company_id: str, provider: str, operation: str, idempotency_key: str):
        row = self.connection.execute(
            """SELECT body FROM provider_receipts WHERE tenant_id=? AND company_id=?
            AND provider=? AND operation=? AND idempotency_key=?""",
            (tenant_id, company_id, provider, operation, idempotency_key),
        ).fetchone()
        return self._provider_receipt(row["body"]) if row else None

    def list_provider_receipts(self, tenant_id: str, company_id: str) -> tuple[Any, ...]:
        rows = self.connection.execute(
            "SELECT body FROM provider_receipts WHERE tenant_id=? AND company_id=? ORDER BY receipt_id",
            (tenant_id, company_id),
        ).fetchall()
        return tuple(self._provider_receipt(row["body"]) for row in rows)

    def save_oauth_transaction(self, transaction: Any) -> None:
        with self._lock, self._write():
            self.connection.execute(
                """INSERT INTO oauth_transactions VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(state_digest) DO NOTHING""",
                (transaction.state_digest, transaction.session_id, transaction.tenant_id,
                 transaction.company_id, transaction.redirect_uri_digest,
                 transaction.pkce_verifier_digest, transaction.expires_at.isoformat(), encode(transaction)),
            )

    def get_oauth_transaction(self, state_digest):
        from businessbuilder.provider_connection.serialization import decode_oauth_transaction
        row = self.connection.execute(
            "SELECT body FROM oauth_transactions WHERE state_digest=?", (state_digest,)
        ).fetchone()
        return decode_oauth_transaction(decode(row["body"])) if row else None

    def consume_oauth_transaction(self, state_digest, session_id, redirect_digest, verifier_digest, *, at):
        from businessbuilder.provider_connection.serialization import decode_oauth_transaction
        from dataclasses import replace
        with self._lock, self.transaction():
            row = self.connection.execute(
                """SELECT body FROM oauth_transactions WHERE state_digest=? AND session_id=?
                AND redirect_digest=? AND verifier_digest=? AND consumed_at IS NULL AND expires_at>?""",
                (state_digest, session_id, redirect_digest, verifier_digest, at.isoformat()),
            ).fetchone()
            if row is None:
                return None
            value = decode_oauth_transaction(decode(row["body"]))
            consumed = replace(value, consumed_at=at)
            self.connection.execute(
                "UPDATE oauth_transactions SET consumed_at=?, body=? WHERE state_digest=? AND consumed_at IS NULL",
                (at.isoformat(), encode(consumed), state_digest),
            )
            return consumed

    def claim_provider_refresh(self, tenant_id, company_id, connection_id, owner, *, at, lease):
        until = at + lease
        with self._lock, self.transaction():
            row = self.connection.execute(
                "SELECT tenant_id, company_id, owner, lease_until FROM provider_refresh_leases WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            if row and (row["tenant_id"], row["company_id"]) != (tenant_id, company_id):
                raise PermissionError("provider refresh lease belongs to another scope")
            if row and datetime.fromisoformat(row["lease_until"]) > at:
                return False
            self.connection.execute(
                """INSERT INTO provider_refresh_leases VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET owner=excluded.owner, lease_until=excluded.lease_until""",
                (connection_id, tenant_id, company_id, owner, until.isoformat()),
            )
            return True

    def release_provider_refresh(self, tenant_id, company_id, connection_id, owner):
        with self._lock, self._write():
            self.connection.execute(
                "DELETE FROM provider_refresh_leases WHERE connection_id=? AND tenant_id=? AND company_id=? AND owner=?",
                (connection_id, tenant_id, company_id, owner),
            )

    def claim_provider_callback(self, provider, event_id):
        with self._lock, self._write():
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO provider_callback_events(provider, event_id) VALUES (?, ?)",
                (provider, event_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _communication_reservation(body: str):
        from businessbuilder.outbound_communications.serialization import decode_communication_record
        return decode_communication_record("communication_rate_reservation", decode(body))

    def reserve_communication_send(self, reservation, limits):
        from datetime import timedelta
        with self._lock, self.transaction():
            row = self.connection.execute(
                """SELECT body FROM communication_send_reservations
                WHERE tenant_id=? AND company_id=? AND idempotency_key=?""",
                (reservation.tenant_id, reservation.company_id, reservation.idempotency_key),
            ).fetchone()
            if row:
                return self._communication_reservation(row["body"]), False, "duplicate"
            windows = (
                ("company_minute", None, timedelta(minutes=1)),
                ("company_hour", None, timedelta(hours=1)),
                ("company_day", None, timedelta(days=1)),
                ("recipient_minute", reservation.recipient_id, timedelta(minutes=1)),
                ("recipient_hour", reservation.recipient_id, timedelta(hours=1)),
                ("recipient_day", reservation.recipient_id, timedelta(days=1)),
                ("burst", reservation.recipient_id, timedelta(seconds=10)),
            )
            for name, recipient_id, window in windows:
                sql = """SELECT COUNT(*) AS n FROM communication_send_reservations
                    WHERE tenant_id=? AND company_id=? AND reserved_at>=?"""
                args = [reservation.tenant_id, reservation.company_id,
                        (reservation.reserved_at - window).isoformat()]
                if recipient_id is not None:
                    sql += " AND recipient_id=?"
                    args.append(recipient_id)
                count = self.connection.execute(sql, args).fetchone()["n"]
                if count >= limits[name]:
                    return reservation, False, f"{name} rate limit exceeded"
            self.connection.execute(
                "INSERT INTO communication_send_reservations VALUES (?,?,?,?,?,?,?,?)",
                (reservation.reservation_id, reservation.tenant_id, reservation.company_id,
                 reservation.recipient_id, reservation.purpose.value,
                 reservation.idempotency_key, reservation.reserved_at.isoformat(), encode(reservation)),
            )
            return reservation, True, "reserved"

    def claim_communication_event(self, kind, event_id):
        with self._lock, self._write():
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO communication_events(kind,event_id) VALUES (?,?)",
                (kind, event_id),
            )
            return cursor.rowcount == 1
