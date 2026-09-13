from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.agent_runtime import (
    AgentJobEnvelope,
    DeliveryState,
    ExecutionRecord,
    ExecutionState,
    ModelPolicy,
    ModelSelection,
    QueueOutboxRecord,
    TriggerClass,
)
from businessbuilder.ai_workforce import (
    ActionRequest,
    ManagementAuthorityProof,
    WorkforcePolicyService,
    safe_role_definitions,
)
from businessbuilder.postgres import PostgresRuntimeRepository, PostgresWorkforceRepository
from businessbuilder.runtime import ArtifactRef, Money
from businessbuilder.runtime.ids import DeterministicIds


NOW = datetime(2026, 9, 13, 20, 0, tzinfo=timezone.utc)


@unittest.skipUnless(
    os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
    "requires an isolated PostgreSQL test database",
)
class PostgresAgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_agent_{uuid4().hex[:10]}"

    def _envelope(self, job_id="job_postgres_agent"):
        return AgentJobEnvelope(
            "tenant_postgres_agent", "company_postgres_agent", job_id,
            "correlation-postgres-agent", "event-inbound-postgres",
            "communications.email", "v1", "classify_message",
            "role_inbox_assistant", 1,
            ("entitlement_ai", "entitlement_inbox"),
            ("ai_workforce.interact", "communications.email:classify_message"),
            (), Money("USD", 3), Money("USD", 5),
            ModelPolicy(70),
            ModelSelection("deterministic-test", "bounded-v1", Money("USD", 3), "eligible"),
            (ArtifactRef("lead", "artifact-postgres-lead"),),
            1, 3, "postgres-agent-idempotency-0001",
            TriggerClass.INBOUND_EVENT, "event-inbound-postgres",
            "businessbuilder.integration.test", "evaluation-postgres-agent",
            "sha256:policy-postgres-agent", NOW, NOW + timedelta(minutes=15),
        )

    def test_runtime_admission_outbox_leases_and_restart_are_durable(self) -> None:
        repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        envelope = self._envelope()
        execution = ExecutionRecord(
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, ExecutionState.QUEUED, 0, NOW, NOW,
        )
        outbox = QueueOutboxRecord(
            "queue-postgres-agent", "queue:postgres-agent-idempotency-0001",
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, DeliveryState.PENDING, 0, NOW, NOW,
        )
        repository.save_agent_admission(envelope, execution, outbox)
        repository.close()

        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        second = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(
                lambda item: item.claim_agent_outbox(
                    "dispatcher-a" if item is first else "dispatcher-b",
                    at=NOW, lease=timedelta(seconds=30), limit=1,
                ),
                (first, second),
            ))
        self.assertEqual(1, sum(len(item) for item in claims))
        leased = first.lease_agent_execution(
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, "worker-a", at=NOW,
            lease=timedelta(seconds=30),
        )
        self.assertEqual(1, leased.attempts)
        self.assertIsNone(second.lease_agent_execution(
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, "worker-b", at=NOW,
            lease=timedelta(seconds=30),
        ))
        recovered = second.lease_agent_execution(
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, "worker-b", at=NOW + timedelta(seconds=31),
            lease=timedelta(seconds=30),
        )
        self.assertEqual(2, recovered.attempts)
        first.close()
        second.close()

    def test_runtime_admission_rolls_back_without_partial_outbox(self) -> None:
        repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        envelope = self._envelope("job_postgres_rollback")
        execution = ExecutionRecord(
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, ExecutionState.QUEUED, 0, NOW, NOW,
        )
        outbox = QueueOutboxRecord(
            "queue-postgres-rollback", "queue:postgres-rollback-idempotency",
            envelope.tenant_id, envelope.company_id, envelope.job_id,
            envelope.envelope_digest, DeliveryState.PENDING, 0, NOW, NOW,
        )
        with self.assertRaises(RuntimeError):
            with repository.transaction():
                repository.save_agent_admission(envelope, execution, outbox)
                raise RuntimeError("simulated crash before transaction commit")
        repository.close()
        reopened = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        self.assertIsNone(reopened.get_agent_envelope(
            envelope.tenant_id, envelope.company_id, envelope.job_id
        ))
        reopened.close()

    def test_workforce_policy_survives_restart_and_remains_tenant_scoped(self) -> None:
        repository = PostgresWorkforceRepository(self.dsn, schema=self.schema)
        proof = ManagementAuthorityProof(
            "proof-postgres-workforce", "tenant-postgres-workforce",
            "company-postgres-workforce", "founder-postgres", "founder",
        )
        service = WorkforcePolicyService(
            repository,
            clock=lambda: NOW,
            id_factory=DeterministicIds(),
            authority_verifier=lambda supplied, digest: supplied == proof and digest.startswith("sha256:"),
        )
        role = safe_role_definitions(
            proof.tenant_id, proof.company_id, created_at=NOW
        )[2]
        service.create_role(role, authority=proof, idempotency_key="postgres-workforce-create")
        evaluation = service.evaluate(ActionRequest(
            "request-postgres-workforce", "postgres-workforce-evaluate",
            proof.tenant_id, proof.company_id, role.role_id, role.version,
            "communications.email", "classify_message", 3, "USD",
        ))
        self.assertTrue(evaluation.permits_execution)
        repository.close()

        reopened = PostgresWorkforceRepository(self.dsn, schema=self.schema)
        self.assertEqual(role, reopened.current_definition(
            proof.tenant_id, proof.company_id, role.role_id
        ))
        self.assertIsNone(reopened.current_definition(
            "tenant-other", proof.company_id, role.role_id
        ))
        replay = WorkforcePolicyService(reopened, clock=lambda: NOW).evaluate(
            ActionRequest(
                "request-postgres-workforce", "postgres-workforce-evaluate",
                proof.tenant_id, proof.company_id, role.role_id, role.version,
                "communications.email", "classify_message", 3, "USD",
            )
        )
        self.assertEqual(evaluation, replay)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
