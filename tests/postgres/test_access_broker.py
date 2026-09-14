from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.access_broker.models import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    CapabilityGrant,
    ConnectionStatus,
    CredentialScope,
    ExternalAccount,
    ExternalCredentialRef,
    ProviderConnection,
    ProviderReceipt,
    ReceiptStatus,
)
from businessbuilder.postgres import PostgresRuntimeRepository


NOW = datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)
TENANT = "tenant_pg_broker"
COMPANY = "company_pg_broker"


@unittest.skipUnless(
    os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
    "requires an isolated PostgreSQL test database",
)
class PostgresBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_broker_{uuid4().hex[:10]}"

    def _records(self):
        account = ExternalAccount(
            "external_account_pg", TENANT, COMPANY, "test-email",
            "external_mailbox_pg", "PG mailbox", ConnectionStatus.ACTIVE, NOW, NOW,
        )
        connection = ProviderConnection(
            "connection_pg", account.account_id, TENANT, COMPANY, account.provider,
            ConnectionStatus.ACTIVE, NOW, NOW,
        )
        credential = ExternalCredentialRef(
            "secretref_pg_email", connection.connection_id, TENANT, COMPANY,
            account.provider, "email.oauth", "arn:aws:secretsmanager:us-east-1:111122223333:secret:opaque-ref",
            ConnectionStatus.ACTIVE, NOW, NOW, NOW + timedelta(days=1),
        )
        grant = CapabilityGrant(
            "grant_pg", TENANT, COMPANY, connection.connection_id,
            "role_inbox_assistant",
            CredentialScope(
                "communications.email", frozenset({"send_preapproved_reply"}),
                frozenset({"email.oauth"}),
            ),
            frozenset({ArtifactClassification.EXTERNAL_ATTACHMENT}), NOW,
        )
        artifact = ArtifactRecord(
            "artifact_pg", TENANT, COMPANY,
            f"tenant/{TENANT}/company/{COMPANY}/artifacts/artifact_pg/" + "a" * 64,
            "a" * 64, "text/plain", 8, ArtifactClassification.EXTERNAL_ATTACHMENT,
            ArtifactStatus.AVAILABLE, "event_pg", NOW,
        )
        return account, connection, credential, grant, artifact

    def test_broker_records_survive_restart_and_remain_scoped(self) -> None:
        repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        records = self._records()
        kinds = (
            "external_account", "provider_connection", "external_credential_ref",
            "capability_grant", "artifact",
        )
        ids = (
            records[0].account_id, records[1].connection_id,
            records[2].secret_ref, records[3].grant_id, records[4].artifact_id,
        )
        for kind, record_id, record in zip(kinds, ids, records):
            repository.save_broker_record(kind, record_id, TENANT, COMPANY, record)
        repository.close()

        reopened = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        for kind, record_id, expected in zip(kinds, ids, records):
            self.assertEqual(expected, reopened.get_broker_record(kind, TENANT, COMPANY, record_id))
            self.assertIsNone(reopened.get_broker_record(kind, "tenant_other", COMPANY, record_id))
        self.assertEqual((), reopened.list_broker_records("artifact", "tenant_other", COMPANY))
        reopened.close()

    def test_concurrent_provider_receipt_claim_has_one_executor(self) -> None:
        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        second = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        receipt = ProviderReceipt(
            "receipt_pg", TENANT, COMPANY, "job_pg", "communications.email",
            "test-email", "send_preapproved_reply", "provider_request_pg",
            "provider_action_pg_idempotency", ReceiptStatus.IN_PROGRESS, NOW,
            "pending", None, False,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda repo: repo.claim_provider_receipt(receipt), (first, second)))
        self.assertEqual(1, sum(1 for _, execute in results if execute))
        self.assertTrue(all(item.receipt_id == receipt.receipt_id for item, _ in results))
        first.complete_provider_receipt(
            replace(receipt, status=ReceiptStatus.SUCCEEDED,
                    response_classification="accepted_test_action",
                    external_object_ref="opaque_external_object", completed_at=NOW)
        )
        first.close()
        second.close()

        reopened = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        stored = reopened.get_provider_receipt(
            TENANT, COMPANY, receipt.provider, receipt.operation, receipt.idempotency_key
        )
        self.assertIs(ReceiptStatus.SUCCEEDED, stored.status)
        self.assertEqual((), reopened.list_provider_receipts("tenant_other", COMPANY))
        reopened.close()

    def test_retryable_failure_can_be_reclaimed_once(self) -> None:
        repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        receipt = ProviderReceipt(
            "receipt_retry_pg", TENANT, COMPANY, "job_retry_pg",
            "communications.email", "test-email", "send_preapproved_reply",
            "provider_request_retry_pg", "provider_action_retry_pg",
            ReceiptStatus.IN_PROGRESS, NOW, "pending", None, False,
        )
        _, execute = repository.claim_provider_receipt(receipt)
        self.assertTrue(execute)
        repository.complete_provider_receipt(
            replace(receipt, status=ReceiptStatus.FAILED,
                    response_classification="temporary_refusal", retryable=True,
                    completed_at=NOW)
        )
        retry, execute = repository.claim_provider_receipt(receipt)
        self.assertTrue(execute)
        self.assertEqual(2, retry.attempts)
        _, duplicate = repository.claim_provider_receipt(receipt)
        self.assertFalse(duplicate)
        repository.close()


if __name__ == "__main__":
    unittest.main()
