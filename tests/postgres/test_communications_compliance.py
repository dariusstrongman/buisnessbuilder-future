from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import uuid4

from businessbuilder.communications_compliance.models import (
    ComplianceDecision, ConsentEvidence, JurisdictionContext, PIIClass,
    RetentionClass, RetentionRecord, RolloutTier,
)
from businessbuilder.outbound_communications import (
    CommunicationPurpose, ConsentState, DestinationType, PolicyOutcome,
)
from businessbuilder.postgres import PostgresRuntimeRepository
from businessbuilder.postgres.migrations import MIGRATION_VERSION


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_DSN"),
                     "requires an isolated PostgreSQL test database")
class PostgresCommunicationsComplianceTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ["BUSINESSBUILDER_TEST_POSTGRES_DSN"]
        prefix = os.environ.get("BUSINESSBUILDER_TEST_POSTGRES_SCHEMA", "bb_test")
        self.schema = f"{prefix}_compliance_{uuid4().hex[:10]}"

    def test_migration_restart_and_tenant_scoped_compliance_records(self):
        first = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        context = JurisdictionContext(
            "tenant_pg", "company_pg", "US", "US", "recipient_pg", "US",
            NOW, "company_profile_pg")
        first.save_broker_record("communication_compliance_jurisdiction",
                                 "jurisdiction_pg", "tenant_pg", "company_pg", context)
        first.close()
        second = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        self.assertEqual(context, second.get_broker_record(
            "communication_compliance_jurisdiction", "tenant_pg", "company_pg",
            "jurisdiction_pg"))
        self.assertIsNone(second.get_broker_record(
            "communication_compliance_jurisdiction", "tenant_other", "company_pg",
            "jurisdiction_pg"))
        with second.connection.cursor() as cursor:
            cursor.execute("SELECT version FROM bb_schema_migrations")
            self.assertEqual(MIGRATION_VERSION, cursor.fetchone()["version"])
        second.close()

    def test_consent_retention_and_decision_rollback_together(self):
        repository = PostgresRuntimeRepository(self.dsn, schema=self.schema)
        consent = ConsentEvidence(
            "consent_pg", "tenant_pg", "company_pg", "recipient_pg",
            CommunicationPurpose.REPLY_TO_INBOUND, DestinationType.EMAIL,
            ConsentState.CUSTOMER_INITIATED, "canonical_event", NOW,
            "event_pg", "evidence_pg", "communications-compliance.us-federal.v1",
            None, NOW + timedelta(days=30))
        retention = RetentionRecord(
            "retention_pg", "tenant_pg", "company_pg", "consent_evidence",
            consent.consent_evidence_id, (PIIClass.CONSENT_EVIDENCE,),
            RetentionClass.COMPLIANCE_EVIDENCE, NOW, NOW + timedelta(days=2555),
            True, False, None, "communications-compliance.us-federal.v1")
        decision = ComplianceDecision(
            "decision_pg", "tenant_pg", "company_pg", "communication_pg", None,
            "recipient_pg", CommunicationPurpose.REPLY_TO_INBOUND, "US", "US", "US",
            "communications-compliance.us-federal.v1", ConsentState.CUSTOMER_INITIATED,
            "clear", RetentionClass.DELIVERY_OPERATIONS, "not_required",
            RolloutTier.SANDBOX, "clear", PolicyOutcome.ALLOWED,
            "compliance_allowed", "admission", NOW)
        with self.assertRaises(RuntimeError):
            with repository.transaction():
                repository.save_broker_record("communication_compliance_consent",
                    consent.consent_evidence_id, "tenant_pg", "company_pg", consent)
                repository.save_broker_record("communication_compliance_retention",
                    retention.retention_record_id, "tenant_pg", "company_pg", retention)
                repository.save_broker_record("communication_compliance_decision",
                    decision.compliance_decision_id, "tenant_pg", "company_pg", decision)
                raise RuntimeError("rollback proof")
        self.assertIsNone(repository.get_broker_record(
            "communication_compliance_consent", "tenant_pg", "company_pg", "consent_pg"))
        self.assertIsNone(repository.get_broker_record(
            "communication_compliance_retention", "tenant_pg", "company_pg", "retention_pg"))
        self.assertIsNone(repository.get_broker_record(
            "communication_compliance_decision", "tenant_pg", "company_pg", "decision_pg"))
        repository.close()


if __name__ == "__main__":
    unittest.main()
